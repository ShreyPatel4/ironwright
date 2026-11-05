"""scripts/record_demo.py: what can be proved about the film without a stack to film.

The beats themselves run against a real console. The rehearsal notes are in docs/DEMO-SCRIPT-SAAS.md.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "record_demo.py"
TICKET_ORDER = ["signup", "upload", "profile", "silver", "run", "ontology",
                "ask", "action", "audit", "isolation", "usage"]


@pytest.fixture(scope="module")
def rec():
    spec = importlib.util.spec_from_file_location("record_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_demo"] = module
    spec.loader.exec_module(module)
    return module


def test_the_captions_tell_the_beats_in_the_tickets_order(rec):
    assert [b.id for b in rec.load_beats()] == TICKET_ORDER


def test_every_beat_has_a_function_that_films_it(rec):
    for beat in rec.load_beats():
        assert callable(getattr(rec.Film, f"beat_{beat.id}", None)), beat.id


def test_the_holds_add_up_to_about_two_and_a_half_minutes(rec):
    held = sum(sum(c.hold for c in b.captions) for b in rec.load_beats())
    assert 100 <= held <= 125, held  # the moves between them take the rest, about 50 s in a rehearsal


def test_the_captions_follow_the_house_style(rec):
    for beat in rec.load_beats():
        assert beat.captions, beat.id
        for caption in beat.captions:
            assert "—" not in caption.text and "–" not in caption.text, caption.text
            assert len(caption.text) <= 110, caption.text
            assert caption.hold >= 3, caption.text  # content holds for a few seconds


def test_a_caption_file_out_of_order_is_refused(rec, tmp_path):
    bad = json.loads(rec.CAPTIONS.read_text())
    bad["beats"][0], bad["beats"][1] = bad["beats"][1], bad["beats"][0]
    path = tmp_path / "captions.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="order"):
        rec.load_beats(path)


def test_the_narrative_tells_the_same_beats_in_the_same_order(rec):
    doc = (ROOT / "docs" / "DEMO-SCRIPT-SAAS.md").read_text()
    assert "\u2014" not in doc
    assert "```mermaid" in doc
    table = [line.split("|")[2].strip() for line in doc.splitlines() if re.match(r"\| *\d+:\d\d", line)]
    assert table == TICKET_ORDER
    for beat in rec.load_beats():
        for caption in beat.captions:
            assert caption.text in doc, caption.text


def test_the_recorder_stubs_nothing():
    """Wave 5's rule. A stub in the film would be a lie on camera."""
    source = SCRIPT.read_text()
    for banned in ("page" + ".route", "context" + ".route", "route" + "_from_har", "add_api" + "_route", "_install" + "_gaps"):
        assert banned not in source, banned


def test_the_recorder_hides_nothing_on_screen(rec):
    assert "visibility" not in rec.OVERLAY_JS and "display:none" not in rec.OVERLAY_JS.replace(" ", "")


# ---------------------------------------------------------------- the cut


def test_a_long_wait_is_cut_down_to_its_lead_and_its_tail(rec):
    kept = rec.keep_segments(100.0, [(10.0, 40.0)], lead=2.0, tail=1.0)
    assert kept == [(0.0, 12.0), (39.0, 100.0)]


def test_a_short_wait_is_left_alone(rec):
    assert rec.keep_segments(100.0, [(10.0, 12.0)], lead=2.0, tail=1.0) == [(0.0, 100.0)]


def test_waits_that_touch_do_not_make_a_backwards_segment(rec):
    kept = rec.keep_segments(100.0, [(10.0, 30.0), (30.0, 60.0)], lead=2.0, tail=1.0)
    assert kept == [(0.0, 12.0), (29.0, 32.0), (59.0, 100.0)]
    assert all(a < b for a, b in kept)


def test_a_moment_in_the_recording_maps_to_its_moment_in_the_film(rec):
    kept = [(0.0, 10.0), (20.0, 30.0)]
    assert rec.film_time(5.0, kept) == 5.0
    assert rec.film_time(25.0, kept) == 15.0
    assert rec.film_time(15.0, kept) == 10.0  # inside a cut lands on the join


def test_the_length_is_checked_against_the_target(rec):
    rec.check_length(150.0)
    with pytest.raises(rec.NotPublishable, match="2:30"):
        rec.check_length(240.0)
    with pytest.raises(rec.NotPublishable, match="2:30"):
        rec.check_length(60.0)


# ---------------------------------------------------------------- what may reach media/


def test_a_film_with_a_gap_never_reaches_media(rec, tmp_path):
    with pytest.raises(rec.NotPublishable, match="action"):
        rec.check_publishable([rec.Gap("action", "no approval gated action")], 150.0)


def test_a_clean_film_of_the_right_length_is_publishable(rec):
    rec.check_publishable([], 150.0)


def test_a_rehearsal_may_not_write_into_media(rec):
    with pytest.raises(SystemExit, match="media"):
        rec.rehearsal_dir(ROOT / "media" / "demo")
    with pytest.raises(SystemExit, match="media"):
        rec.rehearsal_dir(ROOT / "media")


def test_a_rehearsal_writes_where_it_is_told(rec, tmp_path):
    assert rec.rehearsal_dir(tmp_path / "take") == (tmp_path / "take").resolve()


def test_the_film_goes_where_the_landing_page_links(rec):
    assert rec.FILM == ROOT / "media" / "demo" / "ironwright-saas-demo.mp4"
    assert rec.POSTER == ROOT / "media" / "demo" / "ironwright-saas-demo.jpg"


# ---------------------------------------------------------------- the gated action


def test_the_film_picks_the_action_the_policy_gates(rec):
    describe = {"actions": {"note": {"params": {}, "requires_approval": False},
                            "flag": {"params": {"claim_id": {"type": "Claim"}}, "requires_approval": True}}}
    assert rec.gated_action(describe)[0] == "flag"


def test_a_workspace_with_no_gated_action_is_a_gap_and_is_not_faked(rec):
    with pytest.raises(rec.BeatFailed, match="no approval gated action"):
        rec.gated_action({"actions": {"note": {"params": {}, "requires_approval": False}}})


def test_params_come_from_the_real_objects(rec):
    params = {"claim_id": {"type": "Claim"}, "reason": {"type": "str"}, "qty": "int"}
    got = rec.action_params(params, {"Claim": "C-1"}.__getitem__, is_object=lambda t: t == "Claim")
    assert got == {"claim_id": "C-1", "reason": rec.ACTION_REASON, "qty": 1}


# ---------------------------------------------------------------- real tools


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg, so the cut was not proved")
def test_the_cut_makes_an_h264_film_and_a_poster_of_the_right_size(rec, tmp_path):
    raw = tmp_path / "raw.webm"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=1440x900:rate=25:duration=6", "-c:v", "libvpx", "-b:v", "1M", str(raw)], check=True)
    film, poster = tmp_path / "film.mp4", tmp_path / "poster.jpg"
    rec.cut(raw, [(0.0, 1.0), (4.0, 6.0)], film)
    rec.cut_poster(film, 1.0, poster)
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,pix_fmt:format=duration",
         "-of", "json", str(film)], check=True, capture_output=True, text=True).stdout)
    stream = probe["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"], stream["pix_fmt"]) == ("h264", 1440, 900, "yuv420p")
    assert float(probe["format"]["duration"]) == pytest.approx(3.0, abs=0.2)
    assert poster.stat().st_size > 0


def test_the_overlay_never_takes_a_click_from_the_console(rec):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except sync_api.Error as e:
            pytest.skip(f"no chromium for playwright, so this was not proved: {e}")
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(rec.OVERLAY_JS)
            page.goto("data:text/html,<html><body style='margin:0'><button id=b "
                      "style='position:fixed;left:0;right:0;bottom:0;height:300px;width:100%' "
                      "onclick=\"window.hit=1\">x</button></body></html>")
            page.evaluate("t => window.__film.caption(t)", "I can read this")
            box = page.get_by_text("I can read this").bounding_box()
            assert box and box["y"] > 600
            assert page.get_by_text("I can read this").is_visible()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)  # straight through the caption
            assert page.evaluate("window.hit") == 1
        finally:
            browser.close()
