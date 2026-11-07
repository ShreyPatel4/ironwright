"""The supervisor graph: one change from draft to promoted, only with a human approval.

The sub-agents here are tiny and deterministic, so each test names exactly which attempt
fails and why. The silver builder at the bottom drafts from the real silver draft of the
tenant fixture, which holds a person's name in policies.holder.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow as pa
import pytest

from ironwright.bridge import gate, supervisor
from ironwright.bridge.gate import Change, GateError, State
from ironwright.ingest import silver_draft as sd
from ironwright.ingest.profiler import profile_files
from ironwright.ingest.propose import propose
from ironwright.legacy import read_upload

REQUESTER = "user:requester"
APPROVER = "user:approver"
POLICY = {"default": "deny", "rules": [
    {"id": "builders-promote-silver", "effect": "permit", "principal": "agent:*-builder",
     "action": "action:bridge.promote.silver"}]}
FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws" / "uploads" / "fixture"


def bronze() -> dict[str, pa.Table]:
    return {"policies": pa.table({"policy_no": ["P-1", "P-2", "P-3"],
                                  "holder": ["Ann Lee", "Bo Diaz", "Cy Oh"],
                                  "premium": [100.0, 200.0, 300.0]})}


RAW = "select policy_no, holder, premium from bronze_policies"
MASKED = "select policy_no, sha256(holder) as holder, premium from bronze_policies"


class Builder:
    """Drafts RAW until `fixes_on`, then MASKED. Records the feedback it was given."""
    name = "agent:policy-builder"

    def __init__(self, fixes_on: int | None = 1):
        self.fixes_on = fixes_on
        self.feedback: list[dict | None] = []

    def draft(self, attempt: int, feedback: dict | None) -> Change:
        self.feedback.append(feedback)
        good = self.fixes_on is not None and len(self.feedback) >= self.fixes_on
        return Change(layer="silver", models={"01_policies.sql": MASKED if good else RAW},
                      contract={"policies": [{"name": "policy_no", "type": "string"},
                                             {"name": "holder", "type": "string"}]},
                      tests=[{"name": "policy_no_unique",
                              "sql": "select policy_no from policies group by 1 having count(*) > 1"}],
                      dq=[{"table": "policies", "column": "policy_no", "check": "not_null"}],
                      masking={"policies": {"holder": "hash"}} if good else {},
                      purpose="policy servicing", note=f"attempt {attempt}")


class Spy:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, layer: str, files: dict[str, str]) -> None:
        self.calls.append((layer, dict(files)))


def sup(tmp_path: Path, agent, writer=None, bronze_tables=None) -> supervisor.Supervisor:
    return supervisor.Supervisor(gate.BridgeStore(tmp_path / "bridge"), agent, bronze_tables or bronze(),
                                 policy=POLICY, writer=writer or Spy(), live_models=dict)


def approve(s: supervisor.Supervisor, card_id: str, by: str = APPROVER, role: str = "admin") -> None:
    gate.decide(s.store, card_id, by=by, role=role, decision="approve",
                content_hash=s.store.card(card_id)["content_hash"])


def test_a_change_reaches_promoted_only_after_a_human_approval(tmp_path):
    spy = Spy()
    s = sup(tmp_path, Builder(fixes_on=1), writer=spy)
    out = s.start(requested_by=REQUESTER)
    assert out["interrupted"] and out["state"] == State.ESCALATED
    assert spy.calls == []                       # nothing written live before interrupt()
    card = s.store.card(out["card_id"])
    assert card["kind"] == "promotion" and card["decision"] is None
    assert card["diff"]["01_policies.sql"].startswith("--- live/01_policies.sql")
    assert set(card["scores"]) == {"tests", "contract", "dq", "lineage", "cost", "compliance"}
    assert card["evidence"] and all(e.startswith("ev:") for e in card["evidence"])
    assert "Ann Lee" not in str(card)

    approve(s, out["card_id"])
    done = s.resume(out["run_id"])
    assert done["state"] == State.OBSERVED
    assert spy.calls == [("silver", {"01_policies.sql": MASKED})]
    states = [h["to"] for h in s.store.run(out["run_id"])["history"]]
    assert states == ["drafted", "staged", "evaluated", "escalated", "approved", "promoted", "observed"]
    promoted = [r for r in s.store.audit_rows() if r.get("status") == "promoted" and r["kind"] == "promote"]
    assert promoted[0]["approved_by"] == APPROVER
    assert s.store.verify_audit_chain()


def test_every_supervisor_step_is_a_span_and_an_audit_row(tmp_path):
    s = sup(tmp_path, Builder())
    out = s.start(requested_by=REQUESTER)
    approve(s, out["card_id"])
    s.resume(out["run_id"])
    spans = {sp.name for sp in s.store.tracer.spans}
    for node in ("draft", "stage", "evaluate", "escalate", "approval", "settle", "promote", "observe"):
        assert f"supervisor.{node}" in spans, node
    steps = {r["node"] for r in s.store.audit_rows() if r["kind"] == "step"}
    assert {"draft", "stage", "evaluate", "escalate", "settle", "promote", "observe"} <= steps
    assert "supervisor.watch" in spans


def test_resume_without_a_decision_waits_again_and_writes_nothing(tmp_path):
    spy = Spy()
    s = sup(tmp_path, Builder(), writer=spy)
    out = s.start(requested_by=REQUESTER)
    again = s.resume(out["run_id"])
    assert again["interrupted"] and again["state"] == State.ESCALATED
    assert spy.calls == []


def test_a_forged_resume_payload_is_not_an_approval(tmp_path):
    spy = Spy()
    s = sup(tmp_path, Builder(), writer=spy)
    out = s.start(requested_by=REQUESTER)
    forged = s.resume(out["run_id"], payload={"approved": True, "by": APPROVER, "decision": "approve"})
    assert forged["state"] == State.ESCALATED
    assert spy.calls == []


def test_a_failing_eval_loops_back_to_the_agent_with_its_scores(tmp_path):
    b = Builder(fixes_on=2)
    s = sup(tmp_path, b)
    out = s.start(requested_by=REQUESTER)
    run = s.store.run(out["run_id"])
    assert [e["passed"] for e in run["evaluations"]] == [False, True]
    assert run["evaluations"][0]["failed"] == ["compliance"]
    assert b.feedback[0] is None
    assert b.feedback[1]["failed"] == ["compliance"]
    assert b.feedback[1]["gates"]["compliance"]["unmasked"] == ["policies.holder"]
    assert s.store.card(out["card_id"])["kind"] == "promotion"


def test_three_failures_escalate_as_stuck_with_full_context(tmp_path):
    spy = Spy()
    s = sup(tmp_path, Builder(fixes_on=None), writer=spy)
    out = s.start(requested_by=REQUESTER)
    assert out["state"] == State.ESCALATED
    card = s.store.card(out["card_id"])
    assert card["kind"] == "stuck"
    assert len(card["attempts"]) == gate.MAX_ATTEMPTS
    assert all(a["failed"] == ["compliance"] for a in card["attempts"])
    assert [a["note"] for a in card["attempts"]] == ["attempt 1", "attempt 2", "attempt 3"]
    assert card["compliance"]["unmasked"] == ["policies.holder"]
    assert card["samples"]["after"]["policies"][0]["holder"] == "[masked]"
    with pytest.raises(GateError, match="evaluation"):
        approve(s, out["card_id"])
    assert spy.calls == []


def test_a_rejection_reaches_the_agent_and_starts_a_new_cycle(tmp_path):
    b = Builder(fixes_on=None)
    s = sup(tmp_path, b)
    out = s.start(requested_by=REQUESTER)
    gate.decide(s.store, out["card_id"], by=APPROVER, role="admin", decision="reject",
                content_hash=s.store.card(out["card_id"])["content_hash"], reason="mask holder")
    b.fixes_on = len(b.feedback) + 1
    nxt = s.resume(out["run_id"])
    assert nxt["state"] == State.ESCALATED and nxt["card_id"] != out["card_id"]
    assert b.feedback[3]["rejected"]["reason"] == "mask holder"
    assert s.store.card(nxt["card_id"])["kind"] == "promotion"


def test_a_requester_approval_is_refused_and_a_forged_card_record_cannot_promote(tmp_path):
    spy = Spy()
    s = sup(tmp_path, Builder(), writer=spy)
    out = s.start(requested_by=REQUESTER)
    with pytest.raises(GateError, match="requester"):
        approve(s, out["card_id"], by=REQUESTER)
    rec = s.store.card(out["card_id"])
    s.store.append_card({**rec, "decision": {"decision": "approve", "by": "agent:supervisor", "role": "owner",
                                             "content_hash": rec["content_hash"]}})
    run = s.store.run(out["run_id"])
    run["state"] = State.APPROVED
    s.store.put_run(run)
    res = s.resume(out["run_id"])
    assert res["state"] != State.PROMOTED
    assert "agent" in res["refused"]
    assert spy.calls == []


# ---------------------------------------------------------------- the silver builder on the real draft

def fixture_draft() -> tuple[dict, dict, dict[str, pa.Table]]:
    paths = sorted(FIXTURE.glob("*.csv"))
    by_name = {p.name: p for p in paths}
    profile = profile_files([(p.name, p.read_bytes()) for p in paths])
    workspace = propose(profile, "u" * 32)["workspace"]

    def read(src):
        return read_upload({**src, "kind": "upload", "upload_path": str(by_name[src["upload"].split("/")[1]])})

    made = sd.draft(profile, workspace, read)
    tables = {s["name"]: read(s) for s in workspace["sources"]}
    return workspace, made, tables


def test_the_silver_builder_masks_the_holder_after_compliance_says_so(tmp_path):
    workspace, made, tables = fixture_draft()
    builder = supervisor.SilverBuilder(made, workspace)
    spy = Spy()
    s = sup(tmp_path, builder, writer=spy, bronze_tables=tables)
    out = s.start(requested_by=REQUESTER)
    run = s.store.run(out["run_id"])
    assert [e["passed"] for e in run["evaluations"]] == [False, True], run["evaluations"]
    approve(s, out["card_id"])
    assert s.resume(out["run_id"])["state"] == State.OBSERVED
    files = spy.calls[0][1]
    assert set(files) == {f for f in sd.models(made, workspace, [], {})}
    policies = next(sql for name, sql in files.items() if name.endswith("policies.sql"))
    assert "sha256" in policies


def test_nothing_the_supervisor_does_before_interrupt_touches_a_live_folder(tmp_path):
    live = tmp_path / "live"
    (live / "silver").mkdir(parents=True)
    (live / "silver" / "01_policies.sql").write_text(RAW)

    def digest() -> str:
        h = hashlib.sha256()
        for p in sorted(live.rglob("*")):
            h.update(p.name.encode() + (p.read_bytes() if p.is_file() else b""))
        return h.hexdigest()

    before = digest()
    s = supervisor.Supervisor(gate.BridgeStore(tmp_path / "bridge"), Builder(), bronze(), policy=POLICY,
                              live_dir=live)
    out = s.start(requested_by=REQUESTER)
    assert digest() == before
    assert s.store.card(out["card_id"])["diff"]["01_policies.sql"].count("sha256") == 1
    approve(s, out["card_id"])
    s.resume(out["run_id"])
    assert (live / "silver" / "01_policies.sql").read_text() == MASKED


# ---------------------------------------------------------------- a gold mart over promoted silver

GOLD_POLICY = {"default": "deny", "rules": [
    *POLICY["rules"],
    {"id": "gold-builder-promotes-gold", "effect": "permit", "principal": "agent:mart-builder",
     "action": "action:bridge.promote.gold"}]}
MART = ("select policy_no, premium, premium * 12 as premium_year from silver_policies")


class MartBuilder:
    name = "agent:mart-builder"

    def draft(self, attempt: int, feedback: dict | None) -> Change:
        return Change(layer="gold", models={"01_mart_premium.sql": MART},
                      contract={"mart_premium": [{"name": "policy_no", "type": "string"},
                                                 {"name": "premium_year", "type": "double"}]},
                      tests=[{"name": "one_row_per_policy",
                              "sql": "select policy_no from mart_premium group by 1 having count(*) > 1"}],
                      dq=[{"table": "mart_premium", "column": "policy_no", "check": "not_null"}],
                      purpose="premium reporting", note=f"attempt {attempt}")


def test_a_mart_over_promoted_silver_goes_to_gold_only_with_a_human_approval(tmp_path):
    live = tmp_path / "live"
    store = gate.BridgeStore(tmp_path / "bridge")
    silver = supervisor.Supervisor(store, Builder(), bronze(), policy=GOLD_POLICY, live_dir=live)
    first = silver.start(requested_by=REQUESTER)
    approve(silver, first["card_id"])
    assert silver.resume(first["run_id"])["state"] == State.OBSERVED

    mart = supervisor.Supervisor(store, MartBuilder(), bronze(), policy=GOLD_POLICY, live_dir=live)
    out = mart.start(requested_by=REQUESTER)
    assert out["state"] == State.ESCALATED, store.run(out["run_id"])["evaluations"]
    card = store.card(out["card_id"])
    assert card["kind"] == "promotion" and card["layer"] == "gold"
    assert not (live / "gold").exists()
    assert card["samples"]["after"]["mart_premium"][0]["premium_year"] == 1200.0
    with pytest.raises(GateError, match="requester"):
        approve(mart, out["card_id"], by=REQUESTER)
    approve(mart, out["card_id"])
    assert mart.resume(out["run_id"])["state"] == State.OBSERVED
    assert (live / "gold" / "01_mart_premium.sql").read_text() == MART
