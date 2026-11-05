"""Small fixes from docs/review/2026-11-product-review.md, one test per finding."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _demo_check_tomorrow(tz: str) -> str:
    code = ("import importlib.util as u, sys;"
            "s = u.spec_from_file_location('c', sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m);"
            "print(m.tomorrow_utc())")
    done = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts" / "demo_console_check.py")],
                          capture_output=True, text=True, env={**os.environ, "TZ": tz}, check=True)
    return done.stdout.strip()


@pytest.mark.parametrize("tz", ["Etc/GMT+12", "Etc/GMT-14"])  # 26 hours apart: one is always off UTC's date
def test_f14_demo_check_grant_is_tomorrow_in_utc_whatever_the_laptop_says(tz):
    """A grant dies at the start of its date in UTC. Local tomorrow can already be today there."""
    want = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    assert _demo_check_tomorrow(tz) == want


def test_f20_demo_check_covers_every_pack():
    from ironwright.packs import list_packs
    checked = re.search(r"for p in ([^;]+);", (ROOT / "Makefile").read_text()).group(1).split()
    assert sorted(checked) == sorted(list_packs())


def test_f19_no_rules_does_not_read_as_cutover_ready(monkeypatch):
    from ironwright import evals
    monkeypatch.setattr(evals, "_load", lambda pack, name: {"rules": [], "cutover_ready": True})
    got = evals.eval_migration(None)
    assert "cutover ready" not in got["headline"] and "no reconciliation rules" in got["headline"]
    assert got["passed"], "nothing to reconcile is not a failure"


def test_f19_rules_still_say_ready(monkeypatch):
    from ironwright import evals
    rule = {"counts": {"explained": 2, "unexplained": 0}}
    monkeypatch.setattr(evals, "_load", lambda pack, name: {"rules": [rule], "cutover_ready": True})
    assert "cutover ready" in evals.eval_migration(None)["headline"]


def test_f24_map_copy_does_not_quote_our_contract_at_a_customer():
    assert "Contract 3" not in (ROOT / "ironwright" / "ingest" / "propose.py").read_text()


def test_f21_the_workspaces_page_does_not_count_the_demos():
    assert "four demo companies" not in (ROOT / "console" / "src" / "App.jsx").read_text().replace("\n", " ")


def test_f18_a_clone_with_no_console_build_says_how_to_get_one(meta_db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from ironwright import platform_api
    monkeypatch.setattr(platform_api, "CONSOLE_DIST", tmp_path / "not-built")
    with TestClient(platform_api.create_platform_app()) as c:
        got = c.get("/")
    assert got.status_code == 200 and "make console" in got.json()["console"]
