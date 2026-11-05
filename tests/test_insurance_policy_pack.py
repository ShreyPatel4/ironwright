"""The insurance_policy pack: every planted defect, checked against ground truth.

The generic eval layers grade thresholds (accuracy >= 0.9). These checks are exact, so one
missed defect is a red build. Each planted entry names the check that must see it, and every
assertion here reads the pipeline's own artifacts, never the generator's internals.
"""
from __future__ import annotations

import argparse
import json

import pytest

from ironwright.cli import STAGES
from ironwright.lake import Lakehouse
from ironwright.ontology.runtime import Ontology
from ironwright.packs import load_pack
from ironwright.tracing import Tracer

PACK = "insurance_policy"
DEFECTS = ("overlapping_coverage", "endorsement_out_of_order", "lapsed_policy_paid_claim",
           "duplicate_insured", "limit_cents_vs_dollars", "retro_dated_cancellation",
           "orphan_coverage_rows", "wrong_effective_timezone")


def _run(root, seed: int):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("IRONWRIGHT_DATA", str(root))
        pack = load_pack(PACK)
        tracer = Tracer(pack.paths.ensure().traces)
        args = argparse.Namespace(seed=seed, limit=None)
        for stage in ("generate", "migrate", "reconcile", "ontology", "agents", "evals"):
            STAGES[stage](pack, tracer, args)
    return pack


@pytest.fixture(scope="module", params=[7, 47], ids=["seed7", "seed47"])
def run(tmp_path_factory, request):
    """Two seeds, so a detector that only fits one dataset goes red."""
    root = tmp_path_factory.mktemp(f"policy{request.param}")
    pack = _run(root, request.param)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("IRONWRIGHT_DATA", str(root))
        yield {"pack": pack, "gt": json.loads(pack.paths.ground_truth.read_text()),
               "con": Lakehouse(pack).connect(),
               "report": lambda name: json.loads((pack.paths.reports / f"{name}.json").read_text())}


def _planted(run, kind):
    entries = [p for p in run["gt"]["planted"] if p["kind"] == kind]
    assert len(entries) == 1, f"{kind} must be planted exactly once"
    return entries[0]


def _checks(run):
    return {f"{w['table']}:{c['check']}": c for w in run["report"]("migrate")["writes"] for c in w["checks"]}


def _rows(run, sql, *params):
    return run["con"].execute(sql, list(params)).fetchall()


# ---------- the eval layers ----------

def test_every_eval_layer_passes(run):
    rep = run["report"]("evals")
    assert rep["passed"], [(x["layer"], x["headline"]) for x in rep["layers"] if not x["passed"]]


def test_ground_truth_plants_exactly_the_eight_defects(run):
    assert sorted(p["kind"] for p in run["gt"]["planted"]) == sorted(DEFECTS)
    for p in run["gt"]["planted"]:
        assert p["what"] and p["expected"], p["kind"]


# ---------- each defect: the register finds exactly what was planted ----------

@pytest.mark.parametrize("kind", DEFECTS)
def test_register_finds_exactly_the_planted_subjects(run, kind):
    found = sorted(r[0] for r in _rows(run, "select subject from gold_defect_register where defect = ?", kind))
    assert found == sorted(_planted(run, kind)["expected"])


def test_register_has_no_defect_kind_nobody_planted(run):
    assert {r[0] for r in _rows(run, "select distinct defect from gold_defect_register")} == set(DEFECTS)


@pytest.mark.parametrize("kind", DEFECTS)
def test_the_named_quality_check_or_layer_sees_the_defect(run, kind):
    """The register is one witness. The check each entry names is a second, on the eval's own report."""
    p = _planted(run, kind)
    if "check" in p:
        c = _checks(run)[p["check"]]
        assert not c["passed"], p["check"]
        assert c["failing_rows"] == p["failing_rows"], p["check"]
        assert c["severity"] in ("warn", "quarantine"), "a defect must not stop the table publishing"
    else:
        assert kind == "duplicate_insured"  # graded by the ontology layer below


# ---------- each defect: the wrong reading is really wrong ----------

def _verdict(run, claim):
    cols = ("reason", "covered", "cov_id", "limit_dollars", "deductible", "matched_coverages", "net_payable")
    row = _rows(run, f"select {', '.join(cols)} from gold_coverage_verification where claim_id = ?", claim)
    assert len(row) == 1, claim
    return dict(zip(cols, row[0]))


def test_overlap_claim_is_not_silently_assigned_to_one_coverage(run):
    claim = _planted(run, "overlapping_coverage")["claim"]
    v = _verdict(run, claim)
    assert v["reason"] == "overlapping_coverage" and v["matched_coverages"] == 2 and not v["covered"]


def test_adjacent_renewal_terms_are_not_overlaps(run):
    """Decoy: one term ends the minute the next begins."""
    assert run["gt"]["decoys"]["adjacent_renewals"]
    flagged = " ".join(r[0] for r in _rows(run, "select subject from gold_defect_register where defect = 'overlapping_coverage'"))
    for cov in run["gt"]["decoys"]["adjacent_renewals"]:
        assert cov not in flagged


def test_out_of_order_endorsement_keeps_the_higher_sequence(run):
    p = _planted(run, "endorsement_out_of_order")
    cov, right, wrong = p["expected"][0], p["right_limit"], p["naive_limit"]
    assert right != wrong
    # last booked wins would read the stale limit: prove the trap is live in the raw data
    assert _rows(run, "select cast(limit_amt as bigint) from bronze_coverage_admin where cov_id = ? "
                      "order by booked_ts desc limit 1", cov)[0][0] == wrong
    v = _verdict(run, p["claim"])
    assert v["limit_dollars"] == right and v["reason"] == "covered"


def test_late_endorsement_still_governs_the_period_nothing_else_covers(run):
    p = _planted(run, "endorsement_out_of_order")
    rows = _rows(run, "select limit_dollars from silver_coverage_versions where cov_id = ? and tx_to is null "
                      "and valid_from <= ? and ? < valid_to", p["expected"][0], p["gap_ts"], p["gap_ts"])
    assert rows == [(p["naive_limit"],)]


def test_lapsed_paid_claim_is_flagged_and_a_reinstated_policy_is_not(run):
    p = _planted(run, "lapsed_policy_paid_claim")
    for claim in p["expected"]:
        assert _verdict(run, claim)["reason"] == "lapsed_on_loss_date"
    claim = run["gt"]["decoys"]["reinstated_before_loss"]
    assert _verdict(run, claim)["reason"] == "covered"
    assert claim not in p["expected"]


def test_cents_limit_is_normalized_and_flips_the_over_limit_call(run):
    p = _planted(run, "limit_cents_vs_dollars")
    v = _verdict(run, p["claim"])
    assert v["limit_dollars"] == p["right_limit"] and v["reason"] == "over_limit"
    assert p["raw_limit"] == p["right_limit"] * 100
    assert _rows(run, "select cast(limit_amt as bigint) from bronze_coverage_admin where cov_id = ?",
                 v["cov_id"])[0][0] == p["raw_limit"]


def test_retro_cancellation_flips_between_known_then_and_known_now(run):
    p = _planted(run, "retro_dated_cancellation")
    row = _rows(run, "select in_force_known_at_loss, in_force_known_now, reason from gold_coverage_verification "
                     "where claim_id = ?", p["claim"])[0]
    assert row == (True, False, "retro_cancelled")


def test_same_day_cancellation_is_not_retro(run):
    decoy = run["gt"]["decoys"]["same_day_cancellation"]
    assert decoy not in _planted(run, "retro_dated_cancellation")["expected"]


def test_orphan_rows_land_in_quarantine_and_never_reach_silver(run):
    p = _planted(run, "orphan_coverage_rows")
    lake = Lakehouse(run["pack"])
    quarantined = lake.read("silver.coverage_versions__quarantine").to_pylist()
    assert {r["cov_id"] for r in quarantined} == set(p["expected"])
    assert not _rows(run, "select 1 from silver_coverage_versions where cov_id in "
                          f"({', '.join('?' * len(p['expected']))})", *p["expected"])


def test_wrong_timezone_flips_a_loss_the_night_before_inception(run):
    p = _planted(run, "wrong_effective_timezone")
    claim = p["claim"]
    v = _verdict(run, claim)
    assert v["reason"] == "no_coverage_in_force" and v["cov_id"] is None
    # reading the wrong label literally says the policy had already started
    row = _rows(run, """select count(*) from silver_coverage_versions c, silver_claims l
                        where l.claim_id = ? and c.policy_nbr = l.policy_nbr and c.coverage_code = l.peril
                          and c.recorded_valid_from <= l.loss_ts and l.loss_ts < c.valid_from""", claim)
    assert row[0][0] >= 1


# ---------- bitemporal silver ----------

@pytest.mark.parametrize("table,key", [("silver_policy_versions", "policy_nbr"), ("silver_coverage_versions", "cov_id")])
def test_silver_rectangles_never_overlap_in_the_two_time_planes(run, table, key):
    """An as-of join must return at most one row per key, for any pair of times."""
    bad = _rows(run, f"""select a.{key} from {table} a join {table} b on a.{key} = b.{key} and a.version_id < b.version_id
                         where a.valid_from < b.valid_to and b.valid_from < a.valid_to
                           and a.tx_from < coalesce(b.tx_to, timestamp '9999-12-31')
                           and b.tx_from < coalesce(a.tx_to, timestamp '9999-12-31')""")
    assert not bad


@pytest.mark.parametrize("table", ["silver_policy_versions", "silver_coverage_versions"])
def test_silver_time_columns_are_well_formed(run, table):
    assert not _rows(run, f"select 1 from {table} where valid_from >= valid_to or tx_from >= tx_to")


def test_as_of_join_answers_what_we_knew_and_when(run):
    p = _planted(run, "retro_dated_cancellation")
    policy, loss_ts, booked = p["expected"][0], p["loss_ts"], p["booked_ts"]
    sql = """select status from silver_policy_versions where policy_nbr = ? and valid_from <= ? and ? < valid_to
             and tx_from <= ? and ? < coalesce(tx_to, timestamp '9999-12-31')"""
    before = _rows(run, sql, policy, loss_ts, loss_ts, loss_ts, loss_ts)
    after = _rows(run, sql, policy, loss_ts, loss_ts, booked, booked)
    assert before == [("ACTIVE",)] and after == [("CANCELLED",)]


# ---------- ontology and identity ----------

def test_identity_finds_every_duplicate_insured_with_no_false_merge(run):
    ident = run["report"]("evals")
    metrics = next(x for x in ident["layers"] if x["layer"] == "ontology")["metrics"]["identity"]["Insured"]
    assert metrics["precision"] == 1.0 and metrics["recall"] == 1.0
    assert metrics["true_pairs"] > 0
    assert run["gt"]["decoys"]["same_name_different_person"]


def test_same_name_different_person_stays_two_insureds(run):
    con = Ontology(run["pack"], attach_lake=False, read_only=True)
    try:
        for a, b in run["gt"]["decoys"]["same_name_different_person"]:
            ids = con.store.con.execute("select distinct id from crosswalk where type = 'Insured' and "
                                        "system || ':' || source_key in (?, ?)", [a, b]).fetchall()
            assert len(ids) == 2, (a, b)
    finally:
        con.store.con.close()


def test_ontology_has_the_five_object_types():
    pack = load_pack(PACK)
    assert set(pack.ontology["object_types"]) == {"Policy", "Coverage", "Insured", "Claim", "Agent"}
    assert "resolution" in pack.ontology["object_types"]["Insured"]


def test_insured_identifiers_are_masked_for_a_principal_without_the_permit(run):
    ont = Ontology(run["pack"], attach_lake=False, read_only=True)
    try:
        oid = ont.store.con.execute("select id from objects where type = 'Insured' limit 1").fetchone()[0]
        masked = ont.masked(ont.store.get("Insured", oid), "agent:coverage-triage")
        # match_key too: a digest of a birth date and four digits is small enough to brute force
        assert masked["dob"] == masked["gov_last4"] == masked["match_key"] == "***"
        seen = ont.masked(ont.store.get("Insured", oid), "user:coverage.counsel.west")
        assert seen["dob"] != "***"
    finally:
        ont.store.con.close()


def test_the_match_key_does_not_carry_the_identifiers_in_the_clear(run):
    row = _rows(run, "select match_key, dob, gov_last4 from silver_agency_clients limit 1")[0]
    assert row[1] not in row[0] and row[2] not in row[0]


# ---------- the one governed action, and default deny ----------

def test_there_is_exactly_one_action_and_it_needs_approval():
    pack = load_pack(PACK)
    assert list(pack.ontology["actions"]) == ["flag_coverage_review"]
    action = pack.ontology["actions"]["flag_coverage_review"]
    assert action["requires_approval"] is True and action["approver_roles"]
    assert pack.policies["default"] == "deny"


def _scratch(run, tmp_path):
    import shutil
    db = tmp_path / "scratch.duckdb"
    shutil.copy(run["pack"].paths.ontology_db, db)
    return Ontology(run["pack"], attach_lake=False, db_path=db)


def _open_claim(ont):
    return ont.store.con.execute("select id from objects where type = 'Claim' "
                                 "and (props->>'status') = 'OPEN' limit 1").fetchone()[0]


def test_without_approval_the_flag_changes_nothing(run, tmp_path):
    ont = _scratch(run, tmp_path)
    try:
        claim = _open_claim(ont)
        r = ont.act("flag_coverage_review", {"claim_id": claim, "reason": "x"}, actor="agent:coverage-triage",
                    idempotency_key="t:noapproval")
        assert r["status"] == "needs_approval"
        assert ont.store.get("Claim", claim)["status"] == "OPEN"
    finally:
        ont.store.con.close()


@pytest.mark.parametrize("actor", ["agent:intruder", "user:someone.else", "agent:claims-triage"])
def test_default_deny_holds_for_anyone_not_permitted(run, tmp_path, actor):
    ont = _scratch(run, tmp_path)
    try:
        claim = _open_claim(ont)
        r = ont.act("flag_coverage_review", {"claim_id": claim, "reason": "x"}, actor=actor,
                    approved_by="user:coverage.counsel.west", idempotency_key=f"t:{actor}")
        assert r["status"] == "denied", r
        assert ont.store.get("Claim", claim)["status"] == "OPEN"
    finally:
        ont.store.con.close()


# ---------- the usecase ----------

def _triage(run):
    return run["report"]("agents")["coverage_triage"]["runs"]


def test_triage_gets_every_claim_right_not_just_ninety_percent(run):
    truth = run["gt"]["usecases"]["coverage_triage"]
    got = {r["gt_key"]: r["diagnosis"] for r in _triage(run)}
    assert got == truth


def test_every_triage_decision_cites_evidence_it_gathered(run):
    for r in _triage(run):
        assert r["grounded"] and r["cited"], r["gt_key"]
        assert set(r["cited"]) <= set(r["evidence_ids"]), r["gt_key"]
        assert "[ev:" in r["narrative"], r["gt_key"]


def test_covered_claims_take_no_action_and_review_claims_wait_for_a_human(run):
    for r in _triage(run):
        if r["diagnosis"] == "covered":
            assert r["proposal"]["status"] == "no_action" and not r["interrupted"], r["gt_key"]
        else:
            assert r["interrupted"], r["gt_key"]
            assert r["outcome"]["status"] == "applied" and r["outcome"]["approved_by"], r["gt_key"]


def test_every_triage_label_is_reachable(run):
    labels = {r["diagnosis"] for r in _triage(run)}
    assert labels == {"covered", "review_lapsed", "review_retro_cancelled", "review_cancelled", "review_overlap",
                      "review_no_coverage", "review_over_limit"}


# ---------- reconciliation ----------

def test_every_coverage_row_is_published_or_named_as_an_orphan(run):
    rules = {r["name"]: r for r in run["report"]("reconcile")["rules"]}
    rule = rules["Every coverage contract is published or named as an orphan"]
    assert rule["counts"] == {"match": 1, "within_tolerance": 0, "explained": 0, "unexplained": 0}


def test_cents_bug_shows_up_as_an_explained_difference_in_the_legacy_report(run):
    rules = {r["name"]: r for r in run["report"]("reconcile")["rules"]}
    rule = rules["Limit exposure per coverage vs the policy admin report"]
    assert rule["counts"]["explained"] >= 1 and rule["counts"]["unexplained"] == 0
    assert run["report"]("reconcile")["cutover_ready"]


# ---------- it is a pack, and it behaves like the others ----------

def test_data_cells_stay_clean_of_emails_and_urls(run):
    """The narration guardrail would rewrite these; synthetic cells must not trip it."""
    for table in ("silver_claims", "silver_agency_clients", "silver_agents"):
        cols = [r[0] for r in run["con"].execute(f"describe {table}").fetchall()]
        for col in cols:
            assert not _rows(run, f"select 1 from {table} where cast({col} as varchar) similar to '.*(@|https?://).*' limit 1"), (table, col)


def test_the_cli_run_with_no_seed_exports_and_is_green(tmp_path, monkeypatch):
    """The real door: `ironwright run --pack insurance_policy`, default seed, export included."""
    from ironwright.cli import main
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    web = tmp_path / "web"
    main(["run", "--pack", PACK, "--web-dir", str(web)])
    data = json.loads((web / "data" / f"{PACK}.json").read_text())
    assert data["evals"]["passed"]
    assert sorted(p["kind"] for p in data["planted"]) == sorted(DEFECTS)
    from ironwright.verification import derive
    assert derive(load_pack(PACK))["level"] == "L3"


def test_ask_examples_route_and_answer(tmp_path, monkeypatch):
    from ironwright.ask import ask, examples
    pack = _run(tmp_path, 7)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    ont = Ontology(load_pack(PACK), Tracer(None))
    try:
        exs = examples(pack, ont)
        assert exs
        for ex in exs:
            a = ask(pack, ont, ex["question"])
            assert a["status"] == "answered", (ex, a.get("reason"))
    finally:
        ont.store.con.close()
