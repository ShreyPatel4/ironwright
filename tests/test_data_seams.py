"""Records that are not data: header, trailer and footer lines (R1 F-04, issue 25).

The profiler finds them and proposes a skip with the count. The human decides on Map.
The reader honours that decision, and quality flags what was skipped and what was left in.
Each test runs the real reader and, where it says so, a real run into a real lake.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from ironwright import config
from ironwright import workspaces_tenant as wt
from ironwright.ingest.profiler import profile_files
from ironwright.ingest.propose import propose
from ironwright.lake import Lakehouse
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import DEV_ORG_ID

CSV_ROWS = [f"C-{100 + i},P{i},{10 + i}.50,Ann{i},x,y,z" for i in range(9)]
CSV_NAMES = "claim_id,policy,amount,holder,a,b,c"
CSV_FILE = "\n".join([CSV_NAMES, *CSV_ROWS, "TOTAL ROWS: 9,,,,,,"]) + "\n"
FIXED_ROWS = [f"{1000 + i}Holder{i}    {100 + i:06d}" for i in range(6)]
FIXED_FILE = "\n".join([*FIXED_ROWS, "END OF FILE  RECORDS=000006".ljust(24)]) + "\n"
CSV_COLUMNS = [{"name": "claim_id", "type": "string", "key": True}, {"name": "amount", "type": "decimal(12,2)"}]
SKIP_FOOTER = {"skip_header": 0, "skip_footer": 1}


@pytest.fixture()
def root(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(d))
    return d.resolve()


def put_upload(data: str, name: str) -> str:
    upload_id = uuid.uuid4().hex
    d = config.uploads_root(DEV_ORG_ID) / upload_id
    d.mkdir(parents=True)
    (d / name).write_text(data)
    return f"{upload_id}/{name}"


def csv_spec(ref: str) -> dict:
    return {"kind": "workspace", "version": 1, "title": "Seams", "reconcile": [], "quality": [],
            "sources": [{"name": "claims", "upload": ref, "format": "csv",
                         "layout": {"delimiter": ",", "header": True, "encoding": "utf-8"}, "columns": CSV_COLUMNS}]}


def fixed_spec(ref: str) -> dict:
    fields = [{"name": "policy_no", "start": 0, "width": 4}, {"name": "holder", "start": 4, "width": 11},
              {"name": "amount", "start": 15, "width": 6}]
    return {"kind": "workspace", "version": 1, "title": "Seams", "reconcile": [], "quality": [],
            "sources": [{"name": "policies", "upload": ref, "format": "fixed_width",
                         "layout": {"encoding": "utf-8", "fields": fields},
                         "columns": [{"name": "policy_no", "type": "string", "key": True},
                                     {"name": "holder", "type": "string"}, {"name": "amount", "type": "string"}]}]}


def proposed(name: str, data: str) -> dict:
    return propose(profile_files([(name, data.encode())]), "up")


def run_csv(data: str = CSV_FILE, records: dict | None = None) -> dict:
    ref = put_upload(data, "claims.csv")
    wt.create_workspace(DEV_ORG_ID, "seams", csv_spec(ref), records=records)
    wt.run_workspace(DEV_ORG_ID, "seams")
    return json.loads((wt.load_workspace(DEV_ORG_ID, "seams").paths.reports / "migrate.json").read_text())


def bronze_rows(table: str = "claims") -> list[dict]:
    return Lakehouse(wt.load_workspace(DEV_ORG_ID, "seams")).read(f"bronze.{table}").to_pylist()


def checks(report: dict, table: str = "bronze.claims") -> dict[str, dict]:
    write = next(w for w in report["writes"] if w["table"] == table)
    return {c["check"]: c for c in write["checks"]}


# ---------------------------------------------------------------- the profiler proposes

def test_a_csv_total_row_is_proposed_as_a_skip_with_its_count():
    out = proposed("claims.csv", CSV_FILE)
    assert out["records"] == {"claims": SKIP_FOOTER}
    d = next(d for d in out["decisions"] if d["path"] == "sources[0].layout.skip_footer")
    assert d["value"] == 1


def test_a_fixed_width_end_of_file_line_is_proposed_as_a_skip():
    out = proposed("policies.dat", FIXED_FILE)
    assert out["records"] == {"policies": SKIP_FOOTER}


def test_the_profile_reads_the_count_the_trailer_declares():
    assert profile_files([("claims.csv", CSV_FILE.encode())])["files"][0]["declared_rows"] == 9
    assert profile_files([("policies.dat", FIXED_FILE.encode())])["files"][0]["declared_rows"] == 6


def test_a_date_on_a_trailer_is_not_read_as_a_count():
    data = "\n".join([CSV_NAMES, *CSV_ROWS, "END OF EXTRACT 2026-11-05,,,,,,"]) + "\n"
    assert profile_files([("claims.csv", data.encode())])["files"][0]["declared_rows"] is None


def test_a_csv_preamble_line_is_proposed_as_a_header_record():
    data = "\n".join(["REPORT RUN 2026-11-05", CSV_NAMES, *CSV_ROWS]) + "\n"
    f = profile_files([("claims.csv", data.encode())])["files"][0]
    assert f["leader_rows"] == 1
    assert f["rows"] == 9
    assert f["columns"][0]["name"] == "claim_id"
    assert propose({"files": [f], "candidate_joins": [], "skipped": []}, "up")["records"]["claims"]["skip_header"] == 1


def test_a_column_called_header_is_still_the_names_row():
    data = "HEADER,policy,amount\nH1,P1,10.50\nH2,P2,11.50\nH3,P3,12.50\n"
    f = profile_files([("claims.csv", data.encode())])["files"][0]
    assert f["leader_rows"] == 0
    assert f["columns"][0]["name"] == "header"


def test_a_fixed_width_names_row_counts_as_a_header_record():
    rows = [f"{1000 + i} Holder{i}  {100 + i:06d}" for i in range(6)]
    data = "\n".join(["ITEM HOLDER   AMOUNT", *rows]) + "\n"
    f = profile_files([("policies.dat", data.encode())])["files"][0]
    assert f["layout"]["header"] is True
    assert propose({"files": [f], "candidate_joins": [], "skipped": []}, "up")["records"]["policies"]["skip_header"] == 1


def test_a_clean_file_proposes_no_skip():
    out = proposed("claims.csv", "\n".join([CSV_NAMES, *CSV_ROWS]) + "\n")
    assert out["records"] == {"claims": {"skip_header": 0, "skip_footer": 0}}


def test_a_report_never_carries_trailer_text():
    data = CSV_FILE.replace("TOTAL ROWS: 9", "TOTAL Zzcanary: 9")
    out = proposed("claims.csv", data)
    assert "Zzcanary" not in json.dumps(out["decisions"]) and "Zzcanary" not in json.dumps(out["records"])


def test_the_draft_workspace_stays_contract_3():
    """The contract is fixed, so the skip never lands in workspace.yaml."""
    source = proposed("claims.csv", CSV_FILE)["workspace"]["sources"][0]
    assert set(source["layout"]) == {"delimiter", "header", "encoding"}


# ---------------------------------------------------------------- the reader honours it

def test_a_skipped_csv_total_row_never_reaches_bronze(root):
    run_csv(records={"claims": SKIP_FOOTER})
    rows = bronze_rows()
    assert len(rows) == 9
    assert not [r for r in rows if r["claim_id"].startswith("TOTAL")]


def test_a_skipped_fixed_width_trailer_never_reaches_bronze(root):
    ref = put_upload(FIXED_FILE, "policies.dat")
    wt.create_workspace(DEV_ORG_ID, "seams", fixed_spec(ref), records={"policies": SKIP_FOOTER})
    wt.run_workspace(DEV_ORG_ID, "seams")
    rows = bronze_rows("policies")
    assert len(rows) == 6
    assert not [r for r in rows if r["policy_no"].startswith("END")]


def test_a_skipped_preamble_leaves_the_names_row_as_the_names_row(root):
    data = "\n".join(["REPORT RUN 2026-11-05", CSV_NAMES, *CSV_ROWS]) + "\n"
    run_csv(data, records={"claims": {"skip_header": 1, "skip_footer": 0}})
    rows = bronze_rows()
    assert len(rows) == 9
    assert rows[0]["claim_id"] == "C-100"


def test_a_skipped_fixed_width_names_row_never_reaches_bronze(root):
    ref = put_upload("\n".join(["POLI HOLDER     AMOUNT", *FIXED_ROWS]) + "\n", "policies.dat")
    wt.create_workspace(DEV_ORG_ID, "seams", fixed_spec(ref), records={"policies": {"skip_header": 1, "skip_footer": 0}})
    wt.run_workspace(DEV_ORG_ID, "seams")
    assert [r["policy_no"] for r in bronze_rows("policies")] == [str(1000 + i) for i in range(6)]


def test_a_stale_header_decision_keeps_the_first_record(root):
    ref = put_upload("\n".join(FIXED_ROWS) + "\n", "policies.dat")
    wt.create_workspace(DEV_ORG_ID, "seams", fixed_spec(ref), records={"policies": {"skip_header": 1, "skip_footer": 0}})
    wt.run_workspace(DEV_ORG_ID, "seams")
    assert len(bronze_rows("policies")) == 6


def test_declining_the_skip_keeps_the_row_in_bronze(root):
    run_csv(records={"claims": {"skip_header": 0, "skip_footer": 0}})
    assert len(bronze_rows()) == 10


def test_the_skip_lands_beside_the_spec_and_not_in_it(root):
    run_csv(records={"claims": SKIP_FOOTER})
    got = wt.read_workspace(DEV_ORG_ID, "seams")
    assert "skip_footer" not in json.dumps(got["spec"])
    assert got["records"] == {"claims": SKIP_FOOTER}


def test_the_next_extract_without_a_trailer_keeps_its_last_row(root):
    """A blind drop of the last line would lose a real record on every refresh."""
    run_csv(records={"claims": SKIP_FOOTER})
    more = "\n".join([CSV_NAMES, *CSV_ROWS]) + "\n"
    wt.add_source_version(DEV_ORG_ID, "seams", "claims", put_upload(more, "claims.csv"))
    report = wt.run_workspace(DEV_ORG_ID, "seams")
    assert report["passed"], report
    rows = wt.read_source_version(DEV_ORG_ID, "seams", "claims", 2).to_pylist()
    assert len(rows) == 9
    assert rows[-1]["claim_id"] == "C-108"


def test_a_full_rebuild_keeps_the_skip_on_older_versions(root):
    run_csv(records={"claims": SKIP_FOOTER})
    wt.add_source_version(DEV_ORG_ID, "seams", "claims", put_upload(CSV_FILE.replace("C-100", "C-900"), "claims.csv"))
    wt.run_workspace(DEV_ORG_ID, "seams", mode="full")
    assert len(wt.read_source_version(DEV_ORG_ID, "seams", "claims", 1)) == 9


@pytest.mark.parametrize("records, says", [
    ({"nope": SKIP_FOOTER}, "no source nope"),
    ({"claims": {"skip_footer": -1}}, "skip_footer"),
    ({"claims": {"skip_footer": 99}}, "skip_footer"),
    ({"claims": {"skip_footer": True}}, "skip_footer"),
    ({"claims": {"drop_everything": 1}}, "drop_everything"),
])
def test_a_bad_skip_is_refused_before_anything_is_written(root, records, says):
    ref = put_upload(CSV_FILE, "claims.csv")
    with pytest.raises(wt.WorkspaceError, match=says):
        wt.create_workspace(DEV_ORG_ID, "seams", csv_spec(ref), records=records)
    assert wt.list_workspaces(DEV_ORG_ID) == []


def test_the_create_route_takes_the_decision(root, meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    ref = put_upload(CSV_FILE, "claims.csv")
    upload_id = ref.split("/")[0]
    (config.uploads_root(DEV_ORG_ID) / upload_id / "manifest.json").write_text(
        json.dumps({"id": upload_id, "files": [{"name": "claims.csv", "bytes": len(CSV_FILE)}]}))
    with TestClient(create_platform_app()) as c:
        r = c.post("/workspaces", json={"from_upload": upload_id, "slug": "seams", "spec": csv_spec(ref),
                                        "records": {"claims": SKIP_FOOTER}})
        assert r.status_code == 200 or r.status_code == 201, r.text
        bad = c.post("/workspaces", json={"from_upload": upload_id, "slug": "seams2", "spec": csv_spec(ref),
                                          "records": {"nope": SKIP_FOOTER}})
        assert bad.status_code == 422
    assert wt.read_workspace(DEV_ORG_ID, "seams")["records"] == {"claims": SKIP_FOOTER}


# ---------------------------------------------------------------- quality flags them

def test_a_skipped_record_is_flagged_with_its_count(root):
    got = checks(run_csv(records={"claims": SKIP_FOOTER}))["skipped_records"]
    assert (got["passed"], got["severity"], got["failing_rows"]) == (False, "warn", 1)


def test_a_trailer_left_in_is_flagged_not_silent(root):
    """R1 F-04: a trailer in silver and `0 flagged`. This is the honest answer."""
    got = checks(run_csv())["trailer_left_in"]
    assert (got["passed"], got["severity"], got["failing_rows"]) == (False, "warn", 1)


def test_the_run_headline_counts_the_flag(root):
    run_csv()
    evals = json.loads((wt.load_workspace(DEV_ORG_ID, "seams").paths.reports / "evals.json").read_text())
    pipeline = next(la for la in evals["layers"] if la["layer"] == "pipeline")
    assert "0 flagged" not in pipeline["headline"]


def test_a_matching_trailer_count_passes(root):
    got = checks(run_csv(records={"claims": SKIP_FOOTER}))["trailer_count"]
    assert got["passed"] and got["failing_rows"] == 0


def test_a_trailer_count_the_file_does_not_meet_is_flagged(root):
    """A system that says 12 and sends 9 lost three records somewhere."""
    data = CSV_FILE.replace("TOTAL ROWS: 9", "TOTAL ROWS: 12")
    got = checks(run_csv(data, records={"claims": SKIP_FOOTER}))["trailer_count"]
    assert (got["passed"], got["severity"], got["failing_rows"]) == (False, "warn", 3)
    assert "12" in got["detail"] and "9" in got["detail"]


def test_a_count_that_includes_the_records_around_the_data_also_matches(root):
    """Plenty of mainframes count the trailer as a record."""
    data = CSV_FILE.replace("TOTAL ROWS: 9", "TOTAL ROWS: 10")
    assert checks(run_csv(data, records={"claims": SKIP_FOOTER}))["trailer_count"]["passed"]


def test_a_clean_file_adds_no_flags(root):
    got = checks(run_csv("\n".join([CSV_NAMES, *CSV_ROWS]) + "\n"))
    assert not {"skipped_records", "trailer_left_in", "trailer_count"} & set(got)


def test_a_flag_never_quotes_the_record(root):
    data = CSV_FILE.replace("TOTAL ROWS: 9", "TOTAL Zzcanary ROWS: 9")
    assert "Zzcanary" not in json.dumps(checks(run_csv(data, records={"claims": SKIP_FOOTER})))
