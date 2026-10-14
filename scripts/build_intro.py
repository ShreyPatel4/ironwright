"""Build the self-contained intro page (fonts inlined) and, with --video, render it to MP4.
The animation is a pure function of time (window.__render(t)), so every frame is exact."""
import argparse
import base64
import pathlib
import subprocess

root = pathlib.Path(__file__).resolve().parents[1]
src = root / "web" / "intro" / "intro.src.html"
out = root / "dist" / "ontoforge-intro.html"


def build() -> pathlib.Path:
    html = src.read_text().replace("/*FONTS*/", (root / "web" / "intro" / "fonts.css").read_text())
    out.parent.mkdir(exist_ok=True)
    out.write_text(html)
    return out


def video(fps: int, dest: pathlib.Path, scale: int) -> None:
    from playwright.sync_api import sync_playwright
    page_file = out.parent / "_intro_render.html"
    page_file.write_text("<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>" + out.read_text() + "</body></html>")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1200})
        pg.goto(page_file.as_uri())
        pg.wait_for_function("window.__render && document.fonts.status === 'loaded'")
        pg.evaluate("window.__stop = true")
        pg.wait_for_timeout(300)
        dur = pg.evaluate("window.__DUR")
        vf = [] if scale == 1080 else ["-vf", f"scale=-2:{scale}"]
        ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-",
                               *vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium",
                               "-movflags", "+faststart", str(dest)], stdin=subprocess.PIPE)
        n = int(dur * fps)
        for i in range(n + 1):
            data = pg.evaluate("t => { window.__render(t); return document.getElementById('c').toDataURL('image/jpeg', 0.95).split(',')[1] }", i / fps)
            ff.stdin.write(base64.b64decode(data))
        ff.stdin.close()
        ff.wait()
        b.close()
    page_file.unlink()
    print(f"wrote {dest}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", action="store_true")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--height", type=int, default=1080)
    a = ap.parse_args()
    print(f"wrote {build()}")
    if a.video:
        video(a.fps, root / "dist" / "ontoforge-intro.mp4", a.height)
