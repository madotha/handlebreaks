"""
Demo data for the README screenshot (docs/screenshot-files.png): a made-up library
(Blender open movies and invented shows), so no real titles end up in the repo.

    mkdir -p /tmp/demo/data /tmp/demo/lib/{Movies,Shows,Anime} /tmp/demo/out
    # /tmp/demo/data/config.env: SHARE_1..3 = /tmp/demo/lib/{Movies,Shows,Anime},
    #   names Movies, Shows, Anime; ENCODED_DIR=/tmp/demo/out; NOTE_DIR=""
    ENCODING_DATA_DIR=/tmp/demo/data python3 docs/demo_data.py
    ENCODING_DATA_DIR=/tmp/demo/data python3 webui.py --port 7980

ffprobe must be on PATH for the web UI (a stub is enough; the demo never scans),
otherwise the page shows a "ffprobe is not installed" notice.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import encoding as enc  # noqa: E402

enc.apply_config(enc.load_config())
random.seed(7)
movies = ["Big Buck Bunny (2008)", "Sintel (2010)", "Tears of Steel (2012)", "Elephants Dream (2006)", "Cosmos Laundromat (2015)",
          "Spring (2019)", "Agent 327 (2017)", "Caminandes (2013)", "Glass Half (2015)", "Hero (2018)", "Coffee Run (2020)",
          "Sprite Fright (2021)", "Charge (2022)", "Wing It! (2023)", "The Daily Dweebs (2017)", "Pip (2018)",
          "Monkaa (2015)", "Dragon's Pass (2014)", "Iron Lighthouse (2011)", "Paper Rivers (2016)", "Quiet Harbor (2019)",
          "Northern Lights Express (2012)", "Salt and Copper (2021)", "The Long Field (2009)", "Midnight Orchard (2018)"]
shows = {"Northwind Station (2018)": 4, "The Glass Archive (2020)": 3, "Harbor Lights (2015)": 6, "Tin Garden (2022)": 2,
         "Low Tide (2017)": 5, "Comet Street (2019)": 3, "Paper Satellites (2021)": 2, "Blue Valley Rescue (2014)": 7}
anime = {"Sky Wanderers (2016)": 2, "Lantern Academy (2020)": 3, "Iron Bloom (2018)": 2, "Starfall Kitchen (2022)": 1}
profiles = [("hevc", 1920, 1080, 2_600_000), ("h264", 1920, 1080, 9_000_000), ("h264", 1280, 720, 4_500_000),
            ("hevc", 3840, 2160, 14_000_000), ("av1", 1920, 1080, 2_200_000), ("mpeg4", 720, 400, 1_600_000), ("vc1", 1920, 1080, 15_000_000)]
infos = {}
def add(key, dur, weights):
    codec, w, h, br = random.choices(profiles, weights)[0]
    br = int(br * random.uniform(0.75, 1.3))
    status = "h265" if codec in enc.OPTIMIZED_CODECS else random.choices(["offer", "queued", "waiting"], [90, 6, 4])[0]
    infos[key] = {"key": key, "size": int(br / 8 * dur), "mtime": 1.7e9, "video_codec": codec, "width": w, "height": h,
                  "duration": dur, "bitrate": br, "probed_at": "2026-10-24 09:00", "probe_error": None, "status": status}
for m in movies:
    add(f"share1/{m}/{m.split(' (')[0]} {m[-6:]} 1080p.mkv", random.uniform(5400, 8400), [35, 40, 8, 6, 4, 3, 4])
for prefix, lib, weights in (("share2", shows, [30, 45, 12, 2, 3, 6, 2]), ("share3", anime, [50, 30, 10, 0, 8, 2, 0])):
    for s, seasons in lib.items():
        name = s.split(" (")[0].replace(" ", ".")
        bias = random.random()
        for season in range(1, seasons + 1):
            for ep in range(1, random.randint(8, 13)):
                w = [x * (2 if (i in (0, 4)) == (bias > 0.5) else 1) for i, x in enumerate(weights)]
                add(f"{prefix}/{s}/Season {season:02d}/{name}.S{season:02d}E{ep:02d}.1080p.WEB.mkv", random.uniform(1300, 2700), w)
enc.save_files(infos)
st = enc.load_state(); st["last_scan"] = "2026-10-24 09:00"; enc.save_state(st)
import sqlite3
with sqlite3.connect(enc.DB_FILE) as con:
    for p in ("share1", "share2", "share3"):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"share_scan:{p}", "2026-10-24 09:00"))
print(len(infos), "demo files")
