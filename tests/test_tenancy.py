"""Contract 1 of docs/saas-mvp.md: orgs, users, roles, sessions, invites.

Every test here runs on SQLite and on Postgres through the meta_db fixture.
"""
from __future__ import annotations

import re
import stat

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.tenancy import db, service
from ironwright.tenancy.models import DEV_ORG_ID, Ctx

PASSWORD = "correct-horse-9"
HOUR = 3600


@pytest.fixture()
def client(meta_db):
    from ironwright.platform_api import create_platform_app
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup(c, email="owner@a.example", org="Acme Claims"):
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def join(c, session, email, role):
    """A real invite and a real accept, so roles below never come from a shortcut."""
    r = c.post(f"/orgs/{session['org_slug']}/invites", headers=bearer(session["token"]),
               json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    r = c.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def rows(table, *where):
    with db.engine().connect() as con:
        return con.execute(sa.select(table).where(*where)).mappings().all()


# ----- the store

def test_create_all_writes_the_contract_tables_and_a_schema_version(meta_db):
    names = set(sa.inspect(db.engine()).get_table_names())
    assert {"orgs", "users", "memberships", "sessions", "invites", "schema_version"} <= names
    assert [r["version"] for r in rows(db.schema_version)] == [db.SCHEMA_VERSION]


def test_opening_the_store_twice_keeps_one_schema_version_row(meta_db):
    db.engine()
    db.init(db.engine())
    assert len(rows(db.schema_version)) == 1


def test_the_engine_follows_the_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "one"))
    first = db.engine()
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "two"))
    assert db.engine().url != first.url
    assert (tmp_path / "one" / "meta.sqlite").exists() and (tmp_path / "two" / "meta.sqlite").exists()


def test_the_default_store_file_is_private(monkeypatch, tmp_path):
    monkeypatch.delenv("IRONWRIGHT_DB_URL", raising=False)
    monkeypatch.setenv("IRONWRIGHT_DATA", str(tmp_path / "root"))
    db.engine()
    assert stat.S_IMODE((tmp_path / "root" / "meta.sqlite").stat().st_mode) == 0o600


@pytest.mark.parametrize("given,want", [
    ("postgresql://u@h/d", "postgresql+psycopg://u@h/d"),
    ("postgres://u@h/d", "postgresql+psycopg://u@h/d"),
    ("postgresql+psycopg://u@h/d", "postgresql+psycopg://u@h/d"),
    ("sqlite:////x/meta.sqlite", "sqlite:////x/meta.sqlite"),
])
def test_a_plain_postgres_url_uses_the_driver_we_ship(given, want):
    assert db.normalize_url(given) == want


def test_tearing_down_a_test_store_closes_its_connections(meta_db):
    eng = db.engine()
    with eng.connect():
        pass
    db.dispose_current()
    assert eng.pool.checkedin() == 0
    assert db.engine() is not eng


# ----- signup

def test_signup_creates_the_org_and_its_owner(client):
    s = signup(client)
    assert s["role"] == "owner" and s["org_slug"] == "acme-claims"
    assert re.fullmatch(r"[0-9a-f]{32}", s["org_id"]) and re.fullmatch(r"[0-9a-f]{32}", s["user_id"])
    assert s["principal"] == f"user:{s['user_id']}"
    assert [m["role"] for m in rows(db.memberships)] == ["owner"]


def test_signup_slugs_always_match_the_contract_and_never_collide(client):
    seen = set()
    for i, name in enumerate(["Acme", "Acme", "!!", "9 lives", "x" * 80, "Dev"]):
        slug = signup(client, email=f"u{i}@a.example", org=name)["org_slug"]
        assert re.fullmatch(r"[a-z][a-z0-9-]{2,39}", slug), slug
        seen.add(slug)
    assert len(seen) == 6
    assert "dev" not in seen


@pytest.mark.parametrize("body", [
    {"email": "nope", "password": PASSWORD, "org_name": "A org"},
    {"email": "a@b.example", "password": "short", "org_name": "A org"},
    {"email": "a@b.example", "password": PASSWORD, "org_name": "   "},
])
def test_signup_refuses_bad_input_and_leaves_nothing_behind(client, body):
    assert client.post("/auth/signup", json=body).status_code == 422
    assert not rows(db.users) and not rows(db.orgs)


def test_a_second_signup_with_the_same_email_makes_nothing(client):
    signup(client, email="Same@A.example")
    r = client.post("/auth/signup", json={"email": "same@a.example ", "password": PASSWORD,
                                          "org_name": "Other"})
    assert r.status_code == 409
    assert len(rows(db.users)) == 1 and len(rows(db.orgs)) == 1


# ----- login and sessions

def test_login_returns_a_session_for_the_default_org(client):
    first = signup(client)
    s = client.post("/auth/login", json={"email": "OWNER@a.example", "password": PASSWORD}).json()
    assert s["org_id"] == first["org_id"] and s["token"] != first["token"]


def test_the_default_org_is_the_oldest_one_the_user_belongs_to(client):
    home = signup(client, email="both@a.example", org="Home org")
    other = signup(client, email="other@a.example", org="Other org")
    join(client, other, "both@a.example", "viewer")
    s = client.post("/auth/login", json={"email": "both@a.example", "password": PASSWORD}).json()
    assert s["org_id"] == home["org_id"]
    assert {o["slug"] for o in s["orgs"]} == {home["org_slug"], other["org_slug"]}


@pytest.mark.parametrize("email,password", [("owner@a.example", "wrong-password"),
                                            ("nobody@a.example", PASSWORD)])
def test_a_bad_login_is_401_with_one_message(client, email, password):
    signup(client)
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 401 and "token" not in r.json()
    assert r.json()["detail"] == "that email and password do not match"


def test_me_returns_the_ctx(client):
    s = signup(client)
    me = client.get("/auth/me", headers=bearer(s["token"])).json()
    want = Ctx(org_id=s["org_id"], org_slug=s["org_slug"], user_id=s["user_id"], role="owner",
               principal=f"user:{s['user_id']}")
    assert {k: me[k] for k in ("org_id", "org_slug", "user_id", "role", "principal")} == want.__dict__
    assert me["email"] == "owner@a.example"


def test_tokens_and_passwords_are_stored_hashed(client):
    s = signup(client)
    assert rows(db.sessions)[0]["token_hash"] != s["token"]
    assert s["token"] not in rows(db.sessions)[0]["token_hash"]
    pw = rows(db.users)[0]["pw_hash"]
    assert PASSWORD not in pw and pw.startswith("pbkdf2_sha256$")


def test_the_same_password_hashes_differently_per_user(client):
    signup(client, email="a@a.example", org="Org one")
    signup(client, email="b@a.example", org="Org two")
    a, b = (r["pw_hash"] for r in rows(db.users))
    assert a != b


def test_a_session_expires_after_8h_idle_and_its_row_goes(client, monkeypatch):
    s = signup(client)
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + 8 * HOUR + 1)
    assert client.get("/auth/me", headers=bearer(s["token"])).status_code == 401
    assert not rows(db.sessions)


def test_activity_slides_the_expiry_and_it_survives_a_new_app(client, monkeypatch):
    from ironwright.platform_api import create_platform_app
    s = signup(client)
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + 8 * HOUR - 60)
    assert client.get("/auth/me", headers=bearer(s["token"])).status_code == 200
    monkeypatch.setattr(auth, "_now", lambda: start + 16 * HOUR - 120)
    with TestClient(create_platform_app()) as restarted:
        assert restarted.get("/auth/me", headers=bearer(s["token"])).status_code == 200


def test_login_rotates_and_logout_revokes(client):
    s = signup(client)
    again = client.post("/auth/login", json={"email": "owner@a.example", "password": PASSWORD}).json()
    assert client.get("/auth/me", headers=bearer(s["token"])).status_code == 401
    assert client.post("/auth/logout", headers=bearer(again["token"])).status_code == 200
    assert client.get("/auth/me", headers=bearer(again["token"])).status_code == 401
    assert not rows(db.sessions)


def test_a_removed_member_loses_the_session_at_once(client):
    owner = signup(client)
    m = join(client, owner, "m@a.example", "member")
    with db.engine().begin() as con:
        con.execute(db.memberships.delete().where(db.memberships.c.user_id == m["user_id"]))
    assert client.get("/auth/me", headers=bearer(m["token"])).status_code == 401


def test_a_role_change_applies_to_a_live_session(client):
    owner = signup(client)
    m = join(client, owner, "m@a.example", "admin")
    with db.engine().begin() as con:
        con.execute(db.memberships.update().where(db.memberships.c.user_id == m["user_id"])
                    .values(role="viewer"))
    assert client.get("/auth/me", headers=bearer(m["token"])).json()["role"] == "viewer"


# ----- switching org

def test_switch_rotates_the_token_into_the_other_org(client):
    home = signup(client, email="both@a.example", org="Home org")
    other = signup(client, email="other@a.example", org="Other org")
    join(client, other, "both@a.example", "viewer")
    login = client.post("/auth/login", json={"email": "both@a.example", "password": PASSWORD}).json()
    r = client.post(f"/orgs/{other['org_slug']}/switch", headers=bearer(login["token"]))
    assert r.status_code == 200, r.text
    assert r.json()["org_id"] == other["org_id"] and r.json()["role"] == "viewer"
    assert client.get("/auth/me", headers=bearer(login["token"])).status_code == 401
    back = client.post(f"/orgs/{home['org_slug']}/switch", headers=bearer(r.json()["token"]))
    assert back.json()["role"] == "owner"


def test_switching_to_an_org_you_are_not_in_is_404(client):
    a, b = signup(client), signup(client, email="b@b.example", org="Beta org")
    assert client.post(f"/orgs/{a['org_slug']}/switch", headers=bearer(b["token"])).status_code == 404
    assert client.get("/auth/me", headers=bearer(b["token"])).json()["org_id"] == b["org_id"]


# ----- invites

def test_an_invite_token_is_hashed_single_use_and_makes_the_role_it_named(client):
    owner = signup(client)
    token = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                        json={"email": "new@a.example", "role": "member"}).json()["token"]
    assert rows(db.invites)[0]["token_hash"] != token
    r = client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "member"
    assert r.json()["org_id"] == owner["org_id"]
    again = client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD})
    assert again.status_code == 404 and not rows(db.invites)


def test_accepting_as_an_existing_user_needs_that_users_password(client):
    owner = signup(client)
    signup(client, email="exists@a.example", org="Their own org")
    token = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                        json={"email": "exists@a.example", "role": "member"}).json()["token"]
    bad = client.post("/auth/accept-invite", json={"token": token, "password": "wrong-password"})
    assert bad.status_code == 401
    assert len(rows(db.invites)) == 1
    assert client.post("/auth/accept-invite",
                       json={"token": token, "password": PASSWORD}).json()["org_id"] == owner["org_id"]


def test_an_expired_invite_is_404_and_a_garbage_one_looks_the_same(client, monkeypatch):
    owner = signup(client)
    token = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                        json={"email": "late@a.example", "role": "member"}).json()["token"]
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + service.INVITE_SECONDS + 1)
    late = client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD})
    garbage = client.post("/auth/accept-invite", json={"token": "nope", "password": PASSWORD})
    assert late.status_code == garbage.status_code == 404
    assert late.json() == garbage.json()
    assert not rows(db.users, db.users.c.email == "late@a.example")


def test_inviting_again_replaces_the_older_token(client):
    owner = signup(client)
    def invite():
        return client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner["token"]),
                           json={"email": "new@a.example", "role": "member"}).json()["token"]
    old, new = invite(), invite()
    assert client.post("/auth/accept-invite", json={"token": old, "password": PASSWORD}).status_code == 404
    assert client.post("/auth/accept-invite", json={"token": new, "password": PASSWORD}).status_code == 200


def test_accepting_never_lowers_a_role_someone_already_has(client):
    owner = signup(client)
    admin = join(client, owner, "m@a.example", "admin")
    join(client, owner, "m@a.example", "viewer")
    assert client.get("/auth/me", headers=bearer(admin["token"])).status_code == 401  # rotated
    s = client.post("/auth/login", json={"email": "m@a.example", "password": PASSWORD}).json()
    assert s["role"] == "admin"


@pytest.mark.parametrize("inviter,role,want", [
    ("viewer", "viewer", 403), ("member", "viewer", 403),
    ("admin", "member", 201), ("admin", "admin", 201), ("admin", "owner", 403),
    ("owner", "owner", 201), ("owner", "superuser", 422),
])
def test_who_can_invite_whom(client, inviter, role, want):
    owner = signup(client)
    s = owner if inviter == "owner" else join(client, owner, f"{inviter}@a.example", inviter)
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(s["token"]),
                    json={"email": "target@a.example", "role": role})
    assert r.status_code == want, r.text


# ----- the dev flag

def test_dev_open_maps_to_a_fixed_dev_org_without_touching_the_store(meta_db, monkeypatch):
    from ironwright.platform_api import create_platform_app
    monkeypatch.setenv("IRONWRIGHT_DEV_OPEN", "1")
    with TestClient(create_platform_app()) as c:
        me = c.get("/auth/me").json()
        assert me["org_id"] == DEV_ORG_ID and me["role"] == "owner" and me["org_slug"] == "dev"
        assert me["principal"] == auth.CONSOLE_PRINCIPAL
        assert c.get("/auth/me", headers={"X-Principal": "vendor:V-1"}).json()["principal"] == "vendor:V-1"


def test_set_account_seeds_an_owner_in_the_dev_org(client):
    assert client.get("/auth/status").json()["seeded"] is False
    auth.set_account("op@a.example", PASSWORD)
    assert client.get("/auth/status").json()["seeded"] is True
    s = client.post("/auth/login", json={"email": "op@a.example", "password": PASSWORD}).json()
    assert s["org_id"] == DEV_ORG_ID and s["role"] == "owner"
    auth.set_account("op@a.example", "a-new-password-1")
    assert client.post("/auth/login", json={"email": "op@a.example", "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": "op@a.example",
                                            "password": "a-new-password-1"}).status_code == 200


# ----- rate limits

@pytest.mark.parametrize("path,body", [
    ("/auth/login", {"email": "x@a.example", "password": "wrong-password"}),
    ("/auth/signup", {"email": "bad", "password": "x", "org_name": "y"}),
    ("/auth/accept-invite", {"token": "nope", "password": "x"}),
])
def test_door_routes_are_rate_limited_per_ip(client, monkeypatch, path, body):
    answered = [client.post(path, json=body).status_code for _ in range(auth.AUTH_ATTEMPTS)]
    assert 429 not in answered
    limited = client.post(path, json=body)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    other = TestClient(client.app, client=("10.0.0.9", 50000))
    assert other.post(path, json=body).status_code != 429
    start = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: start + auth.AUTH_WINDOW + 1)
    assert client.post(path, json=body).status_code != 429


def test_a_forwarded_header_cannot_dodge_the_limit(client):
    body = {"email": "x@a.example", "password": "wrong-password"}
    codes = [client.post("/auth/login", json=body, headers={"X-Forwarded-For": f"10.1.1.{i}"}).status_code
             for i in range(auth.AUTH_ATTEMPTS + 1)]
    assert codes[-1] == 429
