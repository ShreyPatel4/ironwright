"""The landing page's Try it section: every claim names a test that exists, and every link is real."""
from __future__ import annotations

import ast
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "site" / "index.html").read_text()
BLOB = "https://github.com/ShreyPatel4/ironwright/blob/main/"


class Section(HTMLParser):
    """The links and the text inside <section id="try">."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.links: list[dict[str, str | None]] = []
        self.text: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and (self.depth or a.get("id") == "try"):
            self.depth += 1
        if self.depth:
            if tag == "a":
                self.links.append(a)
            if a.get("id"):
                self.ids.append(a["id"])

    def handle_endtag(self, tag):
        if tag == "section" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


@pytest.fixture(scope="module")
def section() -> Section:
    s = Section()
    s.feed(HTML)
    return s


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_the_section_is_there_and_the_nav_finds_it(section):
    assert section.ids and section.ids[0] == "try"
    assert 'href="#try"' in HTML


def test_it_links_the_film_and_its_poster_relatively(section):
    film = [a for a in section.links if a.get("data-try") == "film"]
    assert [a["href"] for a in film] == ["media/demo/ironwright-saas-demo.mp4"]
    assert 'poster="media/demo/ironwright-saas-demo.jpg"' in HTML


def test_the_sign_up_link_is_a_placeholder_that_says_so(section):
    """Nothing is deployed. .invalid can never resolve to somebody else's site, and the page says it."""
    signup = [a for a in section.links if a.get("data-try") == "signup"]
    assert [a["href"] for a in signup] == ["https://staging.ironwright.invalid/console/"]
    assert "placeholder" in " ".join(section.text).lower()


def test_three_claims_each_link_a_test_that_exists(section):
    claims = [a for a in section.links if a.get("data-claim")]
    assert len(claims) == 3
    for a in claims:
        assert a["href"].startswith(BLOB), a["href"]
        path, _, test = a["href"][len(BLOB):].partition("#")
        assert a["data-claim"] == test
        assert test in _test_names(ROOT / path), f"{path} has no {test}"


def test_the_copy_follows_the_house_style(section):
    text = " ".join(section.text)
    assert text.strip()
    assert "—" not in text and "–" not in text


def test_every_other_link_stays_relative(section):
    for a in section.links:
        if not (a.get("data-claim") or a.get("data-try") == "signup"):
            assert not re.match(r"[a-z]+://|/", a["href"]), a["href"]


@pytest.mark.xfail(strict=True, reason="the final take waits on F2, wave 5 and tenant actions. This goes red when the film lands.")
def test_the_film_and_the_poster_are_committed():
    for name in ("ironwright-saas-demo.mp4", "ironwright-saas-demo.jpg"):
        assert (ROOT / "media" / "demo" / name).stat().st_size > 0
