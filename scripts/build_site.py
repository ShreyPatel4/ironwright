"""Assemble the deployable site into ./site.

    site/
      index.html            landing page            (web/site/index.html)
      demo/                 interactive showcase    (web/index.html + style.css + app.js + data/)
      film/                 interactive intro film  (dist/ironwright-intro.html)
      films/                use-case film player    (dist/ironwright-films.html)
      media/                MP4s and posters        (media/ in the repo, rendered by build_intro/build_films)
      _headers, 404.html, robots.txt

Every link is relative, so the same build works at the subdomain root
(coconutlabs.org/projects/ironwright/) and under a path (coconutlabs.org/ironwright/).

Usage: python scripts/build_site.py [--collect-media]
  --collect-media  copy freshly rendered MP4s from dist/ into media/ and cut posters first
"""
import argparse
import pathlib
import shutil
import subprocess

root = pathlib.Path(__file__).resolve().parents[1]
site = root / "site"
media = root / "media"
FILMS = ["retail", "supply_chain", "banking_aml", "insurance_claims", "healthcare", "investing"]

WRAP = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>.home{{max-width:1240px;margin:0 auto;padding:16px 16px 0;font:500 14px Geist,"Segoe UI",system-ui,sans-serif}}.home a{{color:#4C90F0;text-decoration:none}}.home a:hover{{text-decoration:underline}}</style>
</head><body>
<div class="home"><a href="../">Ironwright home</a> · <a href="{other_href}">{other_label}</a></div>
{body}
</body></html>
"""


def poster(src: pathlib.Path, at: float, dest: pathlib.Path) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(src), "-frames:v", "1",
                    "-vf", "scale=1280:-2", "-q:v", "3", str(dest)], check=True)


def collect_media() -> None:
    (media / "films").mkdir(parents=True, exist_ok=True)
    intro = root / "dist" / "ironwright-intro.mp4"
    if intro.exists():
        shutil.copy2(intro, media / "ironwright-intro.mp4")
        poster(intro, 220.5, media / "intro-poster.jpg")
    for fid in FILMS:
        src = root / "dist" / "films" / f"{fid}.mp4"
        if src.exists():
            shutil.copy2(src, media / "films" / f"{fid}.mp4")
            poster(src, 4.5, media / "films" / f"{fid}.jpg")


def build() -> None:
    if site.exists():
        shutil.rmtree(site)
    site.mkdir()
    shutil.copy2(root / "web" / "site" / "index.html", site / "index.html")
    demo = site / "demo"
    demo.mkdir()
    for f in ("index.html", "style.css", "app.js"):
        shutil.copy2(root / "web" / f, demo / f)
    shutil.copytree(root / "web" / "data", demo / "data")
    for name, src, other in (("film", "ironwright-intro.html", ("../films/", "Use-case films")),
                             ("films", "ironwright-films.html", ("../film/", "The Ironwright film"))):
        d = site / name
        d.mkdir()
        body = (root / "dist" / src).read_text()
        (d / "index.html").write_text(WRAP.format(body=body, other_href=other[0], other_label=other[1]))
    if media.exists():
        shutil.copytree(media, site / "media")
    (site / "_headers").write_text("""/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  X-Frame-Options: SAMEORIGIN

/media/*
  Cache-Control: public, max-age=604800, immutable

/demo/data/*
  Cache-Control: public, max-age=3600
""")
    (site / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    (site / "404.html").write_text("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found, Ironwright</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#111418;color:#F6F7F9;font:16px/1.6 Geist,"Segoe UI",system-ui,sans-serif;padding:0 20px}a{color:#4C90F0}</style></head>
<body><div><h1 style="font-weight:500;letter-spacing:-.02em">That page is not here.</h1><p>Try the <a href="/">Ironwright home page</a>.</p></div></body></html>
""")
    total = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
    print(f"site built: {site} ({total / 1e6:.1f} MB, {sum(1 for _ in site.rglob('*') if _.is_file())} files)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-media", action="store_true")
    a = ap.parse_args()
    if a.collect_media:
        collect_media()
    build()
