"""Connector ladder contract tests. The iron law: a rung ships only when it
moves real bytes on this machine behind a replayable offline fixture, and its
bronze output is byte-identical to the local-file path on the same fixture
wherever that comparison exists. moto fakes S3 in-process; the adbc sqlite
driver reads the same db the native sqlite kind reads; idoc and stream
fixtures are synthetic and committed or generated inside the test.
"""
import json
import os
import pathlib
import sqlite3

import pyarrow as pa
import pytest

from ironwright.legacy import read_source

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

POS_CSV = {
    "pos_r1_20260110.csv": (
        "store_nbr,item_nbr,units,net_sales\n"
        "0001,000000101,4,23.96\n"
        "0001,000000102,0,0.00\n"
        "0002,000000101,7,41.93\n"
    ),
    "pos_r2_20260110.csv": (
        "store_nbr,item_nbr,units,net_sales\n"
        "0009,000000103,2,9.98\n"
        "0009,000000101,,\n"
    ),
}

ITEMMAST = (
    "000000101123456789012SYNTH SHAMPOO 300ML           00000101001000000599A\n"
    "000000102123456789029SYNTH CONDITIONER 300ML       00000101001000000649A\n"
)
ITEM_LAYOUT = [["item_nbr", 9], ["upc", 12], ["description", 30],
               ["vendor_nbr", 8], ["dept", 3], ["cost_cents", 9], ["status", 1]]


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()


def assert_byte_identical(a: pa.Table, b: pa.Table):
    assert a.schema == b.schema
    assert a.equals(b)
    assert _ipc(a) == _ipc(b)


# ------------------------------------------------------- rung 1: object store

@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_s3_csv_drop_lands_byte_identical_to_local_files(tmp_path, aws_env):
    from moto import mock_aws

    pos = tmp_path / "pos"
    pos.mkdir()
    for name, body in POS_CSV.items():
        (pos / name).write_text(body)
    local = read_source({"kind": "csv", "glob": "pos/*.csv"}, tmp_path)
    assert local.num_rows == 5 and "_source_file" in local.column_names

    with mock_aws():
        import boto3
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="brightline-drops")
        for name, body in POS_CSV.items():
            s3.put_object(Bucket="brightline-drops", Key=f"drops/{name}", Body=body.encode())
        remote = read_source({
            "kind": "s3", "bucket": "brightline-drops",
            "prefix": "drops/", "glob": "*.csv",
            "inner": {"kind": "csv"},
        }, tmp_path)
    assert_byte_identical(remote, local)


def test_s3_single_key_fixed_width_byte_identical(tmp_path, aws_env):
    from moto import mock_aws

    (tmp_path / "ITEMMAST.dat").write_text(ITEMMAST, encoding="latin-1")
    local = read_source({"kind": "fixed_width", "file": "ITEMMAST.dat",
                         "layout": ITEM_LAYOUT}, tmp_path)
    assert local.num_rows == 2

    with mock_aws():
        import boto3
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="brightline-drops")
        s3.put_object(Bucket="brightline-drops", Key="mainframe/ITEMMAST.dat",
                      Body=ITEMMAST.encode("latin-1"))
        remote = read_source({
            "kind": "s3", "bucket": "brightline-drops",
            "key": "mainframe/ITEMMAST.dat",
            "inner": {"kind": "fixed_width", "layout": ITEM_LAYOUT},
        }, tmp_path)
    assert_byte_identical(remote, local)


# ------------------------------------------------------ rung 2: adbc warehouse

def _seed_vendor_db(path: pathlib.Path):
    con = sqlite3.connect(path)
    con.execute("create table vendors (vendor_nbr integer, vendor_name text, "
                "tax_id text, rebate_pct real)")
    con.executemany("insert into vendors values (?,?,?,?)", [
        (101, "Lumen Skin Company", "41-2290871", 2.5),
        (102, "Synthetic Pet Goods", None, 0.0),
        (103, "", "99-0000000", None),
    ])
    con.commit()
    con.close()


def test_adbc_sqlite_byte_identical_to_native_sqlite_kind(tmp_path):
    pytest.importorskip("adbc_driver_sqlite", reason="adbc-driver-sqlite not installed")
    _seed_vendor_db(tmp_path / "vendor_portal.db")
    query = "select * from vendors order by vendor_nbr"
    native = read_source({"kind": "sqlite", "db": "vendor_portal.db", "query": query}, tmp_path)
    adbc = read_source({"kind": "adbc", "driver": "sqlite",
                        "uri": "vendor_portal.db", "query": query}, tmp_path)
    assert native.num_rows == 3
    assert_byte_identical(adbc, native)


@pytest.mark.skipif(not os.environ.get("POSTGRES_URI"),
                    reason="postgresql rung is code-present but not fixture-proven: "
                           "set POSTGRES_URI to a reachable server to exercise it")
def test_adbc_postgresql_reads_strings(tmp_path):
    pytest.importorskip("adbc_driver_postgresql",
                        reason="adbc-driver-postgresql not installed")
    t = read_source({"kind": "adbc", "driver": "postgresql",
                     "uri": os.environ["POSTGRES_URI"],
                     "query": "select 42 as answer, 'iron' as name"}, tmp_path)
    assert t.num_rows == 1
    assert all(f.type == pa.string() for f in t.schema)
    assert t.column("answer")[0].as_py() == "42"


# ----------------------------------------------------------- rung 3: SAP IDoc

IDOC_SEGMENTS = {
    "EDI_DC40": [["docnum", 16], ["mestyp", 6], ["sndprn", 10]],
    "E2MARAM": [["matnr", 18], ["mtart", 4], ["matkl", 9], ["meins", 3], ["ntgew", 13]],
}


def _write_idoc(path: pathlib.Path) -> dict:
    """Synthetic but shape-true MATMAS-ish flat IDoc: fixed-width records,
    segment name in the first 30 chars, EDI_DC40 control record first.
    All values invented, clearly synthetic."""
    def rec(seg, fields, layout):
        line = f"{seg:<30}"
        for (name, width) in layout:
            line += f"{fields[name]:<{width}}"[:width]
        return line

    control = {"docnum": "0000000000000042", "mestyp": "MATMAS", "sndprn": "SYNTHCLNT1"}
    mats = [
        {"matnr": "SYN-MAT-000001", "mtart": "FERT", "matkl": "SYN-PET-1", "meins": "EA",
         "ntgew": "000000001.250"},
        {"matnr": "SYN-MAT-000002", "mtart": "HAWA", "matkl": "SYN-CARE2", "meins": "CS",
         "ntgew": "000000012.000"},
    ]
    unknown_payload = "ENSynthetic descriptive text, unmapped segment"
    truncated = "SYN-MAT-000003"  # record ends mid-layout after matnr

    lines = [rec("EDI_DC40", control, IDOC_SEGMENTS["EDI_DC40"])]
    lines += [rec("E2MARAM", m, IDOC_SEGMENTS["E2MARAM"]) for m in mats]
    lines.append(f"{'E2MAKTM':<30}" + unknown_payload)
    lines.append(f"{'E2MARAM':<30}" + f"{truncated:<18}")
    path.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return {"control": control, "mats": mats,
            "unknown_payload": unknown_payload, "truncated": truncated}


def test_idoc_flat_file_round_trip(tmp_path):
    planted = _write_idoc(tmp_path / "MATMAS042.idoc")
    t = read_source({"kind": "idoc", "file": "MATMAS042.idoc",
                     "segments": IDOC_SEGMENTS}, tmp_path)
    rows = t.to_pylist()

    # nothing dropped: control + 2 materials + 1 unknown + 1 truncated
    assert len(rows) == 5
    assert [r["_segment"] for r in rows] == \
        ["EDI_DC40", "E2MARAM", "E2MARAM", "E2MAKTM", "E2MARAM"]

    # every planted field value is present, as strings
    assert rows[0]["docnum"] == planted["control"]["docnum"]
    assert rows[0]["mestyp"] == planted["control"]["mestyp"]
    assert rows[0]["sndprn"] == planted["control"]["sndprn"]
    for got, want in zip(rows[1:3], planted["mats"]):
        for field, value in want.items():
            assert got[field] == value

    # unknown segment lands with _segment set and the raw payload preserved
    unknown = rows[3]
    assert unknown["_segment"] == "E2MAKTM"
    assert unknown["_raw"] == planted["unknown_payload"]
    assert unknown["matnr"] is None

    # truncated last record: leading fields parse, the missing tail is null
    tail = rows[4]
    assert tail["matnr"] == planted["truncated"]
    assert tail["mtart"] is None and tail["ntgew"] is None

    # bronze contract: strings only
    assert all(f.type == pa.string() for f in t.schema)


# ------------------------------------------------------ rung 4: stream windows

STREAM_SRC = {"kind": "stream_window", "glob": "stream/segment-*.jsonl",
              "window": "PT5M", "window_id_from": "ts"}

EXPECTED_WINDOWS = {
    "41": "2026-01-01T00:00:00Z",
    "44": "2026-01-01T00:00:00Z",
    "47": "2026-01-01T00:05:00Z",   # exactly on the boundary: later window
    "12": "2026-01-01T00:05:00Z",
    "15": "2026-01-01T00:10:00Z",   # exactly on the boundary: later window
    "18": "2026-01-01T00:10:00Z",
}


def test_stream_window_assignment_is_deterministic_across_boundaries():
    t = read_source(STREAM_SRC, FIXTURES)
    rows = t.to_pylist()
    assert len(rows) == len(EXPECTED_WINDOWS)
    got = {r["reading"]: r["_window_id"] for r in rows}
    assert got == EXPECTED_WINDOWS
    assert all(f.type == pa.string() for f in t.schema)


def test_stream_window_rerun_is_idempotent_identical():
    first = read_source(STREAM_SRC, FIXTURES)
    second = read_source(STREAM_SRC, FIXTURES)
    assert first.schema == second.schema
    assert first.equals(second)
    assert _ipc(first) == _ipc(second)


def test_stream_window_numeric_seconds_equals_iso_duration():
    iso = read_source(STREAM_SRC, FIXTURES)
    numeric = read_source({**STREAM_SRC, "window": 300}, FIXTURES)
    assert iso.equals(numeric)


def test_stream_window_event_missing_ts_is_an_error_not_a_drop(tmp_path):
    seg = tmp_path / "stream"
    seg.mkdir()
    (seg / "segment-bad.jsonl").write_text(
        json.dumps({"sensor": "dock-9", "reading": "7"}) + "\n")
    with pytest.raises(ValueError, match="missing ts"):
        read_source({**STREAM_SRC}, tmp_path)


# ---------------------------------------------------------------- housekeeping

def test_unknown_kind_still_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="unknown source kind"):
        read_source({"kind": "carrier_pigeon"}, tmp_path)
