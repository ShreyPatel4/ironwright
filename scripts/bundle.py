"""Inline web/style.css, web/app.js and every web/data/*.json into one self-contained HTML page
(dist/ontoforge-showcase.html). Used for the hosted demo; GitHub Pages / Cloudflare Pages can serve web/ as is."""
import json
import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[1]
web = root / "web"
data = {p.stem: json.loads(p.read_text()) for p in sorted((web / "data").glob("*.json"))}
css = (web / "style.css").read_text()
js = (web / "app.js").read_text()
html = (web / "index.html").read_text()
body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
body = body.replace('<script src="app.js"></script>', "")
fonts = re.search(r'(<link rel="preconnect".*?display=swap">)', html, re.S).group(1)
blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
out = f"""<title>OntoForge Showcase</title>
{fonts}
<style>
{css}
</style>
{body}
<script>window.ONTOFORGE_DATA={blob};</script>
<script>
{js}
</script>
"""
dest = root / "dist" / "ontoforge-showcase.html"
dest.parent.mkdir(exist_ok=True)
dest.write_text(out)
print(f"wrote {dest} ({dest.stat().st_size/1024:.0f} KB, packs: {', '.join(data)})")
