"""The promotion gate: the state machine, the sandbox, the evaluator and promote().

Everything here runs real SQL on real Arrow tables in DuckDB. The numbers come from the
tables below, read by hand: claims has 4 rows and 3 distinct claims, policies has 3 rows
and one holder column, which is a person's name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from ironwright.bridge import evaluate as ev
from ironwright.bridge import gate
from ironwright.bridge.gate import Change, GateError, State

REQUESTER = "user:requester"
APPROVER = "user:approver"
AGENT = "agent:silver-builder"
POLICY = {"default": "deny", "rules": [
    {"id": "silver-builder-promotes-silver", "effect": "permit", "principal": AGENT,
     "action": "action:bridge.promote.silver"}]}


def bronze() -> dict[str, pa.Table]:
    return {
        "claims": pa.table({"claim_id": ["C-1", "C-2", "C-2", "C-3"],
                            "policy_no": ["P-1", "P-1", "P-1", "P-2"],
                            "amount": [10.0, 20.0, 20.0, None]}),
        "policies": pa.table({"policy_no": ["P-1", "P-2", "P-3"],
                              "holder": ["Ann Lee", "Bo Diaz", "Cy Oh"],
                              "premium": [100.0, 200.0, 300.0]}),
    }


GOOD_CLAIMS = "select distinct claim_id, policy_no, amount from bronze_claims"
MASKED_POLICIES = "select policy_no, sha256(holder) as holder, premium from bronze_policies"
RAW_POLICIES = "select policy_no, holder, premium from bronze_policies"


def change(policies_sql: str = MASKED_POLICIES, **over) -> Change:
    base = {
        "layer": "silver",
        "models": {"01_claims.sql": GOOD_CLAIMS, "02_policies.sql": policies_sql},
        "contract": {"claims": [{"name": "claim_id", "type": "string"}, {"name": "policy_no", "type": "string"},
                             {"name": "amount", "type": "double"}],
                  "policies": [{"name": "policy_no", "type": "string"}, {"name": "holder", "type": "string"},
                               {"name": "premium", "type": "double"}]},
        "tests": [{"name": "claim_id_unique",
                "sql": "select claim_id from claims group by 1 having count(*) > 1"}],
        "dq": [{"table": "claims", "column": "claim_id", "check": "not_null"},
            {"table": "policies", "column": "policy_no", "check": "unique"}],
        "masking": {"policies": {"holder": "hash"}} if policies_sql == MASKED_POLICIES else {},
        "purpose": "claims operations reporting",
        "agent": AGENT, "requested_by": REQUESTER}
    base.update(over)
    return Change(**base)


@pytest.fixture()
def store(tmp_path: Path) -> gate.BridgeStore:
    return gate.BridgeStore(tmp_path / "bridge")


# ---------------------------------------------------------------- the state machine

def test_every_arrow_in_the_contract_diagram_is_allowed_and_nothing_else():
    arrows = {(None, State.DRAFTED), (State.DRAFTED, State.STAGED), (State.STAGED, State.EVALUATED),
              (State.EVALUATED, State.DRAFTED), (State.EVALUATED, State.ESCALATED),
              (State.ESCALATED, State.APPROVED), (State.ESCALATED, State.DRAFTED),
              (State.APPROVED, State.PROMOTED), (State.PROMOTED, State.OBSERVED),
              (State.OBSERVED, State.DRAFTED)}
    for a in [None, *State]:
        for b in State:
            assert gate.allowed(a, b) == ((a, b) in arrows), (a, b)


def test_an_illegal_transition_is_refused_and_leaves_no_row(store):
    run = gate.open_run(store, change(), sample_rows=50)
    assert run["state"] == State.DRAFTED
    rows = len(store.audit_rows())
    with pytest.raises(GateError):
        gate.transition(store, run["run_id"], State.PROMOTED, actor=AGENT)
    assert store.run(run["run_id"])["state"] == State.DRAFTED
    assert len(store.audit_rows()) == rows


def test_every_transition_is_a_span_and_an_audit_row(store):
    run = gate.open_run(store, change(), sample_rows=50)
    gate.transition(store, run["run_id"], State.STAGED, actor=AGENT)
    rows = [r for r in store.audit_rows() if r["run_id"] == run["run_id"]]
    assert [r["to"] for r in rows if r["kind"] == "transition"] == ["drafted", "staged"]
    spans = [s for s in store.tracer.spans if s.name == "bridge.transition"]
    assert [s.attributes["to"] for s in spans] == ["drafted", "staged"]
    assert store.verify_audit_chain()


def test_the_audit_chain_notices_an_edited_row(store):
    gate.open_run(store, change(), sample_rows=50)
    path = store.root / "audit.jsonl"
    lines = path.read_text().splitlines()
    row = json.loads(lines[0])
    row["actor"] = "user:someone-else"
    path.write_text("\n".join([json.dumps(row, sort_keys=True)] + lines[1:]) + "\n")
    assert not store.verify_audit_chain()


# ---------------------------------------------------------------- the sandbox

def test_staging_runs_in_an_isolated_schema_on_a_sample():
    staged = gate.stage(change(), bronze(), run_id="r1", sample_rows=2)
    try:
        assert staged.schema == "sandbox_r1"
        assert staged.inputs == {"claims": 2, "policies": 2}
        assert staged.tables["claims"]["rows"] == 2   # two sampled rows, C-1 and C-2
        db = staged.con.execute("select current_database()").fetchone()[0]
        assert db == "sandbox_r1"
    finally:
        staged.close()


def test_a_model_that_writes_is_refused_before_it_runs():
    bad = change(models={"01_claims.sql": "drop table bronze_claims"})
    with pytest.raises(GateError, match="(?i)select"):
        gate.stage(bad, bronze(), run_id="r2", sample_rows=10)


def test_the_samples_on_a_staged_change_mask_pii():
    staged = gate.stage(change(RAW_POLICIES), bronze(), run_id="r3", sample_rows=10)
    try:
        flat = json.dumps(staged.samples(ev.pii_tags(bronze())))
    finally:
        staged.close()
    assert "Ann Lee" not in flat
    assert "P-1" in flat


# ---------------------------------------------------------------- the evaluator

def scores(c: Change, **kw):
    staged = gate.stage(c, bronze(), run_id="ev", sample_rows=100)
    try:
        return ev.evaluate(c, staged, pii=ev.pii_tags(bronze()), **kw)
    finally:
        staged.close()


def test_a_good_change_passes_every_gate_with_evidence_ids():
    s = scores(change())
    assert s["passed"], s
    assert set(s["gates"]) == {"tests", "contract", "dq", "lineage", "cost", "compliance"}
    assert all(g["passed"] for g in s["gates"].values())
    assert all(g["evidence"].startswith("ev:") for g in s["gates"].values())
    assert sorted(s["evidence"]) == sorted(g["evidence"] for g in s["gates"].values())


def test_the_same_change_scores_the_same_twice():
    assert scores(change()) == scores(change())


def test_a_failing_test_fails_the_tests_gate():
    s = scores(change(models={"01_claims.sql": "select claim_id, policy_no, amount from bronze_claims",
                              "02_policies.sql": MASKED_POLICIES}))
    assert not s["passed"]
    assert s["gates"]["tests"]["failed"] == ["claim_id_unique"]


def test_a_missing_contract_column_fails_the_contract_gate():
    s = scores(change(models={"01_claims.sql": "select distinct claim_id, policy_no from bronze_claims",
                              "02_policies.sql": MASKED_POLICIES}))
    assert not s["gates"]["contract"]["passed"]
    assert "claims.amount" in s["gates"]["contract"]["missing"]


def test_dq_pass_rate_below_threshold_fails():
    c = change(dq=[{"table": "claims", "column": "amount", "check": "not_null"}])
    s = scores(c, dq_threshold=0.95)
    assert s["gates"]["dq"]["rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert not s["gates"]["dq"]["passed"]


def test_lineage_coverage_may_never_drop():
    s = scores(change(), baseline_coverage=1.0, coverage=lambda c, st: 0.5)
    assert not s["gates"]["lineage"]["passed"]
    assert s["gates"]["lineage"]["before"] == 1.0 and s["gates"]["lineage"]["after"] == 0.5
    assert scores(change(), baseline_coverage=0.5, coverage=lambda c, st: 0.5)["gates"]["lineage"]["passed"]


def test_a_fan_out_join_breaks_the_cost_budget():
    fan = ("select c.claim_id, c.policy_no, c.amount from bronze_claims c "
           "cross join bronze_policies p")
    s = scores(change(models={"01_claims.sql": fan, "02_policies.sql": MASKED_POLICIES}, tests=[]))
    assert not s["gates"]["cost"]["passed"]
    assert s["gates"]["cost"]["ratio"] > s["gates"]["cost"]["budget"]


def test_profiler_columns_get_pii_tags():
    tags = ev.pii_tags(bronze())
    assert tags["holder"] == "person_name"
    assert "premium" not in tags and "claim_id" not in tags
    assert ev.pii_tags({"t": pa.table({"contact_email": ["a@b.c"]})})["contact_email"] == "email"


def test_unmasked_pii_fails_compliance():
    s = scores(change(RAW_POLICIES))
    comp = s["gates"]["compliance"]
    assert not comp["passed"]
    assert comp["unmasked"] == ["policies.holder"]


def test_masking_must_be_proved_on_the_rows_not_just_declared():
    fake = change(RAW_POLICIES, masking={"policies": {"holder": "hash"}})
    comp = scores(fake)["gates"]["compliance"]
    assert not comp["passed"]
    assert comp["unmasked"] == ["policies.holder"]


def test_a_purpose_can_allow_a_pii_class_through_unmasked():
    allow = {"purposes": {"claims operations reporting": ["person_name"]}}
    assert scores(change(RAW_POLICIES), compliance_policy=allow)["gates"]["compliance"]["passed"]


def test_no_declared_purpose_fails_compliance():
    comp = scores(change(purpose=""))["gates"]["compliance"]
    assert not comp["passed"]
    assert comp["purpose"] is None


# ---------------------------------------------------------------- promote, in the fixed order

def escalated(store, c: Change | None = None, tmp: Path | None = None) -> tuple[str, str]:
    """A change walked honestly to Escalated, with its card in the inbox."""
    c = c or change()
    run = gate.open_run(store, c, sample_rows=100)
    rid = run["run_id"]
    while True:
        gate.transition(store, rid, State.STAGED, actor=AGENT)
        staged = gate.stage(c, bronze(), run_id=rid, sample_rows=100)
        try:
            s = ev.evaluate(c, staged, pii=ev.pii_tags(bronze()))
            gate.record_evaluation(store, rid, s, actor="agent:evaluator")
            if s["passed"] or store.run(rid)["attempt"] >= gate.MAX_ATTEMPTS:
                card = gate.package_card(store, rid, staged, s, live_models={}, pii=ev.pii_tags(bronze()))
                return rid, card["card_id"]
        finally:
            staged.close()
        gate.retry(store, rid, actor="agent:supervisor")
        gate.set_change(store, rid, c)


def test_promote_needs_an_approval(store, tmp_path):
    rid, _ = escalated(store)
    with pytest.raises(GateError, match="Approved"):
        gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    assert not (tmp_path / "live").exists()


def test_a_human_approval_promotes_through_the_fixed_order(store, tmp_path):
    rid, card = escalated(store)
    gate.decide(store, card, by=APPROVER, role="admin", decision="approve",
                content_hash=store.card(card)["content_hash"])
    out = gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    assert out["status"] == "promoted"
    assert out["steps"] == ["idempotency", "policy", "params", "preconditions", "approval",
                            "edits", "outbox", "audit"]
    assert (tmp_path / "live" / "silver" / "02_policies.sql").read_text() == MASKED_POLICIES
    assert store.run(rid)["state"] == State.PROMOTED
    assert store.outbox()[-1]["run_id"] == rid
    assert store.verify_audit_chain()
    again = gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    assert again["replayed"] is True


def test_policy_runs_before_validation_and_denies_by_default(store, tmp_path):
    rid, card = escalated(store)
    gate.decide(store, card, by=APPROVER, role="admin", decision="approve",
                content_hash=store.card(card)["content_hash"])
    out = gate.promote(store, rid, live_dir=tmp_path / "live", policy={"default": "deny", "rules": []})
    assert out["status"] == "denied"
    assert out["steps"] == ["idempotency", "policy"]
    assert not (tmp_path / "live").exists()
    assert store.audit_rows()[-1]["status"] == "denied"


@pytest.mark.parametrize("by,role,why", [
    (REQUESTER, "admin", "requester"),
    (AGENT, "admin", "agent"),
    ("agent:supervisor", "owner", "agent"),
    ("user:viewer", "member", "role"),
])
def test_a_forged_approval_is_refused(store, by, role, why):
    _, card = escalated(store)
    with pytest.raises(GateError, match=why):
        gate.decide(store, card, by=by, role=role, decision="approve",
                    content_hash=store.card(card)["content_hash"])
    assert store.card(card)["decision"] is None


def test_an_approval_for_another_version_of_the_change_is_refused(store):
    _, card = escalated(store)
    with pytest.raises(GateError, match="hash"):
        gate.decide(store, card, by=APPROVER, role="admin", decision="approve", content_hash="0" * 64)


def test_a_change_edited_after_approval_cannot_promote(store, tmp_path):
    rid, card = escalated(store)
    gate.decide(store, card, by=APPROVER, role="admin", decision="approve",
                content_hash=store.card(card)["content_hash"])
    run = store.run(rid)
    run["change"]["models"]["02_policies.sql"] = RAW_POLICIES
    store.put_run(run)
    with pytest.raises(GateError, match="hash"):
        gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    assert not (tmp_path / "live").exists()


def test_a_decision_written_straight_into_the_card_file_is_not_an_approval(store, tmp_path):
    rid, card = escalated(store)
    # Bypass decide(): forge the record and the run state by hand.
    rec = store.card(card)
    store.append_card({**rec, "decision": {"decision": "approve", "by": REQUESTER, "role": "owner",
                                           "content_hash": rec["content_hash"]}})
    run = store.run(rid)
    run["state"] = State.APPROVED
    store.put_run(run)
    with pytest.raises(GateError, match="requester"):
        gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    assert not (tmp_path / "live").exists()


def test_a_rejection_sends_the_change_back_to_drafted_with_the_reason(store):
    rid, card = escalated(store)
    gate.decide(store, card, by=APPROVER, role="admin", decision="reject",
                content_hash=store.card(card)["content_hash"], reason="keep premium in cents")
    assert store.run(rid)["state"] == State.DRAFTED
    assert store.card(card)["decision"]["reason"] == "keep premium in cents"


def test_a_failed_evaluation_cannot_be_approved(store):
    rid, card = escalated(store, change(RAW_POLICIES))
    assert store.card(card)["kind"] == "stuck" or not store.run(rid)["evaluation"]["passed"]
    with pytest.raises(GateError, match="evaluation"):
        gate.decide(store, card, by=APPROVER, role="admin", decision="approve",
                    content_hash=store.card(card)["content_hash"])


def test_an_observed_change_reopens_on_drift(store, tmp_path):
    rid, card = escalated(store)
    gate.decide(store, card, by=APPROVER, role="admin", decision="approve",
                content_hash=store.card(card)["content_hash"])
    gate.promote(store, rid, live_dir=tmp_path / "live", policy=POLICY)
    gate.observe(store, rid, monitors=["freshness", "drift", "dq"])
    gate.reopen(store, rid, reason="drift on policies.premium")
    assert store.run(rid)["state"] == State.DRAFTED


# ---------------------------------------------------------------- a rename carries no PII past the gate

ALIASED = "select policy_no, holder as owner_label, premium from bronze_policies"


def test_an_aliased_pii_column_fails_compliance():
    comp = scores(change(ALIASED))["gates"]["compliance"]
    assert not comp["passed"]
    assert comp["unmasked"] == ["policies.owner_label"]


def test_an_aliased_pii_column_is_masked_on_the_card():
    staged = gate.stage(change(ALIASED), bronze(), run_id="r4", sample_rows=10)
    try:
        flat = json.dumps(staged.samples(ev.pii_tags(bronze())))
    finally:
        staged.close()
    assert "Ann Lee" not in flat


# ---------------------------------------------------------------- two requests at once

def test_concurrent_audit_appends_keep_the_chain(store):
    import threading
    rid = gate.open_run(store, change(), sample_rows=10)["run_id"]

    def write(i: int) -> None:
        for j in range(20):
            gate.BridgeStore(store.root).audit(kind="probe", run_id=rid, actor=AGENT, status=f"{i}.{j}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = store.audit_rows()
    assert len(rows) == 1 + 6 * 20
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert store.verify_audit_chain()


def test_two_admins_deciding_at_once_settle_one_decision(store):
    import threading
    rid, card = escalated(store)
    h = store.card(card)["content_hash"]
    out: dict[str, str] = {}

    def go(who: str, decision: str) -> None:
        try:
            gate.decide(gate.BridgeStore(store.root), card, by=who, role="admin", decision=decision,
                        content_hash=h, reason="no")
            out[who] = "ok"
        except GateError as e:
            out[who] = str(e)

    threads = [threading.Thread(target=go, args=(f"user:admin{i}", "approve" if i % 2 else "reject"))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert list(out.values()).count("ok") == 1
    lines = [c for c in store._lines("cards.jsonl") if c["card_id"] == card and c["decision"]]
    assert len(lines) == 1
    state = store.run(rid)["state"]
    assert state == ("approved" if lines[0]["decision"]["decision"] == "approve" else "drafted")
