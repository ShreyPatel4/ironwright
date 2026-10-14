"""Build the use-case film page (all specs inlined) and, with --video, one MP4 per use case.
Run scripts/film_specs.py first. Output: dist/ironwright-films.html and dist/films/<id>.mp4."""
import argparse
import base64
import json
import pathlib
import subprocess

root = pathlib.Path(__file__).resolve().parents[1]
specs_dir = root / "web" / "films" / "specs"
ORDER = ["retail", "supply_chain", "banking_aml", "insurance_claims", "healthcare", "investing"]
out = root / "dist" / "ironwright-films.html"


def build():
    specs = [json.loads((specs_dir / f"{i}.json").read_text()) for i in ORDER if (specs_dir / f"{i}.json").exists()]
    html = (root / "web" / "films" / "film.src.html").read_text()
    html = html.replace("/*FONTS*/", (root / "web" / "intro" / "fonts.css").read_text())
    html = html.replace("/*SPECS*/", "window.FILM_SPECS=" + json.dumps(specs, separators=(",", ":")).replace("</", "<\\/") + ";")
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    return [s["id"] for s in specs]


def video(ids, fps=60, only=None):
    from playwright.sync_api import sync_playwright
    page = out.parent / "_films_render.html"
    page.write_text("<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>" + out.read_text() + "</body></html>")
    (out.parent / "films").mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1400})
        pg.goto(page.as_uri())
        pg.wait_for_function("window.__render && document.fonts.status === 'loaded'")
        pg.evaluate("window.__stop = true")
        pg.wait_for_timeout(300)
        dur = pg.evaluate("window.__DUR")
        for fid in ids:
            if only and fid not in only:
                continue
            pg.evaluate("id => window.__use(id)", fid)
            dest = out.parent / "films" / f"{fid}.mp4"
            ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-",
                                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium", "-movflags", "+faststart", str(dest)],
                                  stdin=subprocess.PIPE)
            for i in range(int(dur * fps) + 1):
                data = pg.evaluate("t => { window.__render(t); return document.getElementById('c').toDataURL('image/jpeg', 0.95).split(',')[1] }", i / fps)
                ff.stdin.write(base64.b64decode(data))
            ff.stdin.close()
            ff.wait()
            print("wrote", dest, flush=True)
        b.close()
    page.unlink()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    ids = build()
    print("wrote", out, ids)
    if a.video:
        video(ids, only=a.only)
