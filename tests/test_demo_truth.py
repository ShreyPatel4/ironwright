"""The demo says what the product does. Each of these was already wrong once (R1, F-25)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ironwright.packs import list_packs
from ironwright.platform_map import platform_map

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
# The script wraps at a fixed width, so a name can sit across two lines.
SCRIPT = " ".join((ROOT / "docs" / "DEMO-SCRIPT.md").read_text().split())


def _titles() -> dict[str, str]:
    return {p: yaml.safe_load((ROOT / "ironwright" / "packs" / p / "pack.yaml").read_text())["title"] for p in list_packs()}


def test_the_readme_names_every_company_and_counts_none_wrong():
    for pack, title in _titles().items():
        assert f"`{pack}`" in README and title in README, pack
    assert "four mock businesses" not in README


def test_the_script_names_every_company():
    for title in _titles().values():
        assert title in SCRIPT, title
    assert "Four fictional" not in SCRIPT and "four tabs" not in SCRIPT and "Four workspaces" not in SCRIPT


def test_the_web_demo_has_a_tab_for_every_pack():
    app = (ROOT / "web" / "app.js").read_text()
    assert set(re.findall(r'\{ id: "(\w+)", short:', app)) == set(list_packs())
    assert "Four fictional" not in (ROOT / "web" / "index.html").read_text()


def test_demo_check_gates_every_pack():
    make = (ROOT / "Makefile").read_text()
    loop = re.search(r"for p in ([\w ]+); do", make).group(1).split()
    assert set(loop) == set(list_packs())
    check = (ROOT / "scripts" / "demo_console_check.py").read_text()
    for pack in list_packs():
        assert f'"{pack}"' in check, pack


def test_sign_up_is_the_way_in_and_the_script_shows_the_second_org():
    for doc in (README, SCRIPT):
        assert "invite-only" not in doc
        assert "seed one with `ironwright auth-set`" not in doc
    assert "Sign up" in README
    assert "Org B asks for org A's job id and gets a 404" in SCRIPT


def test_nothing_shipped_still_reads_as_planned():
    assert "stay PLANNED" not in SCRIPT
    status = {n["id"]: n["status"] for n in platform_map()["nodes"]}
    assert status["pipe_profiler"] == "live" and status["gov_auth"] == "live"


def test_no_doc_quotes_a_test_count():
    for doc in (README, SCRIPT):
        assert not re.search(r"\b\d+ tests\b", doc)


def test_no_em_dashes():
    for doc in (README, SCRIPT):
        assert "—" not in doc


def test_the_readme_quickstart_goes_from_a_clone_to_sign_up():
    for step in ("uv sync", "npm ci", "npm run build", "ironwright platform", "ironwright worker"):
        assert step in README, step
