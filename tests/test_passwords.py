"""Tier 1 item 4 of docs/enterprise-security.md: at least 12 characters, not on the
offline breached list that ships in the wheel, no composition rules. Applied where a
password is set, never at sign in."""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ironwright import auth
from ironwright.security import passwords
from ironwright.tenancy import db, service

ROOT = Path(__file__).resolve().parents[1]
GOOD = "correct-horse-9"


@pytest.fixture()
def client(meta_db):
    from ironwright.platform_api import create_platform_app
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


def signup(c, password, email="o@a.example"):
    return c.post("/auth/signup", json={"email": email, "password": password, "org_name": "Acme"})


def test_eleven_characters_is_too_short(client):
    r = signup(client, "abcdefghijk")
    assert r.status_code == 422 and "12 characters" in r.json()["detail"]


def test_twelve_plain_lowercase_letters_are_fine(client):
    assert signup(client, "plainlettersonly").status_code == 201


@pytest.mark.parametrize("pw", ["qwerty123456", "1q2w3e4r5t6y", "QWERTY123456"])
def test_a_breached_password_is_refused_whatever_its_case(client, pw):
    r = signup(client, pw)
    assert r.status_code == 422 and "breached" in r.json()["detail"]


def test_the_list_is_the_top_100k(meta_db):
    # 99,840 lines, 97,746 once case is folded.
    assert 97_000 <= passwords.breached_count() <= 100_000
    assert passwords.is_breached("password") and not passwords.is_breached(GOOD)


def test_a_new_member_accepting_an_invite_gets_the_policy(client):
    owner = signup(client, GOOD).json()
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers={"Authorization": f"Bearer {owner['token']}"},
                    json={"email": "m@a.example", "role": "member"})
    joined = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": "qwerty123456"})
    assert joined.status_code == 422 and "breached" in joined.json()["detail"]


def test_auth_set_gets_the_policy(meta_db):
    with pytest.raises(ValueError, match="12 characters"):
        auth.set_account("op@a.example", "short-one")


def test_an_older_short_password_still_signs_in(client):
    """The policy is for new passwords. Nobody is locked out by it."""
    assert signup(client, GOOD).status_code == 201
    with db.engine().begin() as con:
        con.execute(db.users.update().where(db.users.c.email == "o@a.example")
                    .values(pw_hash=service.hash_password("old-pass")))
    assert client.post("/auth/login", json={"email": "o@a.example", "password": "old-pass"}).status_code == 200


def test_the_list_ships_in_the_wheel(tmp_path):
    """A real wheel build. An editable install would find the file in the tree and prove nothing."""
    out = tmp_path / "wheel"
    uv = shutil.which("uv")
    cmd = ([uv, "build", "--wheel", "-q", "-o", str(out), str(ROOT)] if uv else
           [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out), str(ROOT)])
    subprocess.run(cmd, check=True, capture_output=True, cwd=tmp_path)
    wheel = next(out.glob("ironwright-*.whl"))
    assert "ironwright/security/breached-100k.txt.gz" in zipfile.ZipFile(wheel).namelist()


def test_no_password_in_the_repo_seeds_is_too_weak():
    """Scripts and tests that set a password must pass the policy themselves."""
    for pw in ("correct-horse-9", "correct horse battery", "seed-password-for-a-test", "a-new-password-1"):
        passwords.check(pw)
