"""Data refresh: the next extract becomes version N+1 of a source.

Every drift kind runs on CSV and on fixed width, on real runs. The T2 fixture
is the acceptance case: its file already has a trailing comma column, a duplicate
key and an amount that does not cast, and none of that may read as drift.
"""
from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ironwright import config
from ironwright import workspaces_tenant as wt
from ironwright.ingest import drift
from ironwright.jobs import handlers
from ironwright.lake import Lakehouse
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import DEV_ORG_ID

FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws"
OTHER_ORG = "b" * 32
PASSWORD = "correct-horse-9"
SLUG = "refresh"
WIDTHS = {"order_id": 6, "customer": 10, "amount": 8}
COLUMNS = ["order_id", "customer", "amount"]
FORMATS = ["csv", "fixed_width"]
# Cell text that must never reach a drift report. Reports travel back to the UI.
CANARY = "Zzcanary"


@pytest.fixture()
def root(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(d))
    return d.resolve()


def base_rows(n: int = 6) -> list[dict[str, str]]:
    return [{"order_id": str(1000 + i), "customer": f"{CANARY}{i}", "amount": f"{10 + i}.50"} for i in range(n)]


def render(fmt: str, rows: list[dict[str, str]], columns: list[str] | None = None) -> bytes:
    """A file as that system would send it. Fixed width has no names, so a dropped
    column is blanks at its old offset and an added one is bytes past the layout."""
    columns = columns if columns is not None else COLUMNS
    if fmt == "csv":
        lines = [",".join(columns)] + [",".join(r.get(c, "") for c in columns) for r in rows]
    else:
        extra = [c for c in columns if c not in WIDTHS]
        lines = ["".join((r.get(c, "") if c in columns else "").ljust(w) for c, w in WIDTHS.items())
                 + "".join(r.get(c, "").ljust(8) for c in extra) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def put_upload(data: bytes, name: str = "orders.dat", org: str = DEV_ORG_ID) -> str:
    upload_id = uuid.uuid4().hex
    d = config.uploads_root(org) / upload_id
    d.mkdir(parents=True)
    (d / name).write_bytes(data)
    return f"{upload_id}/{name}"


def spec_for(fmt: str, ref: str) -> dict:
    columns = [{"name": "order_id", "type": "int", "key": True}, {"name": "customer", "type": "string"},
               {"name": "amount", "type": "decimal(12,2)"}]
    if fmt == "csv":
        layout = {"delimiter": ",", "header": True, "encoding": "utf-8"}
    else:
        start, fields = 0, []
        for name, width in WIDTHS.items():
            fields.append({"name": name, "start": start, "width": width})
            start += width
        layout = {"fields": fields, "encoding": "utf-8"}
    return {"kind": "workspace", "version": 1, "title": "Orders", "reconcile": [],
            "quality": [],
            "sources": [{"name": "orders", "upload": ref, "format": fmt, "layout": layout, "columns": columns}]}


SILVER = {"silver": {"01_orders.sql": "select order_id, customer, amount from bronze_orders"}}


def make(fmt: str, rows: list[dict[str, str]] | None = None, *, models=None, ontology=None, policies=None) -> str:
    ref = put_upload(render(fmt, rows if rows is not None else base_rows()))
    wt.create_workspace(DEV_ORG_ID, SLUG, spec_for(fmt, ref), models=SILVER if models is None else models,
                        ontology=ontology, policies=policies)
    return ref


def add(fmt: str, rows: list[dict[str, str]], columns: list[str] | None = None) -> dict:
    return wt.add_source_version(DEV_ORG_ID, SLUG, "orders", put_upload(render(fmt, rows, columns)))


def kinds(record: dict) -> dict[str, dict]:
    return {f"{i['kind']}:{i.get('column', '')}": i for i in record["drift"]["items"]}


def bronze_all():
    con = Lakehouse(wt.load_workspace(DEV_ORG_ID, SLUG)).connect()
    return con


def run_ids() -> list[str]:
    rows = bronze_all().execute("select distinct _run_id from bronze_orders_all order by 1").fetchall()
    return [r[0] for r in rows]


# ------------------------------------------------------------- source versions

def test_the_first_upload_is_version_one_without_anyone_asking(root):
    ref = make("csv")
    assert [(v["version"], v["upload"]) for v in wt.source_versions(DEV_ORG_ID, SLUG, "orders")] == [(1, ref)]


def test_a_new_upload_is_n_plus_one_and_older_versions_stay_listed(root):
    first = make("csv")
    second = add("csv", base_rows())
    third = add("csv", base_rows())
    assert (second["version"], third["version"]) == (2, 3)
    listed = wt.source_versions(DEV_ORG_ID, SLUG, "orders")
    assert [v["version"] for v in listed] == [1, 2, 3]
    assert listed[0]["upload"] == first
    assert (config.uploads_root(DEV_ORG_ID) / first).is_file()


def test_the_workspace_loads_the_latest_version(root):
    make("csv")
    second = add("csv", base_rows())
    assert wt.load_workspace(DEV_ORG_ID, SLUG).spec["sources"][0]["upload"] == second["upload"]


def test_workspace_yaml_stays_inside_contract_3(root):
    make("csv")
    add("csv", base_rows())
    spec = yaml.safe_load((config.workspace_dir(DEV_ORG_ID, SLUG) / "workspace.yaml").read_text())
    assert set(spec["sources"][0]) == {"name", "upload", "format", "layout", "columns"}


def test_sending_the_same_upload_twice_does_not_make_a_new_version(root):
    make("csv")
    ref = put_upload(render("csv", base_rows()))
    assert wt.add_source_version(DEV_ORG_ID, SLUG, "orders", ref)["version"] == 2
    assert wt.add_source_version(DEV_ORG_ID, SLUG, "orders", ref)["version"] == 2
    assert len(wt.source_versions(DEV_ORG_ID, SLUG, "orders")) == 2


def test_two_uploads_at_once_take_two_numbers_and_the_highest_wins(root):
    make("csv")
    refs = [put_upload(render("csv", base_rows())) for _ in range(8)]
    threads = [threading.Thread(target=wt.add_source_version, args=(DEV_ORG_ID, SLUG, "orders", r)) for r in refs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    listed = wt.source_versions(DEV_ORG_ID, SLUG, "orders")
    assert [v["version"] for v in listed] == list(range(1, 10))
    assert wt.load_workspace(DEV_ORG_ID, SLUG).spec["sources"][0]["upload"] == listed[-1]["upload"]


def test_a_source_the_workspace_does_not_have_is_refused(root):
    make("csv")
    with pytest.raises(wt.WorkspaceError, match="no source"):
        wt.add_source_version(DEV_ORG_ID, SLUG, "ghost", put_upload(render("csv", base_rows())))


def test_another_orgs_upload_reads_exactly_like_one_nobody_has(root):
    make("csv")
    foreign = put_upload(render("csv", base_rows()), org=OTHER_ORG)
    ghost = f"{uuid.uuid4().hex}/orders.dat"
    with pytest.raises(wt.WorkspaceError) as a:
        wt.add_source_version(DEV_ORG_ID, SLUG, "orders", foreign)
    with pytest.raises(wt.WorkspaceError) as b:
        wt.add_source_version(DEV_ORG_ID, SLUG, "orders", ghost)
    assert str(a.value).replace(foreign, "X") == str(b.value).replace(ghost, "X")
    assert len(wt.source_versions(DEV_ORG_ID, SLUG, "orders")) == 1


def test_a_workspace_of_another_org_is_not_found(root):
    make("csv")
    with pytest.raises(FileNotFoundError):
        wt.source_versions(OTHER_ORG, SLUG, "orders")


# ---------------------------------------------------------------- each drift kind

@pytest.mark.parametrize("fmt", FORMATS)
def test_identical_data_has_no_drift(root, fmt):
    make(fmt)
    report = add(fmt, base_rows())["drift"]
    assert report["items"] == [] and report["severity"] == "none" and report["blocking"] is False
    assert report["against_version"] == 1


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_new_column_is_recorded_and_runs(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    rows = [{**r, "region": "EAST"} for r in base_rows()]
    record = add(fmt, rows, [*COLUMNS, "region"])
    items = [i for i in record["drift"]["items"] if i["kind"] == "new_column"]
    assert len(items) == 1 and items[0]["severity"] == "info"
    assert record["drift"]["blocking"] is False
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_missing_plain_column_is_a_warning_and_the_run_still_runs(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add(fmt, base_rows(), ["order_id", "amount"])
    item = kinds(record)["missing_column:customer"]
    assert item["severity"] == "warn" and record["drift"]["blocking"] is False
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True
    got = bronze_all().execute("select count(*), count(customer) from silver_orders").fetchone()
    assert got == (6, 0 if fmt == "csv" else got[1])


def test_a_warned_version_can_run_again(root):
    """The run loads a narrower shape. The next run still has to know why."""
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["order_id", "amount"])
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


def test_a_workspace_that_ran_before_versions_existed_keeps_that_run_on_v1(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    shutil.rmtree(config.workspace_dir(DEV_ORG_ID, SLUG) / wt.VERSIONS_DIR)
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "orders", 1).num_rows == 6


def test_a_missing_plain_column_with_nothing_in_bronze_stops_the_run(root):
    """The silver model reads that column. With no earlier run the table has never
    held it, so a warning here would be a promise the run cannot keep."""
    make("csv")
    record = add("csv", base_rows(), ["order_id", "amount"])
    assert kinds(record)["missing_column:customer"]["severity"] == "block"
    with pytest.raises(wt.DriftBlocked):
        wt.run_workspace(DEV_ORG_ID, SLUG)


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_missing_key_column_stops_the_run_and_asks_for_a_spec_update(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    before = run_ids()
    record = add(fmt, base_rows(), ["customer", "amount"])
    item = kinds(record)["missing_column:order_id"]
    assert item["severity"] == "block" and record["drift"]["blocking"] is True
    with pytest.raises(wt.DriftBlocked, match="spec"):
        wt.run_workspace(DEV_ORG_ID, SLUG)
    assert run_ids() == before


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_type_change_on_a_plain_column_is_a_warning(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    rows = [{**r, "amount": "TBD"} for r in base_rows()]
    record = add(fmt, rows)
    item = kinds(record)["type_change:amount"]
    assert item["severity"] == "warn" and item["declared"] == "decimal(12,2)" and record["drift"]["blocking"] is False
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_type_change_on_a_key_stops_the_run(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    rows = [{**r, "order_id": f"A-{i}"} for i, r in enumerate(base_rows())]
    record = add(fmt, rows)
    assert kinds(record)["type_change:order_id"]["severity"] == "block"
    with pytest.raises(wt.DriftBlocked):
        wt.run_workspace(DEV_ORG_ID, SLUG)


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_key_that_stops_being_unique_is_a_warning_with_a_count(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    rows = base_rows()
    rows[1] = {**rows[1], "order_id": rows[0]["order_id"]}
    record = add(fmt, rows)
    item = kinds(record)["key_uniqueness_loss:order_id"]
    assert item["severity"] == "warn" and item["duplicates"] == 1
    assert record["drift"]["blocking"] is False
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_big_row_count_swing_is_a_warning(root, fmt):
    make(fmt, base_rows(20))
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add(fmt, base_rows(4))
    item = kinds(record)["row_count_swing:"]
    assert item["severity"] == "warn" and (item["from"], item["to"]) == (20, 4)
    assert record["drift"]["blocking"] is False


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_small_row_count_change_is_not_drift(root, fmt):
    make(fmt, base_rows(20))
    assert "row_count_swing:" not in kinds(add(fmt, base_rows(24)))


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_report_is_a_profile_diff(root, fmt):
    make(fmt)
    record = add(fmt, [{**r, "region": "EAST"} for r in base_rows(9)], [*COLUMNS, "region"])
    diff = record["drift"]["profile_diff"]
    assert diff["rows"] == {"from": 6, "to": 9}
    assert len(diff["columns"]["added"]) == 1 and diff["columns"]["removed"] == []


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_report_never_carries_a_cell(root, fmt):
    make(fmt)
    rows = [{**r, "amount": f"{CANARY}amt", "order_id": f"{CANARY}{i}"} for i, r in enumerate(base_rows())]
    assert CANARY not in json.dumps(add(fmt, rows))


def test_a_fixed_width_trailer_record_is_not_a_type_change(root):
    make("fixed_width")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    data = render("fixed_width", base_rows()) + b"TRL00000006\n"
    record = wt.add_source_version(DEV_ORG_ID, SLUG, "orders", put_upload(data))
    assert record["drift"]["items"] == []


def test_a_file_the_spec_cannot_read_is_blocking_not_a_crash(root):
    make("csv")
    record = wt.add_source_version(DEV_ORG_ID, SLUG, "orders", put_upload(b"\xff\xfe\xfa not utf-8\n"))
    assert kinds(record)["unreadable:"]["severity"] == "block"
    with pytest.raises(wt.DriftBlocked):
        wt.run_workspace(DEV_ORG_ID, SLUG)


def test_a_file_with_none_of_the_declared_columns_blocks(root):
    """No key to lose, so the key rule alone would wave this through."""
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    spec_columns = [{"name": "customer", "type": "string"}, {"name": "amount", "type": "decimal(12,2)"},
                    {"name": "order_id", "type": "int"}]
    wt.update_source_spec(DEV_ORG_ID, SLUG, "orders", columns=spec_columns)
    record = wt.add_source_version(DEV_ORG_ID, SLUG, "orders", put_upload(b"a,b\n1,2\n3,4\n"))
    assert record["drift"]["blocking"] is True


# ------------------------------------------------------------- blocked, then unblocked

@pytest.mark.parametrize("fmt", FORMATS)
def test_a_spec_update_unblocks_the_run(root, fmt):
    make(fmt)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add(fmt, [{**r, "order_id": f"A-{i}"} for i, r in enumerate(base_rows())])
    columns = [{"name": "order_id", "type": "string", "key": True}, {"name": "customer", "type": "string"},
               {"name": "amount", "type": "decimal(12,2)"}]
    record = wt.update_source_spec(DEV_ORG_ID, SLUG, "orders", columns=columns)
    assert record["drift"]["blocking"] is False
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


def test_a_spec_update_that_fixes_nothing_stays_blocked(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["customer", "amount"])
    columns = yaml.safe_load((config.workspace_dir(DEV_ORG_ID, SLUG) / "workspace.yaml").read_text())["sources"][0]["columns"]
    assert wt.update_source_spec(DEV_ORG_ID, SLUG, "orders", columns=columns)["drift"]["blocking"] is True
    with pytest.raises(wt.DriftBlocked):
        wt.run_workspace(DEV_ORG_ID, SLUG)


def test_a_bad_spec_update_changes_nothing(root):
    make("csv")
    path = config.workspace_dir(DEV_ORG_ID, SLUG) / "workspace.yaml"
    before = path.read_text()
    with pytest.raises(wt.WorkspaceError):
        wt.update_source_spec(DEV_ORG_ID, SLUG, "orders", columns=[{"name": "X; drop", "type": "string"}])
    assert path.read_text() == before


def test_a_corrected_file_unblocks_the_run_too(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["customer", "amount"])
    fixed = add("csv", base_rows())
    assert fixed["drift"]["blocking"] is False and fixed["drift"]["against_version"] == 1
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True


def test_the_job_handler_turns_blocking_drift_into_a_failure_the_owner_can_read(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["customer", "amount"])
    ctx = handlers.JobCtx(DEV_ORG_ID, "user:x", "job", lambda: False)
    with pytest.raises(handlers.JobError, match=r"(?s)order_id.*spec"):
        handlers.run(ctx, SLUG, {}, lambda _: None)


# ---------------------------------------------------------------- run modes and history

def test_acceptance_t2_fixture_with_an_added_column_runs_green_and_v1_stays_queryable(root):
    """The fixture has a trailing comma column, a duplicated key and amounts that do
    not cast. Those were accepted with v1, so the only drift is the new column."""
    dest = config.uploads_root(DEV_ORG_ID) / "fixture"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / "uploads" / "fixture", dest)
    wt.create_workspace(DEV_ORG_ID, SLUG, yaml.safe_load((FIXTURE / "workspace.yaml").read_text()),
                        models={layer: {f.name: f.read_text() for f in (FIXTURE / "models" / layer).glob("*.sql")}
                                for layer in ("silver", "gold")},
                        ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    assert wt.run_workspace(DEV_ORG_ID, SLUG)["passed"] is True

    lines = (FIXTURE / "uploads" / "fixture" / "claims.csv").read_bytes().decode().splitlines()
    grown = [lines[0] + "Region", *[ln + "EAST" if ln.strip() and "late note" not in ln else ln for ln in lines[1:]]]
    ref = put_upload(("\n".join(grown) + "\n").encode(), "claims.csv")
    record = wt.add_source_version(DEV_ORG_ID, SLUG, "claims", ref)
    assert record["version"] == 2
    assert [(i["kind"], i["column"]) for i in record["drift"]["items"]] == [("new_column", "region")]

    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    assert report["passed"] is True, report["layers"]

    versions = wt.source_versions(DEV_ORG_ID, SLUG, "claims")
    assert [v["version"] for v in versions] == [1, 2]
    v1, v2 = versions[0]["runs"][-1], versions[1]["runs"][-1]
    assert v1["run_id"] != v2["run_id"]

    con = Lakehouse(wt.load_workspace(DEV_ORG_ID, SLUG)).connect()
    assert con.execute(f"select count(*), count(region) from bronze_claims_all where _run_id = '{v1['run_id']}'").fetchone() == (6, 0)
    assert con.execute("select count(*), count(region) from bronze_claims").fetchone()[1] >= 5
    v1_rows = wt.read_source_version(DEV_ORG_ID, SLUG, "claims", 1)
    assert v1_rows.num_rows == 6 and set(v1_rows.column("_run_id").to_pylist()) == {v1["run_id"]}
    assert (config.uploads_root(DEV_ORG_ID) / "fixture" / "claims.csv").is_file()


def test_two_runs_in_the_same_second_keep_apart(root, monkeypatch):
    monkeypatch.setattr(wt.time, "strftime", lambda *_: "20261104120000")
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(4))
    wt.run_workspace(DEV_ORG_ID, SLUG)
    a, b = [v["runs"][-1]["run_id"] for v in wt.source_versions(DEV_ORG_ID, SLUG, "orders")]
    assert a != b and b > a
    assert bronze_all().execute("select count(*) from bronze_orders").fetchone() == (4,)


def test_an_upload_during_a_run_is_not_credited_with_that_run(root, monkeypatch):
    """A run can take minutes, and the next extract does not wait for it."""
    import ironwright.lake as lake_mod
    real = lake_mod.migrate
    make("csv")

    def slow(*args, **kw):
        out = real(*args, **kw)
        add("csv", base_rows(4))
        return out

    monkeypatch.setattr(lake_mod, "migrate", slow)
    wt.run_workspace(DEV_ORG_ID, SLUG)
    v1, v2 = wt.source_versions(DEV_ORG_ID, SLUG, "orders")
    assert len(v1["runs"]) == 1 and v2["runs"] == []
    monkeypatch.setattr(lake_mod, "migrate", real)
    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    got = {la["layer"]: la for la in report["layers"]}["version_reconcile"]["metrics"]["sources"]["orders"]
    assert (got["from_version"], got["to_version"]) == (1, 2)


def test_append_is_the_default_and_keeps_every_run(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(4))
    wt.run_workspace(DEV_ORG_ID, SLUG)
    assert bronze_all().execute("select count(*) from bronze_orders_all").fetchone() == (10,)
    assert bronze_all().execute("select count(*) from bronze_orders").fetchone() == (4,)


def test_a_full_rebuild_replays_every_loaded_version_from_its_kept_bytes(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    wt.run_workspace(DEV_ORG_ID, SLUG)  # one version, two runs: the replay holds it once
    add("csv", base_rows(4))
    wt.run_workspace(DEV_ORG_ID, SLUG)
    report = wt.run_workspace(DEV_ORG_ID, SLUG, mode="full")
    assert report["passed"] is True, report["layers"]
    assert bronze_all().execute("select count(*) from bronze_orders_all").fetchone() == (10,)
    assert bronze_all().execute("select count(*) from bronze_orders").fetchone() == (4,)
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "orders", 1).num_rows == 6
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "orders", 2).num_rows == 4


def test_a_full_rebuild_reads_old_versions_with_the_spec_they_loaded_under(root):
    """A renamed key must not make the old file unreadable on replay."""
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["customer", "amount", "order_no"])
    columns = [{"name": "order_no", "type": "string", "key": True}, {"name": "customer", "type": "string"},
               {"name": "amount", "type": "decimal(12,2)"}]
    wt.update_source_spec(DEV_ORG_ID, SLUG, "orders", columns=columns)
    report = wt.run_workspace(DEV_ORG_ID, SLUG, mode="full")
    assert report["passed"] is True, report["layers"]
    assert wt.read_source_version(DEV_ORG_ID, SLUG, "orders", 1).num_rows == 6


def test_a_full_rebuild_that_cannot_replay_leaves_bronze_alone(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(4))
    wt.run_workspace(DEV_ORG_ID, SLUG)
    v1 = wt.source_versions(DEV_ORG_ID, SLUG, "orders")[0]["upload"]
    (config.uploads_root(DEV_ORG_ID) / v1).unlink()
    with pytest.raises(wt.WorkspaceError):
        wt.run_workspace(DEV_ORG_ID, SLUG, mode="full")
    assert bronze_all().execute("select count(*) from bronze_orders_all").fetchone() == (10,)


def test_a_blocked_version_is_never_replayed(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    add("csv", base_rows(), ["customer", "amount"])
    add("csv", base_rows(5))
    assert wt.run_workspace(DEV_ORG_ID, SLUG, mode="full")["passed"] is True
    assert bronze_all().execute("select count(*) from bronze_orders_all").fetchone() == (11,)


def test_an_unknown_mode_is_refused_at_the_door_and_by_the_run(root):
    make("csv")
    with pytest.raises(ValueError, match="mode"):
        handlers.validate_params("run", {"mode": "sideways"})
    with pytest.raises(wt.WorkspaceError, match="mode"):
        wt.run_workspace(DEV_ORG_ID, SLUG, mode="sideways")
    handlers.validate_params("run", {"mode": "full"})
    handlers.validate_params("run", {"mode": "append"})


def test_the_job_passes_the_mode_through(root):
    make("csv")
    ctx = handlers.JobCtx(DEV_ORG_ID, "user:x", "job", lambda: False)
    got = handlers.run(ctx, SLUG, {"mode": "append"}, lambda _: None)
    assert got["mode"] == "append" and got["evals"]["passed"] is True
    add("csv", base_rows(4))
    got = handlers.run(ctx, SLUG, {"mode": "full"}, lambda _: None)
    assert got["mode"] == "full" and got["evals"]["passed"] is True
    assert bronze_all().execute("select count(*) from bronze_orders_all").fetchone() == (10,)


def test_a_demo_pack_has_no_run_modes(root):
    ctx = handlers.JobCtx(DEV_ORG_ID, "user:x", "job", lambda: False)
    with pytest.raises(handlers.JobError, match="mode"):
        handlers.run(ctx, "retail", {"mode": "full"}, lambda _: None)


# ------------------------------------------------------------- version reconciliation

def test_reconciliation_compares_the_new_version_to_the_last(root):
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    rows = base_rows(5)
    rows.append({"order_id": "2000", "customer": "New", "amount": "100.00"})
    rows.append({"order_id": "2001", "customer": "New", "amount": "100.00"})
    add("csv", rows)
    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    layers = {la["layer"]: la for la in report["layers"]}
    assert layers["version_reconcile"]["passed"] is True
    got = layers["version_reconcile"]["metrics"]["sources"]["orders"]
    assert (got["from_version"], got["to_version"]) == (1, 2)
    assert got["rows"] == {"from": 6, "to": 7}
    assert got["keys"] == {"key": "order_id", "added": 2, "removed": 1, "kept": 5}
    old_total, new_total = sum(10.5 + i for i in range(6)), sum(10.5 + i for i in range(5)) + 200
    assert got["measures"]["amount"] == {"from": old_total, "to": new_total}


def test_a_version_reconcile_fails_when_history_went_missing(root):
    """The one thing this layer promises: every older run still holds its rows."""
    make("csv")
    wt.run_workspace(DEV_ORG_ID, SLUG)
    v1 = wt.source_versions(DEV_ORG_ID, SLUG, "orders")[0]
    path = config.workspace_dir(DEV_ORG_ID, SLUG) / wt.VERSIONS_DIR / "orders" / "0001.json"
    path.write_text(json.dumps({**v1, "runs": [{**v1["runs"][-1], "rows": 999}]}))
    add("csv", base_rows(4))
    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    layers = {la["layer"]: la for la in report["layers"]}
    assert layers["version_reconcile"]["passed"] is False and report["passed"] is False


def test_one_version_has_nothing_to_reconcile(root):
    make("csv")
    report = wt.run_workspace(DEV_ORG_ID, SLUG)
    assert "version_reconcile" not in {la["layer"] for la in report["layers"]}


def test_compare_is_pure_and_counts_only(tmp_path):
    src = {"name": "s", "format": "csv", "layout": {"delimiter": ",", "header": True, "encoding": "utf-8"},
           "columns": [{"name": "id", "type": "int", "key": True}, {"name": "v", "type": "int"}]}
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("id,v\n1,1\n2,1\n")
    b.write_text("id,v\n2,5\n3,5\n4,5\n")
    got = drift.compare(src, drift.observe({**src, "upload_path": str(a)}), drift.observe({**src, "upload_path": str(b)}))
    assert got["rows"] == {"from": 2, "to": 3}
    assert got["keys"] == {"key": "id", "added": 2, "removed": 1, "kept": 1}
    assert got["measures"]["v"] == {"from": 2.0, "to": 15.0}


# ---------------------------------------------------------------- ontology impact

ONTOLOGY = {
    "object_types": {"Order": {"prefix": "ORD", "owner": "ops", "title": "customer", "sources": [
        {"system": "orders", "table": "silver.orders", "key": "order_id", "fields": {"customer": "customer"}}]}},
}


def test_drift_on_a_column_the_ontology_uses_is_flagged(root):
    make("csv", ontology=ONTOLOGY, policies={"default": "deny", "rules": []})
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add("csv", base_rows(), ["order_id", "amount"])
    assert kinds(record)["missing_column:customer"]["ontology"] == [{"kind": "object", "name": "Order"}]
    assert record["drift"]["ontology_impact"] is True
    overview = wt.drift_overview(DEV_ORG_ID, SLUG)
    assert overview["ontology_impact"] == [{"source": "orders", "version": 2, "kind": "missing_column",
                                            "column": "customer", "severity": "warn",
                                            "ontology": [{"kind": "object", "name": "Order"}]}]


def test_drift_on_a_column_the_ontology_does_not_use_is_not_flagged(root):
    make("csv", ontology=ONTOLOGY, policies={"default": "deny", "rules": []})
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add("csv", [{**r, "amount": "TBD"} for r in base_rows()])
    assert kinds(record)["type_change:amount"]["ontology"] == []
    assert record["drift"]["ontology_impact"] is False
    assert wt.drift_overview(DEV_ORG_ID, SLUG)["ontology_impact"] == []


def test_a_renamed_column_is_followed_through_the_model(root):
    ontology = {"object_types": {"Order": {"sources": [
        {"system": "orders", "table": "silver.orders", "key": "order_id", "fields": {"who": "buyer"}}]}}}
    models = {"silver": {"01_orders.sql": "select order_id, upper(customer) as buyer, amount from bronze_orders"}}
    make("csv", models=models, ontology=ontology, policies={"default": "deny", "rules": []})
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add("csv", base_rows(), ["order_id", "amount"])
    assert kinds(record)["missing_column:customer"]["ontology"] == [{"kind": "object", "name": "Order"}]


def test_a_link_reading_the_column_is_flagged(root):
    ontology = {"object_types": {"Order": {"sources": [
        {"system": "orders", "table": "silver.orders", "key": "order_id", "fields": {"amount": "amount"}}]},
        "Customer": {"sources": [{"system": "c", "table": "silver.orders", "key": "customer", "fields": {"amount": "amount"}}]}},
        "link_types": {"order_customer": {"from": "Order", "to": "Customer",
                                          "sql": "select 'orders' from_system, order_id from_key, 'c' to_system, "
                                                 "customer to_key from silver_orders"}}}
    make("csv", ontology=ontology, policies={"default": "deny", "rules": []})
    wt.run_workspace(DEV_ORG_ID, SLUG)
    record = add("csv", [{**r, "customer": ""} for r in base_rows()], ["order_id", "amount"])
    flagged = kinds(record)["missing_column:customer"]["ontology"]
    assert {"kind": "link", "name": "order_customer"} in flagged


def test_a_new_column_cannot_touch_an_ontology_even_when_its_name_matches(root):
    """Nothing reads it yet. The model's region comes from customer, not from this."""
    ontology = {"object_types": {"Order": {"sources": [
        {"system": "orders", "table": "silver.orders", "key": "order_id", "fields": {"where": "region"}}]}}}
    models = {"silver": {"01_orders.sql": "select order_id, upper(customer) as region, amount from bronze_orders"}}
    make("csv", models=models, ontology=ontology, policies={"default": "deny", "rules": []})
    record = add("csv", [{**r, "region": "E"} for r in base_rows()], [*COLUMNS, "region"])
    assert "new_column:region" in kinds(record) and record["drift"]["ontology_impact"] is False


# ---------------------------------------------------------------- HTTP

def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


@pytest.fixture()
def http(meta_db):
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        a = c.post("/auth/signup", json={"email": "a@a.example", "password": PASSWORD, "org_name": "Alpha org"}).json()
        b = c.post("/auth/signup", json={"email": "b@b.example", "password": PASSWORD, "org_name": "Beta org"}).json()
        yield c, a, b


def _upload(c, session, data: bytes) -> str:
    r = c.post("/uploads", headers=bearer(session), files=[("files", ("orders.csv", data))])
    assert r.status_code == 201, r.text
    return f"{r.json()['id']}/orders.csv"


def _http_workspace(c, a) -> None:
    wt.create_workspace(a["org_id"], SLUG, spec_for("csv", _upload(c, a, render("csv", base_rows()))), models=SILVER)


def test_the_routes_add_list_and_update(http):
    c, a, _ = http
    _http_workspace(c, a)
    made = c.post(f"/workspaces/{SLUG}/sources/orders/versions", headers=bearer(a),
                  json={"upload": _upload(c, a, render("csv", base_rows(), ["customer", "amount"]))})
    assert made.status_code == 201, made.text
    assert made.json()["version"] == 2 and made.json()["drift"]["blocking"] is True

    listed = c.get(f"/workspaces/{SLUG}/sources/orders/versions", headers=bearer(a))
    assert [v["version"] for v in listed.json()["versions"]] == [1, 2]

    overview = c.get(f"/workspaces/{SLUG}/drift", headers=bearer(a)).json()
    assert overview["sources"]["orders"]["blocking"] is True and overview["ontology_impact"] == []

    columns = [{"name": "customer", "type": "string"}, {"name": "amount", "type": "decimal(12,2)"}]
    updated = c.post(f"/workspaces/{SLUG}/sources/orders/spec", headers=bearer(a), json={"columns": columns})
    assert updated.status_code == 200, updated.text
    assert updated.json()["drift"]["blocking"] is False


def test_the_routes_answer_a_bad_body_with_our_message(http):
    c, a, _ = http
    _http_workspace(c, a)
    r = c.post(f"/workspaces/{SLUG}/sources/orders/versions", headers=bearer(a), json={"upload": "nope/nope.csv"})
    assert r.status_code == 422 and "no such upload" in r.text
    r = c.post(f"/workspaces/{SLUG}/sources/ghost/versions", headers=bearer(a),
               json={"upload": _upload(c, a, b"a\n1\n")})
    assert r.status_code == 404 and "no source ghost" in r.text


def test_another_org_cannot_reach_the_routes_or_use_its_upload_here(http):
    c, a, b = http
    _http_workspace(c, a)
    theirs = _upload(c, b, render("csv", base_rows()))
    for method, path, body in (("GET", f"/workspaces/{SLUG}/sources/orders/versions", None),
                               ("POST", f"/workspaces/{SLUG}/sources/orders/versions", {"upload": theirs}),
                               ("POST", f"/workspaces/{SLUG}/sources/orders/spec", {"columns": []}),
                               ("GET", f"/workspaces/{SLUG}/drift", None)):
        r = c.request(method, path, headers=bearer(b), json=body)
        assert r.status_code == 404, (method, path, r.text)
    r = c.post(f"/workspaces/{SLUG}/sources/orders/versions", headers=bearer(a), json={"upload": theirs})
    assert r.status_code == 422 and "no such upload" in r.text
    assert len(wt.source_versions(a["org_id"], SLUG, "orders")) == 1
