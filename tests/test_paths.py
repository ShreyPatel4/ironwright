"""Every name that becomes a path is impossible to escape, and a user's source
folder is read only. C4m points a workspace at real files for the first time,
so these run against real files: real symlinks, a real FIFO, a real device
node, a sparse oversize file.

The data root sits two levels inside tmp_path on purpose. A traversal that
works here hits the planted files in ``outer`` and stays in the sandbox.
"""
import json
import os
import signal
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from ironwright import auth, boards, config, connectors, workspaces
from ironwright.legacy import read_source
from ironwright.packs import load_pack
from ironwright.platform_api import create_platform_app
from ironwright.scenario.spec import SpecError

TRAVERSAL = [
    "..", ".", "", "../x", "../../etc/passwd", "a/../..", "a/b", "a\\b", "..\\x",
    "/etc/passwd", "//etc/passwd",
    "%2e%2e", "%2e%2e%2f", "..%2f", "%2e%2e%2fetc", "%252e%252e%252f", "..%c0%af",
    ".hidden", "a\x00b", "abc\n", "x" * 300,
]
ARCHETYPE = "regional_retail_chain"
BOARD = {"title": "T", "tiles": [{"sql": "select 1", "viz": "table"}]}


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture()
def outer(tmp_path, monkeypatch):
    """Data root at outer/data, so outer is what a ``..`` escape lands on."""
    outer = tmp_path / "outer"
    (outer / "data").mkdir(parents=True)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(outer / "data"))
    return outer


@pytest.fixture()
def root(outer):
    return outer / "data"


@pytest.fixture()
def secret(outer):
    """Looks like a deletable, readable organization but is not under the root."""
    (outer / "org.json").write_text('{"company": "LEAKED"}')
    (outer / "scenario.yaml").write_text("name: outer\n")
    (outer / "x").mkdir()
    (outer / "x" / "org.json").write_text('{"company": "LEAKED"}')
    (outer / "x" / "scenario.yaml").write_text("name: x\n")
    return outer / "org.json"


# -------------------------------------------------------------- safe_child

@pytest.mark.parametrize("name", TRAVERSAL)
def test_safe_child_refuses_every_escape_spelling(tmp_path, name):
    with pytest.raises(config.UnsafePath):
        config.safe_child(tmp_path, name)


def test_safe_child_refuses_a_name_that_is_not_a_string(tmp_path):
    with pytest.raises(config.UnsafePath):
        config.safe_child(tmp_path, tmp_path / "x")


@pytest.mark.parametrize("name", ["retail", "supply_chain", "q3-2026.json", "A1"])
def test_safe_child_returns_the_plain_child(tmp_path, name):
    assert config.safe_child(tmp_path, name) == tmp_path.resolve() / name


def test_safe_child_refuses_a_symlink_out_of_the_root(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "root" / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(config.UnsafePath):
        config.safe_child(tmp_path / "root", "link")


def test_safe_child_refuses_a_symlink_to_a_sibling_inside_the_root(tmp_path):
    # is_relative_to alone would pass this: one workspace aliasing another.
    (tmp_path / "other").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "other")
    with pytest.raises(config.UnsafePath):
        config.safe_child(tmp_path, "alias")


def test_safe_child_refuses_a_symlink_loop(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    with pytest.raises(config.UnsafePath):
        config.safe_child(tmp_path, "loop")


def test_safe_child_accepts_a_root_reached_through_a_symlink(tmp_path):
    # macOS: /var is /private/var. A symlinked root is not an escape.
    (tmp_path / "real").mkdir()
    (tmp_path / "via").symlink_to(tmp_path / "real")
    assert config.safe_child(tmp_path / "via", "w1") == tmp_path.resolve() / "real" / "w1"


def test_safe_child_takes_a_wider_pattern_for_file_names_but_never_a_slash(tmp_path):
    name = "Q3 report (final).csv"
    assert config.safe_child(tmp_path, name, pattern=config.FILE_NAME).name == name
    for bad in ("..", "../x", "a/b", "%2e%2e%2f", "", ".hidden"):
        with pytest.raises(config.UnsafePath):
            config.safe_child(tmp_path, bad, pattern=config.FILE_NAME)


@pytest.mark.parametrize("name", TRAVERSAL)
def test_pack_paths_cannot_leave_the_data_root(root, name):
    with pytest.raises(config.UnsafePath):
        _ = config.PackPaths(name).root


def test_pack_paths_report_names_are_checked(root):
    paths = config.PackPaths("retail")
    assert paths.report("evals") == root.resolve() / "retail" / "reports" / "evals.json"
    for bad in ("../../supply_chain/reports/evals", "%2e%2e%2fevals", "/etc/passwd"):
        with pytest.raises(config.UnsafePath):
            paths.report(bad)


# -------------------------------------------------------------- workspaces

@pytest.mark.parametrize("name", TRAVERSAL)
def test_read_org_never_reads_outside_the_root(root, secret, name):
    with pytest.raises(FileNotFoundError):
        workspaces.read_org(name)


@pytest.mark.parametrize("name", TRAVERSAL)
def test_delete_never_touches_anything_outside_the_root(root, secret, name):
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization(name)
    assert secret.read_text() == '{"company": "LEAKED"}'
    assert (secret.parent / "x" / "org.json").exists()
    assert root.exists()


@pytest.mark.parametrize("name", ["../x", "%2e%2e%2fx", "/tmp/x", "abc\n", "a/b", ".."])
def test_create_refuses_a_name_that_is_not_a_slug(root, name):
    with pytest.raises(SpecError):
        workspaces.create_organization(name, ARCHETYPE, 4, 7)


def test_create_then_read_then_delete_still_works(root):
    org = workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    assert workspaces.read_org("acme") == org
    assert [o["name"] for o in workspaces.list_organizations()] == ["acme"]
    workspaces.delete_organization("acme")
    assert not (root / "acme").exists()
    assert workspaces.list_organizations() == []


def test_delete_refuses_a_foreign_folder_that_only_looks_like_an_org(root):
    d = root / "foreign"
    d.mkdir()
    (d / "scenario.yaml").write_text("name: foreign\n")
    (d / "org.json").write_text("{}")
    (d / "precious.txt").write_text("keep me")
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("foreign")
    assert (d / "precious.txt").read_text() == "keep me"


def test_delete_refuses_a_pack_run_directory(root):
    d = root / "retail"
    d.mkdir()
    (d / "ontology.duckdb").write_text("audit")
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("retail")
    assert (d / "ontology.duckdb").exists()


def test_delete_refuses_a_marker_copied_from_another_workspace(root):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    d = root / "victim"
    d.mkdir()
    (d / "precious.txt").write_text("keep me")
    marker = next(p for p in (root / "acme").iterdir() if p.name.startswith("."))
    (d / marker.name).write_bytes(marker.read_bytes())
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("victim")
    assert (d / "precious.txt").exists()


def test_delete_refuses_a_marker_that_is_a_symlink(root, outer):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    d = root / "victim"
    d.mkdir()
    marker = next(p for p in (root / "acme").iterdir() if p.name.startswith("."))
    # The content is right for this folder, so only the symlink check can refuse it.
    forged = outer / "forged"
    forged.write_text(json.dumps({"created_by": "ironwright", "name": "victim"}))
    (d / marker.name).symlink_to(forged)
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("victim")
    assert d.exists()


def test_delete_refuses_a_symlink_to_a_real_org(root, tmp_path):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    (root / "alias").symlink_to(root / "acme")
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("alias")
    assert (root / "acme" / "org.json").exists()


def test_create_refuses_to_adopt_an_existing_folder(root):
    d = root / "foreign"
    d.mkdir()
    (d / "precious.txt").write_text("keep me")
    with pytest.raises(SpecError):
        workspaces.create_organization("foreign", ARCHETYPE, 4, 7)
    assert sorted(p.name for p in d.iterdir()) == ["precious.txt"]


def test_create_refuses_a_name_that_is_already_a_symlink(root, outer):
    (root / "alias").symlink_to(outer)
    with pytest.raises(SpecError):
        workspaces.create_organization("alias", ARCHETYPE, 4, 7)
    assert not (outer / "org.json").exists()


def test_an_org_named_auth_cannot_take_the_credentials_with_it(root):
    auth.set_account("a@b.example", "password123")
    creds = root / "auth" / "credentials.json"
    before = creds.read_text()
    with pytest.raises(SpecError):
        workspaces.create_organization("auth", ARCHETYPE, 4, 7)
    with pytest.raises(FileNotFoundError):
        workspaces.delete_organization("auth")
    assert creds.read_text() == before


def test_an_org_cannot_reserve_the_credentials_folder_before_it_exists(root):
    with pytest.raises(SpecError):
        workspaces.create_organization("auth", ARCHETYPE, 4, 7)
    assert not (root / "auth").exists()


def test_list_organizations_skips_a_symlink_out_of_the_root(root, outer):
    d = outer / "foreign"
    d.mkdir()
    (d / "scenario.yaml").write_text("name: foreign\n")
    (d / "org.json").write_text('{"company": "LEAKED"}')
    (root / "alias").symlink_to(d)
    assert workspaces.list_organizations() == []


def test_read_org_refuses_an_org_json_symlinked_out(root, secret):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    (root / "acme" / "org.json").unlink()
    (root / "acme" / "org.json").symlink_to(secret)
    with pytest.raises(FileNotFoundError):
        workspaces.read_org("acme")
    assert [o["name"] for o in workspaces.list_organizations()] == []


# ---------------------------------------------------- traversal over HTTP

@pytest.mark.parametrize("encoded", ["%2e%2e", "%2E%2E", ".%2e", "%2e."])
def test_url_encoded_dots_do_not_read_outside_the_root(root, secret, encoded):
    client = TestClient(create_platform_app())
    resp = client.get(f"/workspaces/{encoded}/organization")
    assert resp.status_code == 404
    assert "LEAKED" not in resp.text


@pytest.mark.parametrize("encoded", ["%2e%2e", "%2E%2E", ".%2e", "%2e."])
def test_url_encoded_dots_do_not_delete_outside_the_root(root, secret, encoded):
    client = TestClient(create_platform_app())
    assert client.delete(f"/workspaces/{encoded}").status_code == 404
    assert secret.exists()
    assert root.exists()


def test_url_encoded_board_slug_stays_a_404(root):
    pack = load_pack("retail")
    pack.paths.ensure()
    (pack.paths.reports / "evals.json").write_text('{"tiles": "LEAKED", "title": "x"}')
    client = TestClient(create_platform_app())
    for slug in ("%2e%2e", "..%2freports%2fevals", "%2e%2e%2freports%2fevals", "%252e%252e%252freports"):
        resp = client.get(f"/workspaces/retail/boards/{slug}")
        assert resp.status_code in (404, 405)
        assert "LEAKED" not in resp.text


# ------------------------------------------------------------------ boards

@pytest.fixture()
def pack(root):
    p = load_pack("retail")
    p.paths.ensure()
    # A climb out of boards/ only resolves when boards/ exists; a missing
    # folder would make every traversal test pass for the wrong reason.
    (p.paths.root / "boards").mkdir()
    return p


@pytest.mark.parametrize("slug", ["../reports/evals", "../outside", "/etc/passwd",
                                  "%2e%2e%2freports%2fevals", "..", "a/b", "abc\n"])
def test_board_read_and_delete_cannot_leave_the_boards_folder(pack, slug):
    target = pack.paths.reports / "evals.json"
    target.write_text('{"title": "LEAKED", "tiles": []}')
    (pack.paths.root / "outside.json").write_text('{"title": "LEAKED", "tiles": []}')
    with pytest.raises(FileNotFoundError):
        boards.read(pack, slug)
    with pytest.raises(FileNotFoundError):
        boards.delete(pack, slug)
    assert target.exists()


@pytest.mark.parametrize("slug", ["../reports/evals", "%2e%2e%2fx", "/etc/passwd", "abc\n"])
def test_board_write_refuses_a_bad_slug(pack, slug):
    with pytest.raises(boards.BoardError):
        boards.write(pack, slug, BOARD)
    assert not (pack.paths.reports / "evals.json").exists()


def test_board_round_trip_still_works(pack):
    boards.write(pack, "sales", BOARD)
    assert boards.read(pack, "sales")["title"] == "T"
    assert [b["slug"] for b in boards.list_boards(pack)] == ["sales"]
    boards.delete(pack, "sales")
    assert boards.list_boards(pack) == []


def test_board_file_symlinked_out_is_neither_read_nor_listed(pack, outer):
    outside = outer / "x.json"
    outside.write_text('{"title": "LEAKED", "tiles": []}')
    boards.write(pack, "real", BOARD)
    link = pack.paths.root / "boards" / "evil.json"
    link.symlink_to(outside)
    with pytest.raises(FileNotFoundError):
        boards.read(pack, "evil")
    assert [b["slug"] for b in boards.list_boards(pack)] == ["real"]


def test_boards_folder_symlinked_out_is_refused(pack, outer):
    outside = outer / "boards"
    outside.mkdir()
    (outside / "evil.json").write_text('{"title": "LEAKED", "tiles": []}')
    (pack.paths.root / "boards").rmdir()
    (pack.paths.root / "boards").symlink_to(outside)
    with pytest.raises(FileNotFoundError):
        boards.read(pack, "evil")
    with pytest.raises(boards.BoardError):
        boards.write(pack, "evil", BOARD)
    assert boards.list_boards(pack) == []
    assert "LEAKED" in (outside / "evil.json").read_text()


# ------------------------------------------------------- L1 source folders

@pytest.fixture()
def src(tmp_path):
    d = tmp_path / "source"
    d.mkdir()
    (d / "a.csv").write_text("id,name\n1,ann\n")
    (d / "b.csv").write_text("id,name\n2,bob\n")
    return d


@pytest.fixture()
def alarm():
    """A FIFO opened without O_NONBLOCK hangs forever. This turns that
    regression into a red test instead of a stuck CI job."""
    def boom(*_):
        raise AssertionError("blocked on a FIFO: the reader opened it without O_NONBLOCK")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def test_source_bytes_returns_the_file(src):
    assert connectors.read_source_bytes(src, "a.csv") == b"id,name\n1,ann\n"


@pytest.mark.parametrize("rel", ["../outside.txt", "sub/../../outside.txt", "/etc/passwd"])
def test_source_read_refuses_paths_out_of_the_root(src, rel):
    (src.parent / "outside.txt").write_text("secret")
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, rel)


def test_url_encoded_source_name_is_not_decoded_into_a_traversal(src):
    (src.parent / "outside.txt").write_text("secret")
    with pytest.raises((connectors.SourceRefused, FileNotFoundError)):
        connectors.read_source_bytes(src, "%2e%2e%2foutside.txt")


def test_source_read_refuses_a_symlink_that_escapes(src):
    (src.parent / "outside.txt").write_text("secret")
    (src / "link.csv").symlink_to(src.parent / "outside.txt")
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "link.csv")


def test_source_read_refuses_a_directory_symlink_that_escapes(src):
    (src.parent / "elsewhere").mkdir()
    (src.parent / "elsewhere" / "x.csv").write_text("secret")
    (src / "sub").symlink_to(src.parent / "elsewhere")
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "sub/x.csv")
    with pytest.raises(connectors.SourceRefused):
        connectors.source_files(src, "sub/*.csv")


def test_source_read_follows_a_symlink_that_stays_inside(src):
    (src / "link.csv").symlink_to(src / "a.csv")
    assert connectors.read_source_bytes(src, "link.csv") == b"id,name\n1,ann\n"


def test_source_read_refuses_a_fifo_without_hanging(src, alarm):
    os.mkfifo(src / "pipe.csv")
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "pipe.csv")


def test_source_read_refuses_a_device_file():
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes("/dev", "null")


def test_source_read_refuses_a_directory(src):
    (src / "sub").mkdir()
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "sub")


def test_source_read_refuses_an_oversize_file(src):
    with open(src / "big.csv", "wb") as fh:
        fh.truncate(1025)  # sparse: the test stays cheap
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "big.csv", max_bytes=1024)


def test_source_read_takes_a_file_exactly_at_the_cap(src):
    (src / "edge.csv").write_bytes(b"x" * 1024)
    assert len(connectors.read_source_bytes(src, "edge.csv", max_bytes=1024)) == 1024


def test_size_cap_comes_from_the_environment(src, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", "4")
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "a.csv")


def test_size_cap_has_a_default_with_no_environment(src, monkeypatch):
    monkeypatch.delenv("IRONWRIGHT_L1_MAX_FILE_BYTES", raising=False)
    with open(src / "huge.csv", "wb") as fh:
        fh.truncate(64 * 1024 ** 3)  # sparse; no real local load wants 64 GiB in memory
    with pytest.raises(connectors.SourceRefused):
        connectors.read_source_bytes(src, "huge.csv")


def test_file_count_is_capped_per_load(src):
    assert len(connectors.source_files(src, "*.csv", max_files=2)) == 2
    with pytest.raises(connectors.SourceRefused):
        connectors.source_files(src, "*.csv", max_files=1)


def test_file_count_cap_comes_from_the_environment(src, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILES", "1")
    with pytest.raises(connectors.SourceRefused):
        connectors.source_files(src, "*.csv")


def test_source_files_are_sorted_and_relative(src):
    assert connectors.source_files(src, "*.csv") == ["a.csv", "b.csv"]


def test_source_files_refuses_a_fifo_in_the_glob(src, alarm):
    os.mkfifo(src / "pipe.csv")
    with pytest.raises(connectors.SourceRefused):
        connectors.source_files(src, "*.csv")


def test_source_files_refuses_a_glob_that_climbs_out(src):
    (src.parent / "outside.csv").write_text("secret")
    with pytest.raises(connectors.SourceRefused):
        connectors.source_files(src, "../*.csv")


def _freeze(path):
    """The user's folder, as read only as the OS can make it."""
    for f in path.iterdir():
        f.chmod(0o444)
    path.chmod(0o555)


def _thaw(path):
    path.chmod(0o755)


def test_copy_leaves_the_source_untouched_and_the_copy_private(src, root):
    stats = {f.name: (f.stat().st_mtime_ns, f.stat().st_size) for f in src.iterdir()}
    _freeze(src)
    dest = root / "w1" / "legacy"
    try:
        assert connectors.copy_source_files(src, dest, "*.csv") == ["a.csv", "b.csv"]
    finally:
        _thaw(src)
    assert {f.name: (f.stat().st_mtime_ns, f.stat().st_size) for f in src.iterdir()} == stats
    assert (dest / "a.csv").read_text() == "id,name\n1,ann\n"
    assert mode(dest) == 0o700
    assert mode(dest / "a.csv") == 0o600


def test_copy_refuses_colliding_basenames(src, root):
    (src / "sub").mkdir()
    (src / "sub" / "a.csv").write_text("id,name\n9,zed\n")
    (src / "sub2").mkdir()
    (src / "sub2" / "a.csv").write_text("id,name\n8,yan\n")
    with pytest.raises(connectors.SourceRefused):
        connectors.copy_source_files(src, root / "w1", "sub*/a.csv")


def test_copy_refuses_to_write_through_a_symlink_in_the_destination(src, root, outer):
    dest = root / "w1"
    dest.mkdir()
    victim = outer / "victim.txt"
    victim.write_text("keep me")
    (dest / "a.csv").symlink_to(victim)
    with pytest.raises(config.UnsafePath):
        connectors.copy_source_files(src, dest, "a.csv")
    assert victim.read_text() == "keep me"


def test_local_folder_kind_lands_through_the_bronze_contract(src):
    t = read_source({"kind": "local_folder", "root": str(src),
                     "inner": {"kind": "csv", "glob": "*.csv"}}, src.parent)
    assert t.to_pylist() == [
        {"id": "1", "name": "ann", "_source_file": "a.csv"},
        {"id": "2", "name": "bob", "_source_file": "b.csv"},
    ]


def test_local_folder_kind_ignores_a_glob_that_tries_to_climb_out(src):
    (src.parent / "outside.csv").write_text("id,name\n666,LEAKED\n")
    with pytest.raises(connectors.SourceRefused):
        read_source({"kind": "local_folder", "root": str(src),
                     "inner": {"kind": "csv", "glob": "../*.csv"}}, src.parent)


def test_local_folder_kind_refuses_a_fifo_and_a_symlink_escape(src, alarm):
    os.mkfifo(src / "pipe.csv")
    spec = {"kind": "local_folder", "root": str(src),
            "inner": {"kind": "csv", "glob": "*.csv"}}
    with pytest.raises(connectors.SourceRefused):
        read_source(spec, src.parent)
    (src / "pipe.csv").unlink()
    (src.parent / "outside.txt").write_text("secret")
    (src / "link.csv").symlink_to(src.parent / "outside.txt")
    with pytest.raises(connectors.SourceRefused):
        read_source(spec, src.parent)


def test_local_folder_fixed_width_uses_the_named_file(src):
    (src / "item.dat").write_text("000101ANN\n")
    t = read_source({"kind": "local_folder", "root": str(src),
                     "inner": {"kind": "fixed_width", "file": "item.dat",
                               "layout": [["id", 6], ["name", 3]]}}, src.parent)
    assert t.to_pylist() == [{"id": "000101", "name": "ANN"}]


def test_local_folder_jsonl(src):
    (src / "e.jsonl").write_text('{"a": 1}\n')
    t = read_source({"kind": "local_folder", "root": str(src),
                     "inner": {"kind": "jsonl", "glob": "*.jsonl"}}, src.parent)
    assert t.to_pylist() == [{"a": "1"}]


def test_local_folder_refuses_an_inner_kind_it_cannot_read_read_only(src):
    with pytest.raises(ValueError):
        read_source({"kind": "local_folder", "root": str(src),
                     "inner": {"kind": "sqlite", "db": "x.db", "query": "select 1"}}, src.parent)


def test_idoc_reads_through_the_guard(src):
    (src.parent / "outside.idoc").write_text("EDI_DC40" + " " * 22 + "LEAKED\n")
    (src / "link.idoc").symlink_to(src.parent / "outside.idoc")
    spec = {"kind": "idoc", "segments": {}, "file": "link.idoc"}
    with pytest.raises(connectors.SourceRefused):
        read_source(spec, src)
    with pytest.raises(connectors.SourceRefused):
        read_source({**spec, "file": "../outside.idoc"}, src)


def test_stream_window_reads_through_the_guard(src, alarm):
    os.mkfifo(src / "a.jsonl")
    spec = {"kind": "stream_window", "glob": "*.jsonl", "window": "PT5M"}
    with pytest.raises(connectors.SourceRefused):
        read_source(spec, src)
    (src / "a.jsonl").unlink()
    (src.parent / "outside.jsonl").write_text('{"ts": 1, "leak": true}\n')
    with pytest.raises(connectors.SourceRefused):
        read_source({**spec, "glob": "../*.jsonl"}, src)


def test_adbc_sqlite_relative_uri_is_read_only_and_cannot_climb(src):
    pytest.importorskip("adbc_driver_sqlite", reason="adbc-driver-sqlite not installed")
    db = src / "v.db"
    con = sqlite3.connect(db)
    con.execute("create table t (a text)")
    con.execute("insert into t values ('x')")
    con.commit()
    con.close()
    _freeze(src)
    try:
        spec = {"kind": "adbc", "driver": "sqlite", "uri": "v.db", "query": "select a from t"}
        assert read_source(spec, src).to_pylist() == [{"a": "x"}]
        with pytest.raises(Exception, match="(?i)readonly|read-only|read only"):
            read_source({**spec, "query": "insert into t values ('y') returning a"}, src)
        assert sorted(p.name for p in src.iterdir()) == ["a.csv", "b.csv", "v.db"]
        with pytest.raises(connectors.SourceRefused):
            read_source({**spec, "uri": "../v.db"}, src)
    finally:
        _thaw(src)


def test_s3_key_that_ends_in_dots_cannot_write_outside_the_temp_dir(monkeypatch, tmp_path):
    moto = pytest.importorskip("moto", reason="moto not installed")
    boto3 = pytest.importorskip("boto3", reason="boto3 not installed")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="drops")
        s3.put_object(Bucket="drops", Key="drop/..", Body=b"id\n1\n")
        with pytest.raises(config.UnsafePath):
            read_source({"kind": "s3", "bucket": "drops", "key": "drop/..",
                         "inner": {"kind": "csv"}}, tmp_path)


def test_s3_key_with_a_space_still_lands(monkeypatch, tmp_path):
    moto = pytest.importorskip("moto", reason="moto not installed")
    boto3 = pytest.importorskip("boto3", reason="boto3 not installed")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="drops")
        s3.put_object(Bucket="drops", Key="drop/Q3 report.csv", Body=b"id\n1\n")
        t = read_source({"kind": "s3", "bucket": "drops", "key": "drop/Q3 report.csv",
                         "inner": {"kind": "csv"}}, tmp_path)
    assert t.to_pylist() == [{"id": "1", "_source_file": "Q3 report.csv"}]


# ------------------------------------------------------------- permissions

@pytest.fixture()
def wide_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_ensure_makes_the_data_root_and_every_layer_private(root, wide_umask):
    root.chmod(0o755)
    paths = config.PackPaths("retail").ensure()
    for d in (root, paths.root, paths.legacy, paths.lake, paths.traces, paths.reports):
        assert mode(d) == 0o700, d


def test_ensure_tightens_a_workspace_that_already_exists(root, wide_umask):
    paths = config.PackPaths("retail")
    paths.reports.mkdir(parents=True)
    paths.root.chmod(0o755)
    paths.reports.chmod(0o775)
    paths.ensure()
    assert mode(paths.root) == 0o700
    assert mode(paths.reports) == 0o700


def test_ensure_tightens_the_audit_store_and_its_sidecars(root, wide_umask):
    paths = config.PackPaths("retail")
    paths.root.mkdir(parents=True)
    wal = paths.root / "ontology.duckdb.wal"
    for f in (paths.ontology_db, wal, paths.checkpoints):
        f.write_text("x")
        f.chmod(0o644)
    paths.ensure()
    for f in (paths.ontology_db, wal, paths.checkpoints):
        assert mode(f) == 0o600, f


def test_ensure_tightens_the_credentials(root, wide_umask):
    auth.set_account("a@b.example", "password123")
    creds = root / "auth" / "credentials.json"
    creds.chmod(0o644)
    (root / "auth").chmod(0o755)
    config.PackPaths("retail").ensure()
    assert mode(root / "auth") == 0o700
    assert mode(creds) == 0o600


def test_ensure_never_chmods_a_shared_sticky_data_root(tmp_path, monkeypatch):
    bits = stat.S_ISVTX | 0o777
    # IRONWRIGHT_DATA=/tmp must not lock every other user out of /tmp.
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(bits)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(shared))
    paths = config.PackPaths("retail").ensure()
    assert mode(shared) == bits
    assert mode(paths.root) == 0o700


def test_ensure_never_chmods_the_home_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IRONWRIGHT_DATA", str(home))
    paths = config.PackPaths("retail").ensure()
    assert mode(home) == 0o755
    assert mode(paths.root) == 0o700


def test_private_dir_makes_new_parents_private_but_not_existing_ones(tmp_path, wide_umask):
    tmp_path.chmod(0o755)
    config.private_dir(tmp_path / "a" / "b")
    assert mode(tmp_path / "a") == 0o700
    assert mode(tmp_path / "a" / "b") == 0o700
    assert mode(tmp_path) == 0o755


def test_write_private_is_0600_from_birth_and_tightens_an_old_file(tmp_path, wide_umask):
    new = tmp_path / "new.json"
    config.write_private(new, "x")
    assert (new.read_text(), mode(new)) == ("x", 0o600)
    old = tmp_path / "old.json"
    old.write_text("long old contents")
    old.chmod(0o644)
    config.write_private(old, "y")
    assert (old.read_text(), mode(old)) == ("y", 0o600)


def test_write_private_refuses_to_write_through_a_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    (tmp_path / "link").symlink_to(victim)
    with pytest.raises(OSError):
        config.write_private(tmp_path / "link", "x")
    assert victim.read_text() == "keep me"


def test_created_organization_files_are_private(root, wide_umask):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    d = root / "acme"
    assert mode(root) == 0o700
    assert mode(d) == 0o700
    assert sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) == ["org.json", "scenario.yaml"]
    for f in d.iterdir():
        assert mode(f) == 0o600, f


def test_board_files_are_private(pack, wide_umask):
    boards.write(pack, "sales", BOARD)
    assert mode(pack.paths.root / "boards") == 0o700
    assert mode(pack.paths.root / "boards" / "sales.json") == 0o600


def test_marker_records_the_workspace_it_belongs_to(root):
    workspaces.create_organization("acme", ARCHETYPE, 4, 7)
    marker = next(p for p in (root / "acme").iterdir() if p.name.startswith("."))
    assert json.loads(marker.read_text())["name"] == "acme"
