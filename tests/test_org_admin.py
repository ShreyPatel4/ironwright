"""Org admin and data lifecycle: members, the audit chain, export, delete.

The delete proof reads the live schema and walks the live disk, so a table or a
folder added next month is covered or this file goes red. A list of known tables
here would only prove what I already remembered.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from ironwright import hosted
from ironwright import workspaces_tenant as wt
from ironwright.config import data_root, ensure_tenant, tenant_dir
from ironwright.jobs.store import JobStore
from ironwright.jobs.worker import Worker
from ironwright.platform_api import NOT_FOUND, create_platform_app
from ironwright.tenancy import db, service, usage
from ironwright.tenancy.models import DEV_ORG_ID, DEV_ORG_SLUG

PASSWORD = "correct-horse-9"
FIXTURE = Path(__file__).parent / "fixtures" / "tenant_ws"
NOON = 1_790_000_000.0


def bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['token']}"}


@pytest.fixture()
def client(meta_db, monkeypatch):
    monkeypatch.setenv("IRONWRIGHT_SECRET_KEY", Fernet.generate_key().decode())
    with TestClient(create_platform_app(), raise_server_exceptions=False) as c:
        yield c


def signup(c, email="owner@a.example", org="Canary Alpha") -> dict:
    r = c.post("/auth/signup", json={"email": email, "password": PASSWORD, "org_name": org})
    assert r.status_code == 201, r.text
    return r.json()


def join(c, owner, email, role) -> dict:
    r = c.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner), json={"email": email, "role": role})
    assert r.status_code == 201, r.text
    r = c.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def members(c, who, slug=None) -> dict:
    r = c.get(f"/orgs/{slug or who['org_slug']}/members", headers=bearer(who))
    assert r.status_code == 200, r.text
    return r.json()


def role_of(c, who, user_id) -> str:
    return next(m["role"] for m in members(c, who)["members"] if m["user_id"] == user_id)


def set_role(c, who, target, role, slug=None):
    return c.put(f"/orgs/{slug or who['org_slug']}/members/{target['user_id']}",
                 headers=bearer(who), json={"role": role})


def remove(c, who, target, slug=None):
    return c.delete(f"/orgs/{slug or who['org_slug']}/members/{target['user_id']}", headers=bearer(who))


# ---------------------------------------------------------------- members

def test_an_admin_lists_members_and_open_invites(client):
    owner = signup(client)
    admin = join(client, owner, "admin@a.example", "admin")
    assert client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                       json={"email": "waiting@a.example", "role": "viewer"}).status_code == 201
    got = members(client, admin)
    assert {(m["email"], m["role"]) for m in got["members"]} == {("owner@a.example", "owner"),
                                                                 ("admin@a.example", "admin")}
    assert [(i["email"], i["role"]) for i in got["invites"]] == [("waiting@a.example", "viewer")]
    assert "token" not in json.dumps(got) and "pw_hash" not in json.dumps(got)


def test_an_expired_invite_is_not_listed(client):
    owner = signup(client)
    service.invite(owner["org_slug"], "old@a.example", "viewer", NOON - 30 * 86_400)
    assert members(client, owner)["invites"] == []


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_below_admin_cannot_read_the_member_list(client, role):
    owner = signup(client)
    who = join(client, owner, f"{role}@a.example", role)
    r = client.get(f"/orgs/{owner['org_slug']}/members", headers=bearer(who))
    assert r.status_code == 403


def test_an_owner_changes_a_role_and_it_applies_at_once(client):
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    assert client.post("/workspaces", headers=bearer(who), json={"name": "nope-ws"}).status_code == 403
    r = set_role(client, owner, who, "admin")
    assert r.status_code == 200, r.text
    assert role_of(client, owner, who["user_id"]) == "admin"
    assert client.post("/workspaces", headers=bearer(who), json={"name": "now-ok"}).status_code == 201


def test_an_unknown_role_is_refused(client):
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    assert set_role(client, owner, who, "emperor").status_code == 422
    assert role_of(client, owner, who["user_id"]) == "member"


def test_the_last_owner_cannot_be_demoted(client):
    owner = signup(client)
    r = set_role(client, owner, owner, "admin")
    assert r.status_code == 409
    assert "last owner" in r.json()["detail"]
    assert role_of(client, owner, owner["user_id"]) == "owner"


def test_the_last_owner_cannot_be_removed(client):
    owner = signup(client)
    r = remove(client, owner, owner)
    assert r.status_code == 409
    assert "last owner" in r.json()["detail"]
    assert role_of(client, owner, owner["user_id"]) == "owner"


def test_with_a_second_owner_the_first_can_step_down(client):
    owner = signup(client)
    second = join(client, owner, "second@a.example", "owner")
    assert set_role(client, owner, owner, "member").status_code == 200
    assert remove(client, second, owner).status_code == 200
    assert [m["email"] for m in members(client, second)["members"]] == ["second@a.example"]


def test_two_owners_demoting_each_other_at_once_leave_one(client):
    """Each one reads two owners unless the check and the write are one step."""
    owner = signup(client)
    second = join(client, owner, "second@a.example", "owner")
    for _ in range(5):
        go = threading.Barrier(2)
        answers = []

        threads = [threading.Thread(target=_demote_at_once, args=(go, answers, owner["org_id"], *pair))
                   for pair in ((owner, second), (second, owner))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with usage.engine().begin() as con:
            owners = con.execute(sa.select(sa.func.count()).select_from(db.memberships).where(
                db.memberships.c.org_id == owner["org_id"], db.memberships.c.role == "owner")).scalar_one()
            assert owners == 1, answers
            assert sorted(answers) == ["ok", "refused"]
            # Both owners again, for the next round.
            con.execute(db.memberships.update().where(db.memberships.c.org_id == owner["org_id"]).values(role="owner"))


def _demote_at_once(go, answers, org_id, who, target) -> None:
    go.wait()
    try:
        service.set_role(org_id, target["user_id"], "member", who["user_id"], who["principal"], NOON)
        answers.append("ok")
    except (service.LastOwner, service.Forbidden):
        answers.append("refused")


def test_an_admin_manages_members_but_not_owners(client):
    owner = signup(client)
    admin = join(client, owner, "admin@a.example", "admin")
    who = join(client, owner, "m@a.example", "member")
    assert set_role(client, admin, who, "viewer").status_code == 200
    assert remove(client, admin, who).status_code == 200
    assert set_role(client, admin, owner, "viewer").status_code == 403
    assert remove(client, admin, owner).status_code == 403
    assert role_of(client, owner, owner["user_id"]) == "owner"


def test_an_admin_cannot_make_an_owner(client):
    owner = signup(client)
    admin = join(client, owner, "admin@a.example", "admin")
    who = join(client, owner, "m@a.example", "member")
    assert set_role(client, admin, who, "owner").status_code == 403
    assert set_role(client, admin, admin, "owner").status_code == 403
    assert role_of(client, owner, who["user_id"]) == "member"


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_below_admin_cannot_change_anyone(client, role):
    owner = signup(client)
    who = join(client, owner, f"{role}@a.example", role)
    other = join(client, owner, "other@a.example", "viewer")
    assert set_role(client, who, other, "member").status_code == 403
    assert remove(client, who, other).status_code == 403


def test_removal_ends_the_session_and_the_account_of_someone_with_no_org_left(client):
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    assert client.get("/auth/me", headers=bearer(who)).status_code == 200
    assert remove(client, owner, who).status_code == 200
    assert client.get("/auth/me", headers=bearer(who)).status_code == 401
    assert client.post("/auth/login", json={"email": "m@a.example", "password": PASSWORD}).status_code == 401
    with usage.engine().connect() as con:
        assert con.execute(sa.select(db.sessions).where(db.sessions.c.user_id == who["user_id"])).first() is None
        assert con.execute(sa.select(db.users).where(db.users.c.id == who["user_id"])).first() is None
    # The email is free again, so they can start over.
    assert client.post("/auth/signup", json={"email": "m@a.example", "password": PASSWORD,
                                             "org_name": "Fresh start"}).status_code == 201


def test_removal_keeps_the_account_of_someone_in_another_org(client):
    owner = signup(client)
    other = signup(client, "other@b.example", "Beta org")
    both = join(client, other, "both@b.example", "member")
    r = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                    json={"email": "both@b.example", "role": "member"})
    in_a = client.post("/auth/accept-invite", json={"token": r.json()["token"], "password": PASSWORD}).json()
    assert remove(client, owner, in_a).status_code == 200
    back = client.post("/auth/login", json={"email": "both@b.example", "password": PASSWORD})
    assert back.status_code == 200
    assert [o["slug"] for o in back.json()["orgs"]] == [other["org_slug"]]
    assert both["user_id"] == in_a["user_id"]


def test_a_missing_member_is_a_plain_404_and_not_the_gates_answer(client):
    owner = signup(client)
    other = signup(client, "other@b.example", "Beta org")
    for target in ({"user_id": "1"}, other):
        for r in (set_role(client, owner, target, "viewer"), remove(client, owner, target)):
            assert r.status_code == 404
            assert r.json() != {"detail": NOT_FOUND}
    assert role_of(client, other, other["user_id"]) == "owner"


def test_another_org_gets_the_gates_404_on_all_of_it(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta org")
    a_member = join(client, a, "m@a.example", "member")
    slug = a["org_slug"]
    answers = [client.get(f"/orgs/{slug}/members", headers=bearer(b)),
               set_role(client, b, a_member, "viewer", slug), remove(client, b, a_member, slug),
               client.get(f"/orgs/{slug}/audit/chain", headers=bearer(b)),
               client.post(f"/orgs/{slug}/exports", headers=bearer(b)),
               client.get(f"/orgs/{slug}/exports", headers=bearer(b)),
               client.get(f"/orgs/{slug}/exports/1/download", headers=bearer(b)),
               client.post(f"/orgs/{slug}/delete", headers=bearer(b), json={"confirm": slug})]
    for r in answers:
        assert (r.status_code, r.json()) == (404, {"detail": NOT_FOUND})
    assert role_of(client, a, a_member["user_id"]) == "member"


def test_member_changes_land_in_the_audit_chain_with_who_did_it(client):
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    set_role(client, owner, who, "viewer")
    remove(client, owner, who)
    rows = chain(client, owner)["rows"]
    done = {r["action"]: r for r in rows}
    assert done["member.role"]["actor"] == owner["principal"]
    assert done["member.role"]["detail"] == {"user_id": who["user_id"], "from": "member", "to": "viewer"}
    assert done["member.remove"]["detail"]["user_id"] == who["user_id"]
    assert "member.invite" in done and "member.join" in done


# ---------------------------------------------------------------- the audit chain

def chain(c, who, **params) -> dict:
    r = c.get(f"/orgs/{who['org_slug']}/audit/chain", headers=bearer(who), params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _events(org_id, n=4, actor="user:x"):
    with usage.engine().begin() as con:
        for i in range(n):
            usage.audit(con, org_id, actor, "test.event", {"i": i}, NOON + i)


def test_the_chain_verifies_when_nothing_was_touched(client):
    owner = signup(client)
    _events(owner["org_id"])
    got = chain(client, owner)
    assert got["verified"] is True and got["broken_at"] is None
    assert got["total"] == 4 and len(got["rows"]) == 4
    assert all(r["hash"] for r in got["rows"])


def test_an_edited_row_breaks_the_chain_at_that_row(client):
    owner = signup(client)
    _events(owner["org_id"])
    rows = chain(client, owner)["rows"]
    with usage.engine().begin() as con:
        con.execute(usage.org_audit.update().where(usage.org_audit.c.seq == rows[1]["seq"])
                    .values(detail='{"i": 99}'))
    got = chain(client, owner)
    assert got["verified"] is False and got["broken_at"] == rows[1]["seq"]


def test_a_deleted_row_breaks_the_chain(client):
    owner = signup(client)
    _events(owner["org_id"])
    rows = chain(client, owner)["rows"]
    with usage.engine().begin() as con:
        con.execute(usage.org_audit.delete().where(usage.org_audit.c.seq == rows[1]["seq"]))
    got = chain(client, owner)
    assert got["verified"] is False and got["broken_at"] == rows[2]["seq"]


def test_a_row_slipped_in_without_a_link_breaks_the_chain(client):
    owner = signup(client)
    _events(owner["org_id"], 2)
    with usage.engine().begin() as con:
        con.execute(usage.org_audit.insert().values(org_id=owner["org_id"], ts=NOON, actor="user:x",
                                                    action="test.event", detail="{}"))
    assert chain(client, owner)["verified"] is False


def test_rows_from_before_the_chain_are_counted_and_not_called_verified(client):
    owner = signup(client)
    with usage.engine().begin() as con:
        con.execute(usage.org_audit.insert().values(org_id=owner["org_id"], ts=NOON, actor="user:old",
                                                    action="old.event", detail="{}"))
    _events(owner["org_id"], 2)
    got = chain(client, owner)
    assert got["verified"] is True and got["unchained"] == 1 and got["total"] == 3
    assert got["rows"][0]["hash"] is None


def test_filters_narrow_the_rows_and_not_the_verdict(client):
    owner = signup(client)
    _events(owner["org_id"], 2, actor="user:x")
    _events(owner["org_id"], 1, actor="user:y")
    with usage.engine().begin() as con:
        usage.audit(con, owner["org_id"], "user:x", "other.event", {}, NOON)
    assert len(chain(client, owner, actor="user:x")["rows"]) == 3
    assert len(chain(client, owner, action="other.event")["rows"]) == 1
    assert len(chain(client, owner, actor="user:x", action="test.event")["rows"]) == 2
    assert chain(client, owner, actor="user:nobody")["rows"] == []
    got = chain(client, owner, actor="user:y")
    assert got["total"] == 4 and got["verified"] is True
    assert set(got["actors"]) == {"user:x", "user:y"}
    assert set(got["actions"]) == {"test.event", "other.event"}


def test_one_orgs_chain_is_its_own(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta org")
    _events(a["org_id"], 2, actor="user:only-a")
    _events(b["org_id"], 2, actor="user:only-b")
    got = chain(client, b)
    assert got["verified"] is True and "user:only-a" not in json.dumps(got)


def test_concurrent_writers_do_not_fork_the_chain(client):
    owner = signup(client)
    threads = [threading.Thread(target=_events, args=(owner["org_id"], 3, f"user:{n}")) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = chain(client, owner)
    assert got["total"] == 12 and got["verified"] is True


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_below_admin_cannot_read_the_chain(client, role):
    owner = signup(client)
    who = join(client, owner, f"{role}@a.example", role)
    assert client.get(f"/orgs/{owner['org_slug']}/audit/chain", headers=bearer(who)).status_code == 403


# ---------------------------------------------------------------- a real org to export and delete

def _tenant_workspace(org_id: str, slug: str = "claims") -> None:
    root = ensure_tenant(org_id)
    if not (root / "uploads" / "fixture").exists():
        shutil.copytree(FIXTURE / "uploads" / "fixture", root / "uploads" / "fixture")
    models = {layer: {f.name: f.read_text() for f in (FIXTURE / "models" / layer).glob("*.sql")}
              for layer in ("silver", "gold")}
    wt.create_workspace(org_id, slug, yaml.safe_load((FIXTURE / "workspace.yaml").read_text()), models=models,
                        ontology=yaml.safe_load((FIXTURE / "ontology.yaml").read_text()),
                        policies=yaml.safe_load((FIXTURE / "policies.yaml").read_text()))
    assert wt.run_workspace(org_id, slug)["passed"]


def _rich_org(c, email="canary-owner@a.example", org="Canary Alpha") -> dict:
    """An org that has touched every table and folder a tenant can reach."""
    owner = signup(c, email, org)
    h = bearer(owner)
    _tenant_workspace(owner["org_id"])
    join(c, owner, f"canary-member-{owner['org_id'][:6]}@a.example", "member")
    assert c.post(f"/orgs/{owner['org_slug']}/invites", headers=h,
                  json={"email": f"canary-invited-{owner['org_id'][:6]}@a.example", "role": "viewer"}).status_code == 201
    made = c.post("/uploads", headers=h, files=[("files", ("canary.csv", b"id,name\n1,ann\n"))])
    assert made.status_code == 201, made.text
    made = c.post("/workspaces", headers=h, json={"name": "canary-scenario", "size": 4, "seed": 3})
    assert made.status_code == 201, made.text
    scenario = made.json()["name"]
    # Generated policy and escalation records write into the scenario's own folder.
    done = c.post(f"/workspaces/{scenario}/orgpolicy/apply", headers=h, json={"approved_by": owner["principal"]})
    assert done.status_code == 200, done.text
    asked = c.post(f"/workspaces/{scenario}/escalations", headers=h,
                   json={"principal": owner["principal"], "action": "delist_item", "object_id": "canary-object",
                         "denying_rule": "default-deny"})
    assert asked.status_code in (200, 201), asked.text
    assert c.put(f"/orgs/{owner['org_slug']}/llm-key", headers=h,
                 json={"provider": "anthropic", "key": "sk-canary-" + "x" * 30}).status_code == 200
    assert c.post("/jobs", headers=h, json={"kind": "run", "workspace": "claims"}).status_code == 200
    Worker(JobStore()).run_once()
    usage.add(owner["org_id"], "runs", NOON, 1)
    with usage.engine().begin() as con:
        con.execute(usage.org_plans.insert().values(org_id=owner["org_id"], plan="team"))
    return owner


def _needles(c, owner) -> tuple[str, ...]:
    """Everything that would identify this org if it were left behind."""
    got = members(c, owner)
    emails = [m["email"] for m in got["members"]] + [i["email"] for i in got["invites"]]
    return (owner["org_id"], owner["org_slug"], f"o{owner['org_id'][:8]}-", "Canary Alpha", *emails)


def _rows_left(needles) -> dict[str, int]:
    """Every row of every table the database has now, with no table named here."""
    found: dict[str, int] = {}
    with usage.engine().connect() as con:
        for name in sa.inspect(con).get_table_names():
            for row in con.execute(sa.text(f'select * from "{name}"')).all():
                text = " ".join(str(v) for v in row)
                if any(n in text for n in needles):
                    found[name] = found.get(name, 0) + 1
    return found


def _files_left(needles) -> list[str]:
    """Every path and every file under the data root. The SQLite store is judged by
    its rows above: freed pages keep old bytes until a vacuum."""
    found = []
    for p in data_root().rglob("*"):
        rel = str(p.relative_to(data_root()))
        if any(n in rel for n in needles):
            found.append(rel)
        elif p.is_file() and not p.is_symlink() and not p.name.startswith("meta.sqlite"):
            raw = p.read_bytes()
            if any(n.encode() in raw for n in needles):
                found.append(rel)
    return found


def _no_wait(monkeypatch):
    """As hosted: the API only queues, and the worker does the work."""
    monkeypatch.setenv("IRONWRIGHT_RUNS_WAIT", "0")


def delete_org(c, owner, confirm=None):
    return c.post(f"/orgs/{owner['org_slug']}/delete", headers=bearer(owner),
                  json={"confirm": owner["org_slug"] if confirm is None else confirm})


# ---------------------------------------------------------------- export

def _export(c, owner) -> tuple[str, zipfile.ZipFile]:
    h = bearer(owner)
    slug = owner["org_slug"]
    r = c.post(f"/orgs/{slug}/exports", headers=h)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    listed = c.get(f"/orgs/{slug}/exports", headers=h).json()
    assert next(j for j in listed if j["id"] == job_id)["state"] == "succeeded", listed
    got = c.get(f"/orgs/{slug}/exports/{job_id}/download", headers=h)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/zip"
    assert f"{slug}" in got.headers["content-disposition"]
    return job_id, zipfile.ZipFile(io.BytesIO(got.content))


def test_an_owner_downloads_specs_ontology_and_gold_as_parquet(client):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    _, z = _export(client, owner)
    names = set(z.namelist())
    assert {"manifest.json", "workspaces/claims/workspace.yaml", "workspaces/claims/ontology.yaml",
            "workspaces/claims/policies.yaml", "workspaces/claims/gold/claims_by_policy.parquet"} <= names
    table = pq.read_table(io.BytesIO(z.read("workspaces/claims/gold/claims_by_policy.parquet")))
    assert table.num_rows > 0
    assert yaml.safe_load(z.read("workspaces/claims/workspace.yaml"))["kind"] == "workspace"
    assert json.loads(z.read("manifest.json"))["workspaces"] == ["claims"]


def test_an_export_holds_no_raw_upload_no_bronze_and_no_server_path(client):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    _, z = _export(client, owner)
    names = z.namelist()
    assert not [n for n in names if n.endswith(".csv") or "/bronze" in n or "/silver/" in n.replace("models/silver/", "")]
    for name in names:
        if name.endswith((".yaml", ".json", ".sql")):
            assert str(data_root()).encode() not in z.read(name), name


def test_an_export_holds_only_this_org(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta org")
    _tenant_workspace(a["org_id"], "only-a")
    _tenant_workspace(b["org_id"], "only-b")
    _, z = _export(client, b)
    assert not [n for n in z.namelist() if "only-a" in n]


def test_an_export_carries_a_scenario_spec_and_accepted_versions(client):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    assert client.post("/workspaces", headers=bearer(owner),
                       json={"name": "my-scenario", "size": 4, "seed": 3}).status_code == 201
    versions = tenant_dir(owner["org_id"]) / "workspaces" / "claims" / "ontology_versions"
    versions.mkdir()
    (versions / "1.json").write_text('{"version": 1}')
    _, z = _export(client, owner)
    assert "scenarios/my-scenario/scenario.yaml" in z.namelist()
    assert json.loads(z.read("workspaces/claims/ontology_versions/1.json")) == {"version": 1}


def test_an_export_does_not_follow_a_symlink_out_of_the_org(client):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    secret = data_root() / "outside.txt"
    secret.write_text("outside the org")
    d = tenant_dir(owner["org_id"]) / "workspaces" / "claims" / "ontology_versions"
    d.mkdir()
    (d / "1.json").symlink_to(secret)
    _, z = _export(client, owner)
    assert "workspaces/claims/ontology_versions/1.json" not in z.namelist()


def test_a_new_export_replaces_the_old_file(client):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    first, _ = _export(client, owner)
    second, _ = _export(client, owner)
    assert first != second
    held = [p.name for p in (tenant_dir(owner["org_id"]) / "exports").iterdir()]
    assert held == [f"{second}.zip"]
    h = bearer(owner)
    assert client.get(f"/orgs/{owner['org_slug']}/exports/{first}/download", headers=h).status_code == 404


def test_an_export_runs_through_a_real_worker_when_hosted(client, monkeypatch):
    _no_wait(monkeypatch)
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    h = bearer(owner)
    job_id = client.post(f"/orgs/{owner['org_slug']}/exports", headers=h).json()["id"]
    r = client.get(f"/orgs/{owner['org_slug']}/exports/{job_id}/download", headers=h)
    assert r.status_code == 404, "not ready is not downloadable"
    assert Worker(JobStore()).run_once(job_id) is not None
    assert client.get(f"/orgs/{owner['org_slug']}/exports/{job_id}/download", headers=h).status_code == 200


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_only_an_owner_exports_or_downloads(client, role):
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    job_id, _ = _export(client, owner)
    who = join(client, owner, f"{role}@a.example", role)
    slug = owner["org_slug"]
    h = bearer(who)
    assert client.post(f"/orgs/{slug}/exports", headers=h).status_code == 403
    assert client.get(f"/orgs/{slug}/exports", headers=h).status_code == 403
    assert client.get(f"/orgs/{slug}/exports/{job_id}/download", headers=h).status_code == 403


def test_a_demoted_owner_loses_the_download(client):
    owner = signup(client)
    second = join(client, owner, "second@a.example", "owner")
    _tenant_workspace(owner["org_id"])
    job_id, _ = _export(client, owner)
    assert set_role(client, owner, second, "member").status_code == 200
    r = client.get(f"/orgs/{owner['org_slug']}/exports/{job_id}/download", headers=bearer(second))
    assert r.status_code == 403


def test_the_owner_of_another_org_cannot_fetch_this_export(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta org")
    _tenant_workspace(a["org_id"])
    job_id, _ = _export(client, a)
    h = bearer(b)
    gate = client.get(f"/orgs/{a['org_slug']}/exports/{job_id}/download", headers=h)
    assert (gate.status_code, gate.json()) == (404, {"detail": NOT_FOUND})
    own = client.get(f"/orgs/{b['org_slug']}/exports/{job_id}/download", headers=h)
    assert own.status_code == 404
    assert b"PK" not in own.content[:2]


def test_the_download_route_serves_export_jobs_only(client):
    owner = signup(client)
    h = bearer(owner)
    made = client.post("/workspaces", headers=h, json={"name": "some-ws", "size": 4, "seed": 3}).json()["name"]
    job = client.post("/jobs", headers=h, json={"kind": "profile", "workspace": made}).json()["id"]
    assert client.get(f"/orgs/{owner['org_slug']}/exports/{job}/download", headers=h).status_code == 404
    assert client.get(f"/orgs/{owner['org_slug']}/exports/..%2f..%2fetc/download", headers=h).status_code == 404


def test_post_jobs_is_not_a_side_door_to_an_export(client):
    """/jobs is member level and any registered kind goes through it."""
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    made = client.post("/workspaces", headers=bearer(owner), json={"name": "some-ws", "size": 4, "seed": 3}).json()["name"]
    r = client.post("/jobs", headers=bearer(who), json={"kind": "org_export", "workspace": made})
    assert r.status_code == 200, r.text
    Worker(JobStore()).run_once(r.json()["id"])
    job = JobStore().get(owner["org_id"], r.json()["id"])
    assert job["state"] == "failed"
    assert not (tenant_dir(owner["org_id"]) / "exports").exists()


def test_the_dev_org_has_nothing_to_export_or_delete(client):
    service.seed_operator("dev@a.example", PASSWORD, NOON)
    who = service.login("dev@a.example", PASSWORD, time.time())
    assert client.post(f"/orgs/{DEV_ORG_SLUG}/exports", headers=bearer(who)).status_code == 403
    r = client.post(f"/orgs/{DEV_ORG_SLUG}/delete", headers=bearer(who), json={"confirm": DEV_ORG_SLUG})
    assert r.status_code == 403
    with usage.engine().connect() as con:
        assert con.execute(sa.select(db.orgs).where(db.orgs.c.id == DEV_ORG_ID)).first() is not None


# ---------------------------------------------------------------- the worker can see these kinds

def test_the_worker_process_has_both_kinds_without_the_api_loaded():
    """The tests above import the API. `ironwright worker` imports only this."""
    code = ("from ironwright.jobs import handlers, worker; import sys;"
            "assert {'org_export', 'org_delete'} <= set(handlers.HANDLERS);"
            "assert 'ironwright.platform_api' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)


# ---------------------------------------------------------------- delete

def test_the_wrong_slug_changes_nothing(client):
    owner = signup(client)
    r = delete_org(client, owner, confirm="not-the-slug")
    assert r.status_code == 422
    assert client.post(f"/orgs/{owner['org_slug']}/delete", headers=bearer(owner), json={}).status_code == 422
    assert client.get("/auth/me", headers=bearer(owner)).status_code == 200


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_only_an_owner_deletes(client, role):
    owner = signup(client)
    who = join(client, owner, f"{role}@a.example", role)
    assert delete_org(client, who).status_code == 403
    assert client.get("/auth/me", headers=bearer(owner)).status_code == 200


@pytest.mark.parametrize("role", ["owner", "member"])
def test_post_jobs_is_not_a_side_door_to_a_delete(client, role):
    """Only the owner route writes the deleting mark, and the job refuses without it."""
    owner = signup(client)
    made = client.post("/workspaces", headers=bearer(owner), json={"name": "some-ws", "size": 4, "seed": 3}).json()["name"]
    who = owner if role == "owner" else join(client, owner, "m@a.example", "member")
    r = client.post("/jobs", headers=bearer(who), json={"kind": "org_delete", "workspace": made})
    assert r.status_code == 200, r.text
    Worker(JobStore()).run_once(r.json()["id"])
    assert JobStore().get(owner["org_id"], r.json()["id"])["state"] == "failed"
    assert client.get(f"/workspaces/{made}/organization", headers=bearer(owner)).status_code == 200
    assert client.get("/auth/me", headers=bearer(owner)).status_code == 200


def test_marking_shuts_the_door_before_the_job_runs(client, monkeypatch):
    _no_wait(monkeypatch)
    owner = signup(client)
    who = join(client, owner, "m@a.example", "member")
    _tenant_workspace(owner["org_id"])
    assert client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                       json={"email": "late@a.example", "role": "viewer"}).status_code == 201
    token = client.post(f"/orgs/{owner['org_slug']}/invites", headers=bearer(owner),
                        json={"email": "late2@a.example", "role": "viewer"}).json()["token"]
    r = delete_org(client, owner)
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "deleting"
    # The bytes are still there, and nobody can reach them.
    assert (tenant_dir(owner["org_id"]) / "workspaces" / "claims").is_dir()
    for session in (owner, who):
        assert client.get("/auth/me", headers=bearer(session)).status_code == 401
    assert client.post("/auth/login", json={"email": "owner@a.example", "password": PASSWORD}).status_code == 401
    assert client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 404


def test_an_invite_accepted_as_the_org_is_marked_gets_nothing(client, monkeypatch):
    """The accept has read the invite, and the mark commits before it writes. Without
    a second look under the org's lock, a new member lands in an org being deleted."""
    owner = signup(client)
    token = service.invite(owner["org_slug"], "late@a.example", "member", time.time())
    real_lock = service._lock_org
    fired = []

    def mark_first(con, org_id):
        if not fired:
            fired.append(True)
            assert service.mark_deleting(org_id, owner["principal"], NOON) is True
        real_lock(con, org_id)

    monkeypatch.setattr(service, "_lock_org", mark_first)
    with pytest.raises(service.NoSuchInvite):
        service.accept_invite(token, PASSWORD, time.time())
    assert fired
    with usage.engine().connect() as con:
        assert con.execute(sa.select(db.memberships).where(db.memberships.c.org_id == owner["org_id"])).first() is None
        assert con.execute(sa.select(db.users).where(db.users.c.email == "late@a.example")).first() is None


def test_marking_cancels_what_was_queued_and_asks_what_runs_to_stop(client, monkeypatch):
    _no_wait(monkeypatch)
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    store = JobStore()
    queued = client.post("/jobs", headers=bearer(owner), json={"kind": "run", "workspace": "claims"}).json()["id"]
    running = client.post("/jobs", headers=bearer(owner), json={"kind": "run", "workspace": "claims"}).json()["id"]
    claimed = store.claim("test", job_id=running)
    assert claimed is not None
    assert delete_org(client, owner).status_code == 202
    assert store.get(owner["org_id"], queued)["state"] == "cancelled"
    assert store.cancel_requested(running) is True
    # One running job per org: the delete cannot start until that one is out of the way.
    delete_job = next(j for j in store.list(owner["org_id"]) if j["kind"] == "org_delete")
    assert Worker(store).run_once(delete_job["id"]) is None
    assert (tenant_dir(owner["org_id"]) / "workspaces" / "claims").is_dir()
    store.finish(running, claimed["token"], "cancelled")
    assert Worker(store).run_once(delete_job["id"]) is not None
    assert not tenant_dir(owner["org_id"]).exists()


def test_nothing_of_the_org_remains_but_one_tombstone(client, monkeypatch):
    _no_wait(monkeypatch)
    owner = _rich_org(client)
    needles = _needles(client, owner)
    assert _files_left(needles), "the sweep must find the org before the delete, or it proves nothing"
    assert len(_rows_left(needles)) > 8, _rows_left(needles)
    assert delete_org(client, owner).status_code == 202
    job = next(j for j in JobStore().list(owner["org_id"]) if j["kind"] == "org_delete")
    assert Worker(JobStore()).run_once(job["id"]) is not None
    assert _files_left(needles) == []
    assert _rows_left(needles) == {"org_audit": 1, "org_audit_chain": 1}


def test_the_tombstone_says_who_and_when_and_names_nobody(client):
    owner = _rich_org(client)
    needles = [n for n in _needles(client, owner) if n != owner["org_id"]]
    assert delete_org(client, owner).status_code == 200
    with usage.engine().connect() as con:
        rows = con.execute(sa.select(usage.org_audit).where(usage.org_audit.c.org_id == owner["org_id"])).mappings().all()
    assert len(rows) == 1
    assert rows[0]["action"] == "org.deleted" and rows[0]["actor"] == owner["principal"]
    assert not [n for n in needles if n in rows[0]["detail"]]
    assert usage.verify_chain(owner["org_id"])["verified"] is True


def test_the_delete_answers_deleted_when_the_api_drove_it(client):
    owner = signup(client)
    r = delete_org(client, owner)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "deleted"
    assert not tenant_dir(owner["org_id"]).exists()


def test_the_email_and_slug_are_free_again(client):
    owner = signup(client)
    slug = owner["org_slug"]
    assert delete_org(client, owner).status_code == 200
    again = signup(client)
    assert again["org_slug"] == slug and again["org_id"] != owner["org_id"]


def test_someone_in_another_org_keeps_their_account_and_that_org(client):
    a = signup(client)
    b = signup(client, "b@b.example", "Beta org")
    token = client.post(f"/orgs/{a['org_slug']}/invites", headers=bearer(a),
                        json={"email": "b@b.example", "role": "member"}).json()["token"]
    assert client.post("/auth/accept-invite", json={"token": token, "password": PASSWORD}).status_code == 200
    assert delete_org(client, a).status_code == 200
    back = client.post("/auth/login", json={"email": "b@b.example", "password": PASSWORD})
    assert back.status_code == 200
    assert [o["slug"] for o in back.json()["orgs"]] == [b["org_slug"]]


def test_another_org_is_untouched_to_the_byte(client):
    a = _rich_org(client)
    b = _rich_org(client, "canary-two@b.example", "Beta org")
    root = tenant_dir(b["org_id"])
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    rows_before = _rows_left((b["org_id"],))
    assert delete_org(client, a).status_code == 200
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert _rows_left((b["org_id"],)) == rows_before
    assert client.get(f"/workspaces/{'o' + b['org_id'][:8] + '-canary-scenario'}/organization",
                      headers=bearer(b)).status_code == 200


def test_a_scenario_folder_owned_by_someone_else_with_the_same_prefix_stays(client):
    """Folders carry the first 8 characters of the org id, and that is not proof of ownership."""
    owner = signup(client)
    other = data_root() / f"o{owner['org_id'][:8]}-not-mine"
    other.mkdir()
    (other / service.OWNER_FILE).write_text("f" * 32)
    assert delete_org(client, owner).status_code == 200
    assert other.is_dir()


def test_the_demo_tree_is_never_touched(client):
    owner = signup(client)
    demo = data_root() / "demo" / "retail"
    demo.mkdir(parents=True)
    (demo / "keep.txt").write_text("shared")
    assert delete_org(client, owner).status_code == 200
    assert (demo / "keep.txt").read_text() == "shared"


def test_a_delete_run_twice_finishes_the_same(client, monkeypatch):
    """requeue_stale runs the job again if the worker dies halfway."""
    _no_wait(monkeypatch)
    owner = _rich_org(client)
    needles = _needles(client, owner)
    assert delete_org(client, owner).status_code == 202
    real = shutil.rmtree
    calls = []

    def dies_once(path, *a, **kw):
        calls.append(path)
        real(path, *a, **kw)
        raise KeyboardInterrupt("the worker died right here")

    monkeypatch.setattr(service.shutil, "rmtree", dies_once)
    with pytest.raises(KeyboardInterrupt):
        service.delete_org_data(owner["org_id"], NOON)
    monkeypatch.setattr(service.shutil, "rmtree", real)
    service.delete_org_data(owner["org_id"], NOON)
    assert _files_left(needles) == []
    assert _rows_left(needles) == {"org_audit": 1, "org_audit_chain": 1}


def test_a_delete_that_fails_keeps_the_rows_and_can_be_resumed(client, monkeypatch):
    _no_wait(monkeypatch)
    owner = signup(client)
    _tenant_workspace(owner["org_id"])
    assert delete_org(client, owner).status_code == 202
    job = next(j for j in JobStore().list(owner["org_id"]) if j["kind"] == "org_delete")

    def fails(*_a, **_kw):
        raise OSError("the store is down")

    real = service._remove_lake_prefix
    monkeypatch.setattr(service, "_remove_lake_prefix", fails)
    monkeypatch.setattr(service, "_RETRY_WAITS", (0, 0))
    Worker(JobStore()).run_once(job["id"])
    assert JobStore().get(owner["org_id"], job["id"])["state"] == "failed"
    with usage.engine().connect() as con:
        assert con.execute(sa.select(db.orgs).where(db.orgs.c.id == owner["org_id"])).first() is not None
    assert service.pending_deletions() == [owner["org_id"]]
    monkeypatch.setattr(service, "_remove_lake_prefix", real)
    again = service.resume_deletion(owner["org_id"], JobStore())
    assert Worker(JobStore()).run_once(again) is not None
    assert service.pending_deletions() == []
    with usage.engine().connect() as con:
        assert con.execute(sa.select(db.orgs).where(db.orgs.c.id == owner["org_id"])).first() is None


def test_resume_refuses_an_org_nobody_asked_to_delete(client):
    owner = signup(client)
    with pytest.raises(service.NotDeleting):
        service.resume_deletion(owner["org_id"], JobStore())


def test_the_data_step_refuses_the_dev_org_and_a_bad_id_outright(client):
    with pytest.raises(service.NotDeleting):
        service.delete_org_data(DEV_ORG_ID, NOON)
    with pytest.raises(service.NotDeleting):
        service.delete_org_data("../..", NOON)


def _local_bucket(root: Path):
    """An S3 shaped store on local disk, so the prefix logic runs for real here."""
    return pafs.SubTreeFileSystem(str(root), pafs.LocalFileSystem())


def test_the_lake_prefix_goes_and_a_neighbour_stays(client, monkeypatch, tmp_path):
    owner = signup(client)
    other = signup(client, "b@b.example", "Beta org")
    bucket = tmp_path / "s3"
    for org in (owner["org_id"], other["org_id"]):
        d = bucket / "lake" / "pre" / "tenants" / org / "workspaces" / "w" / "lake"
        d.mkdir(parents=True)
        (d / "data.parquet").write_bytes(b"x")
    keep = bucket / "lake" / "pre" / "demo" / "retail"
    keep.mkdir(parents=True)
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://lake/pre")
    monkeypatch.setattr(hosted, "object_store", lambda **_: _local_bucket(bucket))
    assert delete_org(client, owner).status_code == 200
    assert not (bucket / "lake" / "pre" / "tenants" / owner["org_id"]).exists()
    assert (bucket / "lake" / "pre" / "tenants" / other["org_id"] / "workspaces" / "w" / "lake" / "data.parquet").exists()
    assert keep.is_dir()


def test_a_prefix_already_gone_is_not_a_failure(client, monkeypatch, tmp_path):
    owner = signup(client)
    monkeypatch.setenv("IRONWRIGHT_LAKE_URI", "s3://lake/pre")
    monkeypatch.setattr(hosted, "object_store", lambda **_: _local_bucket(tmp_path))
    assert delete_org(client, owner).status_code == 200


def test_the_iceberg_catalog_rows_in_the_store_go_too(client):
    """On a hosted lake the catalog is the metadata store, keyed by a name that holds the
    org id, in tables pyiceberg made and no column here calls org_id."""
    from pyiceberg.catalog.sql import SqlCatalog

    owner = signup(client)
    other = signup(client, "b@b.example", "Beta org")
    url = str(usage.engine().url.render_as_string(hide_password=False))
    wh = data_root() / "wh"
    wh.mkdir()
    for org in (owner["org_id"], other["org_id"]):
        cat = SqlCatalog(f"tenant/{org}/w", uri=url, warehouse=f"file://{wh}/{org}")
        cat.create_namespace_if_not_exists("gold")
        cat.create_table_if_not_exists("gold.t", pa.schema([("a", pa.int32())]))
    needles = (owner["org_id"],)
    assert "iceberg_tables" in _rows_left(needles)
    assert delete_org(client, owner).status_code == 200
    assert _rows_left(needles) == {"org_audit": 1, "org_audit_chain": 1}
    assert "iceberg_tables" in _rows_left((other["org_id"],))


def test_a_table_added_later_with_an_org_id_is_cleared_without_being_named(client):
    owner = signup(client)
    with usage.engine().begin() as con:
        con.execute(sa.text("create table next_months_table (org_id varchar(32), note varchar(40))"))
        con.execute(sa.text("insert into next_months_table values (:o, 'x')"), {"o": owner["org_id"]})
        con.execute(sa.text("insert into next_months_table values (:o, 'y')"), {"o": "f" * 32})
    try:
        assert delete_org(client, owner).status_code == 200
        with usage.engine().connect() as con:
            assert con.execute(sa.text("select note from next_months_table")).scalars().all() == ["y"]
    finally:
        with usage.engine().begin() as con:
            con.execute(sa.text("drop table next_months_table"))
