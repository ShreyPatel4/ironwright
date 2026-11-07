"""The hosted, multi-tenant security pass. docs/threat-model.md links here.

Everything that needs a session runs with the door closed and through harden(),
because that is how the hosted app serves.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from ironwright import escalation, orgpolicy, sqlguard
from ironwright.api import MAX_BODY_BYTES, harden
from ironwright.platform_api import create_platform_app
from ironwright.tenancy import db, service

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-9"
MIB = 1024 * 1024


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def hosted(meta_db):
    with TestClient(harden(create_platform_app()), raise_server_exceptions=False) as c:
        yield c


def signup(c, email="owner@a.example", org="Acme Claims", password=PASSWORD):
    return c.post("/auth/signup", json={"email": email, "password": password, "org_name": org})


def count(table) -> int:
    with db.engine().connect() as con:
        return con.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


# ------------------------------------------------------------ signup enumeration

def _shape(value):
    """The structure of an answer: its keys and types, none of its values."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return type(value).__name__


def test_signup_answers_alike_for_new_and_taken_emails(hosted):
    new = signup(hosted, email="first@a.example", org="Org one")
    taken = signup(hosted, email="first@a.example", org="Org two")
    assert new.status_code == taken.status_code == 201
    assert _shape(new.json()) == _shape(taken.json())
    assert sorted(new.headers.keys()) == sorted(taken.headers.keys())
    assert taken.json()["email"] == "first@a.example"
    assert taken.json()["org_slug"] == "org-two"
    assert taken.json()["role"] == "owner"
    assert taken.json()["principal"] == f"user:{taken.json()['user_id']}"


def test_signup_on_a_taken_email_writes_nothing_and_hands_out_no_session(hosted):
    victim = signup(hosted, email="victim@a.example", org="Victim org").json()
    before = {t.name: count(t) for t in (db.users, db.orgs, db.memberships, db.sessions)}

    # The right password as well: a signup is not a login.
    decoy = signup(hosted, email="victim@a.example", org="Attacker org").json()

    assert {t.name: count(t) for t in (db.users, db.orgs, db.memberships, db.sessions)} == before
    assert decoy["user_id"] != victim["user_id"] and decoy["org_id"] != victim["org_id"]
    assert hosted.get("/auth/me", headers=bearer(decoy["token"])).status_code == 401
    # The victim's own session was not rotated away.
    assert hosted.get("/auth/me", headers=bearer(victim["token"])).status_code == 200


def test_signup_does_the_same_slow_work_for_new_and_taken_emails(meta_db, monkeypatch):
    calls = []
    real = service.hashlib.pbkdf2_hmac

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(service.hashlib, "pbkdf2_hmac", counted)
    service.signup("slow@a.example", PASSWORD, "Org one", 1000.0)
    new_cost = len(calls)
    calls.clear()
    service.signup("slow@a.example", PASSWORD, "Org two", 1000.0)
    assert new_cost == len(calls) == 1


def test_login_answers_alike_for_an_unknown_email_and_a_wrong_password(hosted):
    signup(hosted, email="known@a.example")
    wrong = hosted.post("/auth/login", json={"email": "known@a.example", "password": "nope-nope-nope"})
    unknown = hosted.post("/auth/login", json={"email": "ghost@a.example", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_does_the_same_slow_work_for_an_unknown_email_and_a_wrong_password(meta_db, monkeypatch):
    service.signup("known@a.example", PASSWORD, "Org one", 1000.0)
    calls = []
    real = service.hashlib.pbkdf2_hmac

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(service.hashlib, "pbkdf2_hmac", counted)
    for email in ("known@a.example", "ghost@a.example"):
        with pytest.raises(service.BadCredentials):
            service.login(email, "nope-nope-nope", 1000.0)
    assert len(calls) == 2


# ------------------------------------------------------------ session transport

def test_every_door_sets_only_the_httponly_session_cookie(hosted):
    """enterprise-security.md item 1 moved the session from localStorage to a cookie.
    It must be the one cookie, and a script must never be able to read it."""
    made = signup(hosted, email="crumbs@a.example")
    logged_in = hosted.post("/auth/login", json={"email": "crumbs@a.example", "password": PASSWORD})
    invite = hosted.post(f"/orgs/{made.json()['org_slug']}/invites", headers=bearer(logged_in.json()["token"]),
                         json={"email": "second@a.example", "role": "member"})
    accepted = hosted.post("/auth/accept-invite", json={"token": invite.json()["token"], "password": PASSWORD})
    for answer in (made, logged_in, accepted):
        assert answer.status_code in (200, 201), answer.text
        cookies = answer.headers.get_list("set-cookie")
        assert len(cookies) == 1 and cookies[0].startswith("iw_session="), cookies
        low = cookies[0].lower()
        assert "httponly" in low and "secure" in low and "samesite=lax" in low


def test_a_session_token_in_a_cookie_is_not_a_credential(hosted):
    token = signup(hosted, email="crumbs@a.example").json()["token"]
    hosted.cookies.set("session", token)
    hosted.cookies.set("token", token)
    assert hosted.get("/auth/me").status_code == 401


# ---------------------------------------------------------- upload and BodyCap

@pytest.fixture()
def stub_upload():
    """Stands in for T3's route: reads the stream, answers how much it saw."""
    app = FastAPI()

    @app.post("/uploads")
    @app.put("/uploads")
    @app.post("/uploads/{rest}")
    @app.post("/other")
    async def read_all(request: Request):
        seen = 0
        async for chunk in request.stream():
            seen += len(chunk)
        return {"seen": seen}

    return TestClient(harden(app), raise_server_exceptions=False)


def _chunks(total: int):
    for _ in range(total // (64 * 1024)):
        yield b"x" * (64 * 1024)


def test_post_uploads_is_exempt_from_the_body_cap(stub_upload):
    r = stub_upload.post("/uploads", content=b"x" * (3 * MIB))
    assert r.status_code == 200 and r.json()["seen"] == 3 * MIB


def test_a_chunked_post_to_uploads_is_exempt_too(stub_upload):
    r = stub_upload.post("/uploads", content=_chunks(3 * MIB))
    assert r.status_code == 200 and r.json()["seen"] == 3 * MIB


@pytest.mark.parametrize("method,path", [("POST", "/other"), ("PUT", "/uploads"), ("POST", "/uploads/x")])
def test_nothing_else_escapes_the_body_cap(stub_upload, method, path):
    assert stub_upload.request(method, path, content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413


@pytest.fixture()
def real_upload(hosted, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_L1_MAX_FILE_BYTES", str(3 * MIB))
    return hosted


def _csv(size: int) -> bytes:
    head = b"id,name\n"
    line = b"1,abcdefgh\n"
    body = line * ((size - len(head)) // len(line))
    return head + body + b"z" * (size - len(head) - len(body))


def _post_file(c, content: bytes, token: str | None = None):
    return c.post("/uploads", headers=bearer(token) if token else {},
                  files={"file": ("a.csv", content, "text/csv")})


def test_a_3_mib_upload_works_through_the_served_app(real_upload):
    token = signup(real_upload).json()["token"]
    r = _post_file(real_upload, _csv(3 * MIB), token)
    assert r.status_code == 201, r.text


def test_an_upload_one_byte_over_the_file_cap_is_refused(real_upload):
    token = signup(real_upload).json()["token"]
    assert _post_file(real_upload, _csv(3 * MIB + 1), token).status_code == 413
    assert not list((Path(os.environ["IRONWRIGHT_DATA"]) / "tenants").rglob("a.csv"))


def test_the_exemption_does_not_let_a_stranger_stream(real_upload):
    assert _post_file(real_upload, _csv(3 * MIB)).status_code == 401


# ------------------------------------------------------------ escalation identity

@pytest.fixture()
def org_ws(hosted):
    """A real org workspace, its owner, and a member who joined by invite."""
    owner = signup(hosted, email="owner@a.example", org="Acme").json()
    made = hosted.post("/workspaces", headers=bearer(owner["token"]), json={"name": "ops", "size": 4, "seed": 3})
    assert made.status_code == 201, made.text
    invite = hosted.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                         json={"email": "member@a.example", "role": "member"})
    member = hosted.post("/auth/accept-invite", json={"token": invite.json()["token"], "password": PASSWORD}).json()
    # Accepting opened a session for the member in this org; the owner's still stands.
    return hosted, made.json()["name"], made.json()["org"], owner, member


def _escalate(c, ws, org, token, **over):
    action = next(iter(orgpolicy.action_catalog(orgpolicy.read_org(ws))))
    body = {"principal": "user:the-cfo", "action": action, "object_id": "ITM-1",
            "denying_rule": "default-deny", **over}
    return c.post(f"/workspaces/{ws}/escalations", headers=bearer(token), json=body)


def test_an_escalation_is_raised_as_the_session_whatever_the_body_says(org_ws):
    c, ws, org, _, member = org_ws
    r = _escalate(c, ws, org, member["token"])
    assert r.status_code == 201, r.text
    assert r.json()["principal"] == member["principal"]


def test_a_grant_is_decided_as_the_session_and_goes_to_who_asked(org_ws):
    c, ws, org, owner, member = org_ws
    esc = _escalate(c, ws, org, member["token"]).json()
    r = c.post(f"/workspaces/{ws}/escalations/{esc['id']}/decide", headers=bearer(owner["token"]),
               json={"decision": "grant", "decided_by": "role:the-cfo", "expires": "2100-01-01"})
    assert r.status_code == 200, r.text
    assert r.json()["rule"]["granted_by"] == owner["principal"]
    assert r.json()["rule"]["principal"] == member["principal"]
    moments = [json.loads(ln) for ln in escalation._workspace(ws)[1].read_text().splitlines()]
    assert {m["moment"]: m.get("decided_by") for m in moments}["granted"] == owner["principal"]
    log = (orgpolicy._target_dir(ws) / orgpolicy.LOG_FILE).read_text()
    assert owner["principal"] in log and "the-cfo" not in log


def test_a_denial_is_decided_as_the_session(org_ws):
    c, ws, org, owner, member = org_ws
    esc = _escalate(c, ws, org, member["token"]).json()
    r = c.post(f"/workspaces/{ws}/escalations/{esc['id']}/decide", headers=bearer(owner["token"]),
               json={"decision": "deny", "decided_by": "role:the-cfo", "reason": "no"})
    assert r.status_code == 200, r.text
    moments = [json.loads(ln) for ln in escalation._workspace(ws)[1].read_text().splitlines()]
    assert {m["moment"]: m.get("decided_by") for m in moments}["denied"] == owner["principal"]


def test_orgpolicy_apply_is_approved_by_the_session(org_ws):
    c, ws, _, owner, _ = org_ws
    r = c.post(f"/workspaces/{ws}/orgpolicy/apply", headers=bearer(owner["token"]),
               json={"approved_by": "user:the-cfo"})
    assert r.status_code == 200, r.text
    log = (orgpolicy._target_dir(ws) / orgpolicy.LOG_FILE).read_text()
    assert json.loads(log.splitlines()[-1])["actor"] == owner["principal"]


def test_identity_fields_may_be_left_out_of_the_body(org_ws):
    c, ws, _, owner, member = org_ws
    action = orgpolicy.action_catalog(orgpolicy.read_org(ws))[0]
    r = c.post(f"/workspaces/{ws}/escalations", headers=bearer(member["token"]),
               json={"action": action, "object_id": "ITM-1", "denying_rule": "default-deny"})
    assert r.status_code == 201, r.text
    r = c.post(f"/workspaces/{ws}/escalations/{r.json()['id']}/decide", headers=bearer(owner["token"]),
               json={"decision": "deny"})
    assert r.status_code == 200, r.text
    assert c.post(f"/workspaces/{ws}/orgpolicy/apply", headers=bearer(owner["token"]), json={}).status_code == 200


@pytest.mark.parametrize("field", ["object_id", "denying_rule"])
def test_a_trailing_newline_does_not_pass_the_shape_checks(org_ws, field):
    c, ws, org, _, member = org_ws
    assert _escalate(c, ws, org, member["token"], **{field: "ITM-1\n"}).status_code == 422


# ------------------------------------------------------------------- path escapes

@pytest.fixture()
def planted(tmp_path, monkeypatch):
    """An org.json one level above the data root, and a symlink to it inside."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = {"secret": "outside the root", "data_owners": {"item": "x"}}
    (tmp_path / "org.json").write_text(json.dumps(secret))
    (outside / "org.json").write_text(json.dumps(secret))
    (root / "linked").symlink_to(outside)
    return root


@pytest.mark.parametrize("name", ["..", "../outside", "linked", "a/../..", ".", ""])
def test_orgpolicy_refuses_a_name_that_leaves_the_data_root(planted, name):
    with pytest.raises(FileNotFoundError):
        orgpolicy.read_org(name)
    with pytest.raises(FileNotFoundError):
        orgpolicy.applied_rules(name)


@pytest.mark.parametrize("name", ["..", "../outside", "linked", "a/../..", ".", ""])
def test_escalation_refuses_a_name_that_leaves_the_data_root(planted, name):
    with pytest.raises(HTTPException) as e:
        escalation._workspace(name)
    assert e.value.status_code == 404


def test_org_json_that_is_a_symlink_out_of_the_root_is_refused(planted, tmp_path):
    d = planted / "real-org"
    d.mkdir()
    (d / "org.json").symlink_to(tmp_path / "org.json")
    with pytest.raises(FileNotFoundError):
        orgpolicy.read_org("real-org")
    with pytest.raises(HTTPException):
        escalation._workspace("real-org")


def test_a_real_org_folder_still_resolves(planted):
    d = planted / "real-org"
    d.mkdir()
    (d / "org.json").write_text('{"data_owners": {}}')
    assert orgpolicy.read_org("real-org") == {"data_owners": {}}
    assert escalation._workspace("real-org") == ("organization", d / escalation.RECORD_FILE)


# ------------------------------------------------------------ SQL concurrency cap

def _query(c, token, sql="select 1"):
    return c.post("/workspaces/retail/lake/query", headers=bearer(token), json={"sql": sql})


def _fill_slots(org_id: str) -> None:
    for _ in range(sqlguard.MAX_CONCURRENT_PER_ORG):
        sqlguard.take_slot(org_id)


@pytest.fixture()
def two_orgs(hosted):
    a = signup(hosted, email="a@a.example", org="Org alpha").json()
    b = signup(hosted, email="b@b.example", org="Org beta").json()
    yield hosted, a, b
    assert sqlguard.slots_held() == {}


def test_an_org_at_its_sql_cap_gets_429_with_a_retry_after(two_orgs):
    c, a, _ = two_orgs
    _fill_slots(a["org_id"])
    try:
        r = _query(c, a["token"])
    finally:
        for _ in range(sqlguard.MAX_CONCURRENT_PER_ORG):
            sqlguard.give_slot(a["org_id"])
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_the_board_run_route_shares_the_cap(two_orgs):
    c, a, _ = two_orgs
    _fill_slots(a["org_id"])
    try:
        r = c.post("/workspaces/retail/boards/any/run", headers=bearer(a["token"]))
    finally:
        for _ in range(sqlguard.MAX_CONCURRENT_PER_ORG):
            sqlguard.give_slot(a["org_id"])
    assert r.status_code == 429


def test_one_orgs_cap_does_not_touch_another_org(two_orgs):
    c, a, b = two_orgs
    _fill_slots(a["org_id"])
    try:
        r = _query(c, b["token"])
    finally:
        for _ in range(sqlguard.MAX_CONCURRENT_PER_ORG):
            sqlguard.give_slot(a["org_id"])
    assert r.status_code != 429


def test_a_query_holds_a_slot_while_it_runs_and_gives_it_back(two_orgs, monkeypatch):
    c, a, _ = two_orgs
    seen = []
    real = sqlguard.run_capped

    def spying(*args, **kwargs):
        seen.append(sqlguard.slots_held())
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlguard, "run_capped", spying)
    r = _query(c, a["token"])
    assert seen == [{a["org_id"]: 1}], r.text
    assert sqlguard.slots_held() == {}


def test_a_handler_that_blows_up_still_gives_the_slot_back(two_orgs, monkeypatch):
    c, a, _ = two_orgs

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sqlguard, "guard_select", boom)
    assert _query(c, a["token"]).status_code == 500
    assert sqlguard.slots_held() == {}


def test_a_stranger_gets_the_401_and_takes_no_slot(two_orgs):
    c, _, _ = two_orgs
    assert c.post("/workspaces/retail/lake/query", json={"sql": "select 1"}).status_code == 401
    assert sqlguard.slots_held() == {}


# -------------------------------------------------------------- board tile cap

def _board(tiles: int) -> dict:
    return {"title": "Big", "tiles": [{"sql": "select 1", "viz": "table"} for _ in range(tiles)]}


@pytest.fixture()
def own_board_path(tmp_path, monkeypatch):
    """Scenario orgs have no lake, so no boards. A pack on the dev org has both."""
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path))
    with TestClient(harden(create_platform_app()), raise_server_exceptions=False) as c:
        yield c, {"token": "unused on a dev open server"}, "/workspaces/retail/boards/big"


def test_a_board_at_the_tile_cap_saves(own_board_path):
    c, a, path = own_board_path
    r = c.put(path, headers=bearer(a["token"]), json=_board(sqlguard.MAX_BOARD_TILES))
    assert r.status_code == 200, r.text
    assert len(r.json()["tiles"]) == sqlguard.MAX_BOARD_TILES


def test_a_board_one_over_the_tile_cap_is_refused_and_not_saved(own_board_path):
    c, a, path = own_board_path
    r = c.put(path, headers=bearer(a["token"]), json=_board(sqlguard.MAX_BOARD_TILES + 1))
    assert r.status_code == 422
    assert str(sqlguard.MAX_BOARD_TILES) in r.json()["detail"]
    assert c.get(path, headers=bearer(a["token"])).status_code == 404


def test_an_oversize_board_body_answers_413_not_500(own_board_path):
    c, a, path = own_board_path
    r = c.put(path, headers=bearer(a["token"]), content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413


def test_a_board_body_that_is_not_json_still_reaches_the_app(own_board_path):
    c, a, path = own_board_path
    r = c.put(path, headers={**bearer(a["token"]), "content-type": "application/json"}, content=b"{nope")
    assert r.status_code == 422


# -------------------------------------------------------- DuckDB path leak

def _capped_locked():
    con = duckdb.connect()
    sqlguard.apply_limits(con)
    con.execute("set enable_external_access = false")
    con.execute("set lock_configuration = true")
    return con


def test_duckdb_settings_shows_no_host_path_on_a_capped_connection():
    con = _capped_locked()
    try:
        values = [str(v) for _, v in con.execute("select name, value from duckdb_settings()").fetchall()]
    finally:
        con.close()
    home = os.path.expanduser("~")
    assert not [v for v in values if home in v or v.startswith("/") or ".duckdb" in v]


@pytest.mark.parametrize("name", ["home_directory", "extension_directory", "secret_directory", "temp_directory"])
def test_current_setting_shows_no_host_path_on_a_capped_connection(name):
    con = _capped_locked()
    try:
        assert not con.execute(f"select current_setting('{name}')").fetchone()[0]
    finally:
        con.close()


# ------------------------------------------------------------- repo posture

def test_makefile_static_servers_bind_loopback():
    servers = [ln for ln in (REPO / "Makefile").read_text().splitlines() if "http.server" in ln]
    assert len(servers) == 2
    for line in servers:
        assert "--bind 127.0.0.1" in line, line


def test_mcp_is_pinned_to_2_and_the_lock_agrees():
    assert '"mcp>=2"' in (REPO / "pyproject.toml").read_text()
    lock = (REPO / "uv.lock").read_text()
    assert '{ name = "mcp", specifier = ">=2" }' in lock
    assert re.search(r'name = "mcp"\nversion = "2\.', lock)


def test_threat_model_is_written_for_the_hosted_product():
    text = (REPO / "docs" / "threat-model.md").read_text()
    for actor in ("Anonymous", "member of org A", "Malicious upload", "Prompt injection", "Compromised worker"):
        assert actor in text, actor
    assert "out of scope, and\nnothing here claims" not in text
    # A control is proved by a test name, not by a promise.
    section = text.split("## Controls", 1)[1].split("\n## ", 1)[0]
    assert "tests/test_security_mt.py::" in section
