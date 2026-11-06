"""
Takes the screenshots of the README (docs/screenshot-*.png) and the wiki (images/ in the wiki
repository) from a demo instance with the made-up library of docs/demo_data.py, so no real
titles or paths end up in them.

    python3 docs/screenshots.py                       # all of them
    python3 docs/screenshots.py --only stats,replace-dialog
    python3 docs/screenshots.py --list                # their names and files

Needs the web UI's packages (requirements.txt) and Playwright with Chromium and WebKit
(pip install playwright && playwright install chromium webkit). The demo runs in a temporary
folder on a free port and is removed afterwards; ffprobe is replaced by a stub, the demo
never scans. Whenever a page shown here changes visually, run it again for that page and
commit the README images or push the wiki.
"""
import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent
USER, PASSWORD = "demo", "demo-pass-123"
DESKTOP = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1.5, "color_scheme": "dark"}
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "has_touch": True, "color_scheme": "dark"}


# ─── Demo instance ────────────────────────────────────────────────────────────

def make_demo(root):
    lib = root / "lib"
    for name in ("Movies", "Shows", "Anime"):
        (lib / name).mkdir(parents=True)
        (lib / name / ".keep").touch()                  # an empty share folder counts as offline
    for name in ("data", "out", "bin"):
        (root / name).mkdir()
    (root / "data" / "config.env").write_text("".join(
        f'SHARE_{n}="{lib / name}"\nSHARE_{n}_NAME="{name}"\n' for n, name in enumerate(("Movies", "Shows", "Anime"), 1))
        + f'ENCODED_DIR="{root / "out"}"\nNOTE_DIR=""\n')
    stub = root / "bin" / "ffprobe"
    # every file reads as HEVC, so Replace… suggests x265 names (the demo never scans)
    stub.write_text("#!/bin/sh\necho '{\"streams\": [{\"codec_name\": \"hevc\", \"width\": 1920, \"height\": 1080}]}'\n")
    stub.chmod(0o755)
    env = dict(os.environ, ENCODING_DATA_DIR=str(root / "data"), PATH=f"{root / 'bin'}:{os.environ['PATH']}")
    subprocess.run([sys.executable, str(REPO / "docs" / "demo_data.py")], env=env, check=True, stdout=subprocess.DEVNULL)
    return env


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_web(env, port, log):
    proc = subprocess.Popen([sys.executable, str(REPO / "webui.py"), "--port", str(port)], env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return proc
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise SystemExit("The demo web UI didn't start.")


def replace_jobs(data, keys):
    """A replace in every stage for the progress bar: two done, one moving, three waiting."""
    start = datetime.now() - timedelta(seconds=40)
    for n, key in enumerate(keys[:6]):
        job_id = (start + timedelta(seconds=n)).strftime("%Y%m%d-%H%M%S-%f")
        name = key.rsplit("/", 1)[-1]
        job = {"id": job_id, "key": key, "encoded": name, "new_name": name.replace("h264", "x265"),
               "keep_original": True, "requested": (start + timedelta(seconds=n)).strftime("%Y-%m-%d %H:%M:%S")}
        if n < 2:
            folder = data / "replace-done"
            job.update(state="done", message="Replaced.", new_key=key.rsplit("/", 1)[0] + "/" + job["new_name"],
                       finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), synced=True)
        else:
            folder = data / ("replace-running" if n == 2 else "replace")
        folder.mkdir(exist_ok=True)
        (folder / f"{job_id}.json").write_text(json.dumps(job))


def scan_running(data, pid):
    """A scan in its ffprobe phase, as encoding.ScanProgress writes it."""
    now = time.time()
    (data / "logs" / "scan.json").write_text(json.dumps({
        "pid": pid, "started": now - 400, "shares": ["share1", "share2", "share3"], "phase": "probing",
        "expected": 411, "found": 411, "checked": 411, "to_probe": 160, "probed": 58, "probe_started": now - 95,
        "updated": now}))


def opaque_bars(pg):
    """Headless WebKit draws no blur behind the translucent bars, so content would show through."""
    pg.add_style_tag(content=".top, .tabbar, .crumb-bar { background: var(--surface) !important; }")


# ─── Screenshots ──────────────────────────────────────────────────────────────

class Shots:
    def __init__(self, play, base, data, pid):
        self.play, self.base, self.data, self.pid = play, base, data, pid
        self.browsers = {}

    def page(self, engine="chromium", **opts):
        if engine not in self.browsers:
            self.browsers[engine] = getattr(self.play, engine).launch()
        ctx = self.browsers[engine].new_context(**opts)
        pg = ctx.new_page()
        pg.goto(self.base + "/login")
        pg.fill("#user", USER)
        pg.fill("#password", PASSWORD)
        pg.click(".auth-form button")
        pg.wait_for_load_state("networkidle")
        return pg

    def go(self, pg, path, wait=600):
        pg.goto(self.base + path)
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(wait)

    def settle(self, pg):
        pg.mouse.move(5, 5)                     # no hover tooltips
        pg.evaluate("window.scrollTo(0, 0)")
        pg.wait_for_timeout(400)

    def open_folders(self, pg, *names):
        for name in names:
            pg.locator("summary .dirname", has_text=name).first.click()
            pg.wait_for_load_state("networkidle")
            pg.wait_for_timeout(300)

    def tick(self, pg, *names):
        for name in names:
            pg.locator("tr", has_text=name).locator('input[name="key"]').check()

    # README: docs/screenshot-files.png and docs/screenshot-phone.png (sizes in CLAUDE.md)
    def readme_files(self, out):
        pg = self.page(**dict(DESKTOP, viewport={"width": 1280, "height": 1330}))
        self.go(pg, "/files")
        self.open_folders(pg, "Shows", "Northwind Station (2018)", "Season 02")
        self.tick(pg, "Northwind.Station.S02E01", "Northwind.Station.S02E03", "Northwind.Station.S02E05")
        self.settle(pg)
        pg.screenshot(path=out)

    def readme_phone(self, out):
        pg = self.page("webkit", **PHONE)
        self.go(pg, "/status?view=review", 1500)
        opaque_bars(pg)
        pg.screenshot(path=out)

    # Wiki: Web-UI
    def files(self, out):
        pg = self.page(**dict(DESKTOP, viewport={"width": 1480, "height": 900}))   # wide enough for the filter panel
        self.go(pg, "/files")
        self.open_folders(pg, "Movies")
        self.settle(pg)
        pg.screenshot(path=out)

    def files_phone(self, out):
        pg = self.page("webkit", **PHONE)
        self.go(pg, "/files")
        self.open_folders(pg, "Shows", "Harbor Lights (2015)", "Season 01")
        opaque_bars(pg)
        # scroll down past the folder, then up a little, so the tab bar shows (it hides while scrolling down)
        pg.evaluate("""async () => {
            const s = [...document.querySelectorAll('summary')].find(e => e.textContent.includes('Harbor Lights'));
            const y = s.getBoundingClientRect().top + scrollY - 110;
            window.scrollTo(0, y + 60);
            await new Promise(r => setTimeout(r, 400));
            window.scrollTo(0, y);
        }""")
        pg.wait_for_timeout(1000)
        pg.screenshot(path=out)

    def status(self, out):
        pg = self.page(**DESKTOP)
        self.go(pg, "/status?view=review", 1200)
        self.settle(pg)
        pg.screenshot(path=out)

    def stats(self, out):
        pg = self.page(**DESKTOP)
        self.go(pg, "/stats")
        self.settle(pg)
        pg.screenshot(path=out)

    def settings(self, out):
        pg = self.page(**DESKTOP)
        self.go(pg, "/settings/replace")
        self.settle(pg)
        pg.locator(".field .info-btn").first.click()
        pg.wait_for_timeout(300)
        pg.screenshot(path=out)

    # Wiki: Presets
    def preset_editor(self, out):
        pg = self.page(**DESKTOP)
        self.go(pg, "/presets")
        pg.locator("a[href^='/presets/']").filter(has_text="1080p").first.click()
        pg.wait_for_load_state("networkidle")
        pg.locator("button[data-tab='Video']").click()
        pg.wait_for_timeout(300)
        self.settle(pg)
        pg.screenshot(path=out)

    # Wiki: Replacing-Originals
    def replace_dialog(self, out):
        pg = self.page(**DESKTOP)
        self.go(pg, "/status?view=review", 1200)
        pg.locator("button[data-replace]").first.click()
        pg.wait_for_timeout(1200)
        pg.mouse.move(5, 5)
        box = pg.locator("dialog[data-replace-dialog]").bounding_box()
        pg.screenshot(path=out, clip={"x": box["x"] - 24, "y": box["y"] - 24,
                                      "width": box["width"] + 48, "height": box["height"] + 48})

    def replace_progress(self, out):
        with sqlite3.connect(self.data / "encoding.db") as con:
            keys = [k for (k,) in con.execute("SELECT key FROM files WHERE status = 'waiting' ORDER BY key")]
        replace_jobs(self.data, keys)
        pg = self.page(**DESKTOP)
        self.go(pg, "/status?view=review", 1500)
        pg.locator("[data-rp-toggle]").click()
        pg.wait_for_timeout(400)
        self.settle(pg)
        bar = pg.locator("[data-rp]").bounding_box()
        pg.screenshot(path=out, clip={"x": 0, "y": 0, "width": 1280, "height": bar["y"] + bar["height"] + 16})

    # Wiki: Scanning
    def scan_progress(self, out):
        scan_running(self.data, self.pid)
        pg = self.page(**DESKTOP)
        self.go(pg, "/status", 1500)
        pg.locator("#scan").scroll_into_view_if_needed()
        pg.mouse.move(5, 5)
        pg.wait_for_timeout(400)
        pg.locator("#scan").screenshot(path=out)
        (self.data / "logs" / "scan.json").unlink()

    def close(self):
        for b in self.browsers.values():
            b.close()


# name → (method, folder: "readme" or "wiki", file name); the order matters: replace-progress
# and scan-progress leave state behind that would show on later screenshots
SHOTS = {
    "readme-files": ("readme_files", "readme", "screenshot-files.png"),
    "readme-phone": ("readme_phone", "readme", "screenshot-phone.png"),
    "files": ("files", "wiki", "web-ui-files.png"),
    "files-phone": ("files_phone", "wiki", "web-ui-files-phone.png"),
    "status": ("status", "wiki", "web-ui-status.png"),
    "stats": ("stats", "wiki", "web-ui-stats.png"),
    "settings": ("settings", "wiki", "web-ui-settings.png"),
    "preset-editor": ("preset_editor", "wiki", "preset-editor.png"),
    "replace-dialog": ("replace_dialog", "wiki", "replace-dialog.png"),
    "scan-progress": ("scan_progress", "wiki", "scan-progress.png"),
    "replace-progress": ("replace_progress", "wiki", "replace-progress.png"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--only", help="comma-separated names (see --list)")
    ap.add_argument("--wiki", default=str(REPO.parent / "handlebreaks.wiki" / "images"), help="folder for the wiki images")
    ap.add_argument("--list", action="store_true", help="list the screenshots and exit")
    args = ap.parse_args()
    if args.list:
        for name, (_, where, file) in SHOTS.items():
            print(f"{name:18} {where}/{file}")
        return
    names = args.only.split(",") if args.only else list(SHOTS)
    unknown = [n for n in names if n not in SHOTS]
    if unknown:
        raise SystemExit(f"Unknown: {', '.join(unknown)} (see --list)")
    folders = {"readme": REPO / "docs", "wiki": Path(args.wiki)}
    if any(SHOTS[n][1] == "wiki" for n in names):
        folders["wiki"].mkdir(parents=True, exist_ok=True)

    root = Path(tempfile.mkdtemp(prefix="handlebreaks-demo-"))
    proc = None
    try:
        env = make_demo(root)
        data = root / "data"
        subprocess.run([sys.executable, "-c", f"import auth; auth.set_account({USER!r}, {PASSWORD!r})"],
                       cwd=REPO, env=env, check=True)
        port = free_port()
        with open(root / "web.log", "w") as log:
            proc = start_web(env, port, log)
            with sync_playwright() as play:
                shots = Shots(play, f"http://127.0.0.1:{port}", data, proc.pid)
                try:
                    for name in (n for n in SHOTS if n in names):
                        method, where, file = SHOTS[name]
                        out = folders[where] / file
                        getattr(shots, method)(str(out))
                        print(f"{name:18} {out}")
                finally:
                    shots.close()
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
