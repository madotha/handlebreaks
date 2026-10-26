"""
HandBrake presets: storage, import/export and what the installed HandBrakeCLI supports
=====================================================================================

Every preset is one HandBrake export file (the same JSON the HandBrake app exports)
in <data folder>/presets/<id>.json. The id is derived from the name when the preset
is created and never changes, so shares and queued files can refer to it even after
the preset is renamed.

The options offered in the web UI (video and audio encoders, encoder speed presets,
containers, mixdowns, built-in presets) are read from the installed HandBrakeCLI, so
they always match its version.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ENCODING_DATA_DIR") or BASE)
PRESET_DIR = DATA_DIR / "presets"
BUNDLED = BASE / "preset.json"                  # preset shipped with the tool, imported on first run

HANDBRAKE = shutil.which("HandBrakeCLI", path=os.pathsep.join(
    [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]))

# Shown when HandBrakeCLI isn't installed (e.g. web UI on a machine that doesn't encode)
FALLBACK = {
    "formats": ["av_mkv", "av_mp4", "av_webm"],
    "video_encoders": ["x265", "x265_10bit", "x264", "svt_av1", "svt_av1_10bit"],
    "audio_encoders": ["av_aac", "ac3", "eac3", "opus", "mp3", "flac16", "copy", "copy:aac", "copy:ac3",
                       "copy:eac3", "copy:dts", "copy:dtshd", "copy:truehd"],
    "mixdowns": ["mono", "stereo", "dpl2", "5point1", "6point1", "7point1"],
    "encoder_presets": {"x265": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                                 "slow", "slower", "veryslow", "placebo"]},
    "encoder_tunes": {"x265": ["psnr", "ssim", "grain", "zerolatency", "fastdecode", "animation"]},
    "encoder_profiles": {"x265": ["auto", "main", "mainstillpicture"]},
    "encoder_levels": {"x265": ["auto", "1.0", "2.0", "2.1", "3.0", "3.1", "4.0", "4.1", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2"]},
    "samplerates": ["8", "11.025", "12", "16", "22.05", "24", "32", "44.1", "48", "88.2", "96", "176.4", "192"],
    "dithers": ["auto", "none", "rectangular", "triangular", "triangular_hp", "lipshitz_ns"],
    # filter option of HandBrakeCLI -> {"presets": [...], "tunes": [...]}
    "filters": {
        "comb-detect": {"presets": ["permissive", "fast"]},
        "deinterlace": {"presets": ["skip-spatial", "bob"]}, "bwdif": {"presets": ["bob"]},
        "decomb": {"presets": ["bob", "eedi2", "eedi2bob"]},
        "hqdn3d": {"presets": ["ultralight", "light", "medium", "strong"]},
        "nlmeans": {"presets": ["ultralight", "light", "medium", "strong"],
                    "tunes": ["none", "film", "grain", "highmotion", "animation", "tape", "sprite"]},
        "chroma-smooth": {"presets": ["ultralight", "light", "medium", "strong", "stronger", "verystrong"],
                          "tunes": ["none", "tiny", "small", "medium", "wide", "verywide"]},
        "unsharp": {"presets": ["ultralight", "light", "medium", "strong", "stronger", "verystrong"],
                    "tunes": ["none", "ultrafine", "fine", "medium", "coarse", "verycoarse"]},
        "lapsharp": {"presets": ["ultralight", "light", "medium", "strong", "stronger", "verystrong"],
                     "tunes": ["none", "film", "grain", "animation", "sprite"]},
        "deblock": {"presets": ["ultralight", "light", "medium", "strong", "stronger", "verystrong"],
                    "tunes": ["small", "medium", "large"]},
        "colorspace": {"presets": ["bt2020", "bt709", "bt601-6-525", "bt601-6-625"]},
    },
}
CAPS_VERSION = 3                                # bump when _capabilities() returns new keys (invalidates the cache)
WORKER_FILE = DATA_DIR / "handbrake-worker.json"   # capabilities of the HandBrakeCLI that encodes (written by encode.sh)
PLATFORMS = {"darwin": "macOS", "linux": "Linux", "win32": "Windows"}
# Encoders tied to a platform or hardware (by name prefix), shown next to them in the editor
PLATFORM_NOTES = [("vt_", "macOS only (VideoToolbox)"), ("ca_", "macOS only (Apple AAC)"),
                  ("mf_", "Windows only (Media Foundation)"), ("nvenc_", "needs an NVIDIA GPU"),
                  ("qsv_", "needs Intel Quick Sync"), ("vce_", "needs an AMD GPU"), ("vcn_", "needs an AMD GPU")]


class PresetError(Exception):
    pass


# ─── Storage ──────────────────────────────────────────────────────────────────

def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "preset"


def path_of(preset_id):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", preset_id or ""):
        raise PresetError(f"Invalid preset id: {preset_id!r}")
    return PRESET_DIR / f"{preset_id}.json"


def exists(preset_id):
    try:
        return path_of(preset_id).exists()
    except PresetError:
        return False


def load(preset_id):
    """The preset object (the single entry of PresetList)."""
    try:
        data = json.loads(path_of(preset_id).read_text(encoding="utf-8"))
        return data["PresetList"][0]
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise PresetError(f"Preset {preset_id!r} can't be read: {e}")


def file_format(preset_id):
    """Preset format (VersionMajor) of the stored file, i.e. of the HandBrake that wrote it, or None."""
    try:
        return json.loads(path_of(preset_id).read_text(encoding="utf-8")).get("VersionMajor")
    except (OSError, ValueError):
        return None


def name_of(preset_id):
    try:
        return load(preset_id).get("PresetName") or preset_id
    except PresetError:
        return preset_id


def all_presets():
    """[(id, preset)] sorted by name."""
    out = []
    if PRESET_DIR.exists():
        for f in PRESET_DIR.glob("*.json"):
            try:
                out.append((f.stem, load(f.stem)))
            except PresetError:
                continue
    return sorted(out, key=lambda ip: ip[1].get("PresetName", ip[0]).lower())


def new_id(name):
    base = slug(name)
    pid, n = base, 2
    while exists(pid):
        pid, n = f"{base}-{n}", n + 1
    return pid


def save(preset_id, preset, version=None):
    """Write the preset as a HandBrake export file (atomically)."""
    check(preset)
    PRESET_DIR.mkdir(parents=True, exist_ok=True)
    version = version or version_info()
    data = {"PresetList": [preset], **version}
    target = path_of(preset_id)
    tmp = target.with_name("." + target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def delete(preset_id):
    path_of(preset_id).unlink(missing_ok=True)


def export_bytes(preset_id):
    return path_of(preset_id).read_bytes()


def check(preset):
    if not isinstance(preset, dict):
        raise PresetError("A preset must be a JSON object.")
    if not str(preset.get("PresetName", "")).strip():
        raise PresetError("The preset needs a PresetName.")
    if preset.get("Folder"):
        raise PresetError("This is a preset folder, not a preset.")
    for key in ("VideoEncoder", "FileFormat"):
        if not preset.get(key):
            raise PresetError(f"The preset needs {key}.")


def version_info():
    """VersionMajor/Minor/Micro of the installed HandBrake's preset format, or of the bundled preset."""
    caps = capabilities()
    if caps.get("version"):
        return caps["version"]
    try:
        data = json.loads(BUNDLED.read_text(encoding="utf-8"))
        return {k: data[k] for k in ("VersionMajor", "VersionMinor", "VersionMicro") if k in data}
    except (OSError, ValueError):
        return {}


def import_data(raw):
    """Import every preset of a HandBrake export file (folders are flattened).
    Returns the new ids."""
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise PresetError(f"Not a JSON file: {e}")
    if isinstance(data, dict) and "PresetList" not in data and "PresetName" in data:
        data = {"PresetList": [data]}               # a single preset object
    if not isinstance(data, dict) or not isinstance(data.get("PresetList"), list):
        raise PresetError("Not a HandBrake preset file (PresetList is missing).")
    version = {k: data[k] for k in ("VersionMajor", "VersionMinor", "VersionMicro") if k in data}

    def flatten(items):
        for p in items:
            if isinstance(p, dict) and p.get("Folder"):
                yield from flatten(p.get("ChildrenArray") or [])
            elif isinstance(p, dict):
                yield p

    ids = []
    for preset in flatten(data["PresetList"]):
        check(preset)
        pid = new_id(preset["PresetName"])
        save(pid, dict(preset, Default=False), version or None)
        ids.append(pid)
    if not ids:
        raise PresetError("The file contains no presets.")
    return ids


def bootstrap(preset_file=None, preset_name=None):
    """Make sure at least one preset exists. Imports the preset named preset_name from
    preset_file (settings of version 2.x), or the bundled preset.json. Returns the id of
    the imported preset, or None if presets already existed."""
    if all_presets():
        return None
    src = Path(preset_file) if preset_file else BUNDLED
    data = json.loads(src.read_text(encoding="utf-8"))
    version = {k: data[k] for k in ("VersionMajor", "VersionMinor", "VersionMicro") if k in data}
    candidates = [p for p in data.get("PresetList", []) if isinstance(p, dict) and not p.get("Folder")]
    chosen = next((p for p in candidates if p.get("PresetName") == preset_name), None) or candidates[0]
    pid = new_id(chosen["PresetName"])
    save(pid, dict(chosen, Default=False), version or None)
    return pid


# ─── HandBrakeCLI ─────────────────────────────────────────────────────────────

def _run(*args, timeout=60):
    out = subprocess.run([HANDBRAKE, *args], capture_output=True, text=True, timeout=timeout)
    return out.stdout + out.stderr


def _help_list(text, heading):
    """Items listed below a heading line in HandBrakeCLI --help (one per line, deeply indented)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if heading in line:
            items = []
            for item in lines[i + 1:]:
                if re.fullmatch(r" {25,}\S+", item):
                    items.append(item.strip())
                else:
                    break
            return items
    return []


def _help_blocks(text):
    """Option name -> its help text, e.g. "nlmeans" -> the lines of --nlmeans."""
    blocks, name = {}, None
    for line in text.splitlines():
        m = re.match(r"^\s{1,8}(?:-\S, )?--([\w-]+)", line)
        if m:
            name = m.group(1)
            blocks[name] = ""
        if name:
            blocks[name] += line + "\n"
    return blocks


def _encoder_list(kind, encoder):
    """--encoder-<kind>-list for one encoder (preset, tune, profile or level)."""
    text = _run(f"--encoder-{kind}-list", encoder)
    return [] if "encoder:" not in text else re.findall(r"^\s+(\S+)\s*$", text.split("encoder:", 1)[1], re.M)


CACHE_FILE = DATA_DIR / ".handbrake-cache.json"   # capabilities and built-in presets per HandBrakeCLI build


def _disk_cache(name, compute):
    """compute() once per HandBrakeCLI build (path, size, date), kept in CACHE_FILE, so
    restarts don't have to ask HandBrakeCLI again (about a second)."""
    st = os.stat(HANDBRAKE)
    build = [HANDBRAKE, st.st_size, st.st_mtime_ns, CAPS_VERSION]
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("build") == build and name in data:
            return data[name]
    except (OSError, ValueError):
        data = {}
    if data.get("build") != build:
        data = {"build": build}
    data[name] = compute()
    try:
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass                                        # no cache, compute again next time
    return data[name]


@lru_cache(maxsize=1)
def capabilities():
    """What the installed HandBrakeCLI supports. Cached in the process and on disk."""
    if not HANDBRAKE:
        return dict(FALLBACK, available=False, version=None)
    return _disk_cache("capabilities", _capabilities)


def _capabilities():
    text = _run("--help")
    caps = {
        "available": True,
        "formats": _help_list(text, "Select container format:"),
        "video_encoders": _help_list(text, "Select video encoder:"),
        "audio_encoders": [e for e in _help_list(text, "Select audio encoder(s):") if e != "none"],
        "mixdowns": _help_list(text, "Format(s) for audio downmixing/upmixing:"),
        "encoder_presets": {}, "encoder_tunes": {}, "encoder_profiles": {}, "encoder_levels": {},
    }
    for enc in caps["video_encoders"]:          # one value per line below the heading
        for kind in ("preset", "tune", "profile", "level"):
            values = _encoder_list(kind, enc)
            if values:
                caps[f"encoder_{kind}s"][enc] = values
    blocks = _help_blocks(text)
    caps["filters"] = {}
    for name in FALLBACK["filters"]:            # presets and tunes of each filter
        block = blocks.get(name, "")
        entry = {"presets": _help_list(block, "Presets:"),          # tunes have their own option, --<name>-tune
                 "tunes": _help_list(blocks.get(f"{name}-tune", ""), "Tunes:")}
        if entry["presets"] or entry["tunes"]:
            caps["filters"][name] = {k: v for k, v in entry.items() if v}
    m = re.search(r"\(([\d./]+) kHz\)", blocks.get("arate", ""))
    caps["samplerates"] = m.group(1).split("/") if m else []
    caps["dithers"] = _help_list(blocks.get("adither", ""), "Select dithering")
    caps["version"] = None
    with tempfile.TemporaryDirectory() as tmp:     # preset format and every setting it knows, via a throwaway export
        out = Path(tmp) / "v.json"
        _run("--preset-export", "v", "--preset-export-file", str(out))
        try:
            data = json.loads(out.read_text())
            caps["version"] = {k: data[k] for k in ("VersionMajor", "VersionMinor", "VersionMicro") if k in data}
            exported = data["PresetList"][0]
            caps["preset_keys"] = sorted(exported)
            caps["track_keys"] = sorted({k for t in exported.get("AudioList") or [] for k in t})
        except (OSError, ValueError, KeyError, IndexError):
            pass
    m = re.search(r"HandBrake (\S+)", _run("--version"))
    caps["handbrake"] = m.group(1) if m else None
    caps["platform"] = PLATFORMS.get(sys.platform, sys.platform)
    for key, value in FALLBACK.items():          # never leave a list empty if parsing failed
        if not caps.get(key):
            caps[key] = value
    return caps


@lru_cache(maxsize=1)
def builtin_presets():
    """HandBrake's built-in presets: [(category, name, description)]."""
    if not HANDBRAKE:
        return []
    return [tuple(p) for p in _disk_cache("builtins", _builtin_presets)]


def _builtin_presets():
    out, category, current = [], "", None
    for line in _run("--preset-list").splitlines():
        if re.fullmatch(r"\S.*/", line):
            category = line[:-1]
        elif re.fullmatch(r" {4}\S.*", line):
            current = [category, line.strip(), ""]
            out.append(current)
        elif re.fullmatch(r" {8}\S.*", line) and current:
            current[2] = (current[2] + " " + line.strip()).strip()
    return [tuple(p) for p in out]


def from_builtin(name, new_name):
    """A copy of a built-in HandBrake preset, renamed to new_name."""
    if not HANDBRAKE:
        raise PresetError("HandBrakeCLI is not installed here, so built-in presets aren't available.")
    if name not in {n for _, n, _ in builtin_presets()}:
        raise PresetError(f"Unknown built-in preset: {name}")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "p.json"
        _run("-Z", name, "--preset-export", new_name, "--preset-export-file", str(out))
        try:
            return json.loads(out.read_text())["PresetList"][0]
        except (OSError, ValueError, KeyError, IndexError):
            raise PresetError(f"HandBrakeCLI couldn't export the built-in preset {name!r}.")


# Apple's AAC encoders only exist on macOS; HandBrake elsewhere uses av_aac for them.
# Used only when the encoder itself isn't available.
AUDIO_ALIASES = {"ca_aac": "av_aac", "ca_haac": "av_aac"}


def publish_worker_capabilities():
    """Write the capabilities of this machine's HandBrakeCLI to WORKER_FILE, for a web UI
    elsewhere (e.g. in Docker while the Mac encodes) to offer and check what the worker
    really supports. Called by encode.sh; only rewrites the file when HandBrakeCLI changed.
    Returns True if it was written."""
    if not HANDBRAKE:
        return False
    st = os.stat(HANDBRAKE)
    build = [HANDBRAKE, st.st_size, st.st_mtime_ns, CAPS_VERSION]
    try:
        if json.loads(WORKER_FILE.read_text()).get("build") == build:
            return False
    except (OSError, ValueError):
        pass
    data = dict(capabilities(), build=build, published=time.time())
    tmp = WORKER_FILE.with_name(WORKER_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, WORKER_FILE)
    return True


def worker_capabilities():
    """Capabilities published by the worker (publish_worker_capabilities), or None."""
    try:
        data = json.loads(WORKER_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("available") else None


def editor_capabilities(encodes_here):
    """(caps, remote): the capabilities of the HandBrakeCLI that encodes. With encodes_here
    False (a web UI in Docker while the Mac encodes) and a published worker file, that's
    the worker's (remote = True: its HandBrakeCLI can't be run here); otherwise this
    machine's."""
    worker = None if encodes_here else worker_capabilities()
    return (worker, True) if worker else (capabilities(), False)


def platform_note(value):
    """"macOS only (VideoToolbox)" etc. for an encoder tied to a platform or hardware, or None."""
    value = str(value or "")
    return next((note for prefix, note in PLATFORM_NOTES if value.startswith(prefix)), None)


def unsupported(preset, caps=None):
    """Settings the installed HandBrakeCLI doesn't support. HandBrakeCLI itself loads
    presets with unknown encoders without complaint, so they're checked here."""
    caps = caps or capabilities()
    if not caps.get("available"):
        return []
    problems = []
    enc = preset.get("VideoEncoder")
    if enc not in caps["video_encoders"]:
        problems.append(f"video encoder {enc!r}")
    elif preset.get("VideoPreset") and caps["encoder_presets"].get(enc) and \
            preset["VideoPreset"] not in caps["encoder_presets"][enc]:
        problems.append(f"encoder speed {preset['VideoPreset']!r} for {enc}")
    if preset.get("FileFormat") not in caps["formats"]:
        problems.append(f"container {preset.get('FileFormat')!r}")
    for track in preset.get("AudioList") or []:
        audio = track.get("AudioEncoder")
        supported = caps["audio_encoders"] + ["none"]
        if audio not in supported and AUDIO_ALIASES.get(audio) not in supported:
            problems.append(f"audio encoder {track.get('AudioEncoder')!r}")
        if track.get("AudioMixdown") and track["AudioMixdown"] not in caps["mixdowns"] + ["none"]:
            problems.append(f"mixdown {track['AudioMixdown']!r}")
    return problems


def verify(preset, caps=None, remote=False):
    """Check the preset against the HandBrakeCLI that encodes and let it load it. Returns
    an error message, or None if it's fine. remote: the worker's HandBrakeCLI runs on
    another machine (caps from its published file), so only its lists are checked."""
    if remote:
        problems = unsupported(preset, caps)
        return ("Not supported by the worker's HandBrakeCLI: " + ", ".join(problems) + ".") if problems else None
    if not HANDBRAKE:
        return None
    problems = unsupported(preset)
    if problems:
        return "Not supported by the installed HandBrakeCLI: " + ", ".join(problems) + "."
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        src.write_text(json.dumps({"PresetList": [preset], **version_info()}))
        log = _run("--preset-import-file", str(src), "-Z", preset["PresetName"],
                   "--preset-export", "check", "--preset-export-file", str(out))
        if not out.exists():
            lines = [l for l in log.splitlines() if l.strip() and not l.startswith("[")]
            return "HandBrakeCLI can't load this preset: " + (" ".join(lines[-2:]) or "unknown error")
    return None


# ─── Form fields (the common settings) ────────────────────────────────────────

# Resolution limits offered in the form: label -> (width, height); None = source size
RESOLUTIONS = {"Source": None, "2160p (4K)": (3840, 2160), "1440p": (2560, 1440), "1080p": (1920, 1080),
               "720p": (1280, 720), "576p": (1024, 576), "480p": (854, 480)}
FRAMERATES = ["auto", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60"]
TRACK_BEHAVIORS = {"none": "None", "first": "First matching track", "all": "All matching tracks"}
BURN_BEHAVIORS = {"none": "Never", "foreign": "Foreign audio search", "first": "First track",
                  "foreign_first": "Foreign audio search, then first track"}


def resolution_of(preset):
    w, h = preset.get("PictureWidth") or 0, preset.get("PictureHeight") or 0
    for label, size in RESOLUTIONS.items():
        if size and (w, h) == size:
            return label
    return "Source" if not w or w >= 7680 else f"{w}×{h}"


def langs(value):
    """'ger, eng' -> ['ger', 'eng'] (ISO 639-2 codes, as HandBrake uses them)."""
    return [x for x in re.split(r"[,\s]+", value.strip().lower()) if x]


# ─── Editor ───────────────────────────────────────────────────────────────────
# Every setting of a HandBrake preset, grouped like the tabs of the HandBrake app. The
# web UI renders the form from editor() and saves it with apply_form(). Settings not
# listed here (e.g. from a newer HandBrake) appear under "More", by their type, so
# nothing is lost. Choices come from the installed HandBrakeCLI (capabilities()).

INTERNAL = {"ChildrenArray", "Default", "Folder", "FolderOpen", "Type", "PresetDisabled"}  # HandBrake app bookkeeping
# Older settings HandBrake still exports for compatibility; shown under "More" with a note
LEGACY = {"PictureAutoCrop": "replaced by Crop", "x264Option": "replaced by Extra encoder options",
          "x264UseAdvancedOptions": "no longer used"}

LABELS = {"off": "Off", "default": "Default", "custom": "Custom", "none": "None", "auto": "Automatic",
          "yadif": "Yadif", "decomb": "Decomb", "bwdif": "Bwdif", "nlmeans": "NLMeans", "hqdn3d": "HQDN3D",
          "unsharp": "Unsharp", "lapsharp": "Lapsharp", "av_mkv": "MKV", "av_mp4": "MP4", "av_webm": "WebM",
          "vfr": "Variable (same as source)", "cfr": "Constant", "pfr": "Peak (limit to the frame rate)",
          "loose": "Loose", "swscale": "swscale (default)", "zscale": "zscale", "letterbox": "Letterbox",
          "pillarbox": "Pillarbox", "fill": "Fill"}
CROP_MODES = {0: "Automatic", 1: "Conservative", 2: "None", 3: "Custom"}
QUALITY_TYPES = {2: "Constant quality (RF)", 1: "Average bitrate"}


def F(key, label, kind="text", options=None, hint="", show=None, **attrs):
    """One editor field. kind: text, int, float, bool, select, langs (comma separated
    language codes), multi (checkboxes, a list). options: list, dict value -> label, or
    {"depends": other key, "map": {its value: list}}. show: {other key: [values]} (a
    value starting with ! means "not"), the field is only shown then."""
    return dict(key=key, label=label, kind=kind, options=options, hint=hint, show=show or {}, attrs=attrs)


def _filter(caps, name, kind="presets"):
    return list(caps.get("filters", {}).get(name, {}).get(kind, [])) or list(FALLBACK["filters"].get(name, {}).get(kind, []))


def editor_schema(caps):
    """[(tab, [field])] for the preset editor."""
    f = lambda name, kind="presets": _filter(caps, name, kind)
    copy_encoders = [e for e in caps["audio_encoders"] if e.startswith("copy:")]
    plain_encoders = [e for e in caps["audio_encoders"] if not e.startswith("copy")]
    by_encoder = lambda k, extra=(): {"depends": "VideoEncoder",
                                       "map": {e: list(extra) + v for e, v in caps.get(k, {}).items()}}
    return [
        ("Summary", [
            F("PresetName", "Name", required=True),
            F("PresetDescription", "Description"),
            F("FileFormat", "Container", "select", caps["formats"]),
            F("Optimize", "Web optimized (moov atom at the start, for streaming)", "bool", show={"FileFormat": ["av_mp4"]}),
            F("Mp4iPodCompatible", "iPod 5G support", "bool", show={"FileFormat": ["av_mp4"]}),
            F("AlignAVStart", "Align A/V start (pad the start so audio and video begin together)", "bool"),
            F("InlineParameterSets", "Inline parameter sets (for some players and streaming)", "bool"),
            F("MetadataPassthru", "Pass through metadata (title, year, …) from the source", "bool"),
            F("ChapterMarkers", "Chapter markers", "bool"),
        ]),
        ("Dimensions", [
            F("PictureCropMode", "Crop", "select", CROP_MODES, cast=int),
            F("PictureTopCrop", "Crop top", "int", show={"PictureCropMode": ["3"]}, min=0),
            F("PictureBottomCrop", "Crop bottom", "int", show={"PictureCropMode": ["3"]}, min=0),
            F("PictureLeftCrop", "Crop left", "int", show={"PictureCropMode": ["3"]}, min=0),
            F("PictureRightCrop", "Crop right", "int", show={"PictureCropMode": ["3"]}, min=0),
            F("PictureWidth", "Maximum width", "int", "Larger sources are scaled down to fit. 0 = no limit.", min=0, quick="size"),
            F("PictureHeight", "Maximum height", "int", "0 = no limit.", min=0),
            F("PictureUseMaximumSize", "Use the width and height as a maximum (keeps smaller sources as they are)", "bool"),
            F("PictureAllowUpscaling", "Allow upscaling smaller sources", "bool"),
            F("PictureKeepRatio", "Keep the aspect ratio", "bool"),
            F("PictureForceWidth", "Force width", "int", "0 = off. Overrides the maximum.", min=0),
            F("PictureForceHeight", "Force height", "int", "0 = off.", min=0),
            F("PictureModulus", "Modulus (sizes divisible by)", "select", [2, 4, 8, 16], cast=int),
            F("PicturePAR", "Anamorphic", "select", ["off", "auto", "loose", "custom"]),
            F("PicturePARWidth", "Pixel aspect width", "int", show={"PicturePAR": ["custom"]}, min=1),
            F("PicturePARHeight", "Pixel aspect height", "int", show={"PicturePAR": ["custom"]}, min=1),
            F("PictureDARWidth", "Display width", "int", "0 = from the pixel aspect.", show={"PicturePAR": ["custom"]}, min=0),
            F("PictureItuPAR", "Use ITU pixel aspect (for underscanned sources)", "bool"),
            F("_rotate_angle", "Rotate", "select", {"0": "No rotation", "90": "90° clockwise", "180": "180°", "270": "270° clockwise"}),
            F("_rotate_flip", "Flip horizontally", "bool"),
            F("PicturePadMode", "Borders (padding)", "select", ["none", "letterbox", "pillarbox", "fill", "custom"]),
            F("PicturePadColor", "Border color", "text", "An HTML color name (black, white, …) or #rrggbb.",
              show={"PicturePadMode": ["!none"]}, list="pad-colors"),
            F("PicturePadTop", "Border top", "int", show={"PicturePadMode": ["custom"]}, min=0),
            F("PicturePadBottom", "Border bottom", "int", show={"PicturePadMode": ["custom"]}, min=0),
            F("PicturePadLeft", "Border left", "int", show={"PicturePadMode": ["custom"]}, min=0),
            F("PicturePadRight", "Border right", "int", show={"PicturePadMode": ["custom"]}, min=0),
        ]),
        ("Filters", [
            F("PictureDetelecine", "Detelecine", "select", ["off", "default", "custom"]),
            F("PictureDetelecineCustom", "Detelecine parameters", show={"PictureDetelecine": ["custom"]}, mono=True),
            F("PictureCombDetectPreset", "Interlace detection", "select", ["off", "default"] + f("comb-detect") + ["custom"]),
            F("PictureCombDetectCustom", "Interlace detection parameters", show={"PictureCombDetectPreset": ["custom"]}, mono=True),
            F("PictureDeinterlaceFilter", "Deinterlace", "select", ["off", "yadif", "decomb", "bwdif"]),
            F("PictureDeinterlacePreset", "Deinterlace preset", "select",
              {"depends": "PictureDeinterlaceFilter", "map": {"yadif": ["default"] + f("deinterlace") + ["custom"],
                                                              "decomb": ["default"] + f("decomb") + ["custom"],
                                                              "bwdif": ["default"] + f("bwdif") + ["custom"]}},
              show={"PictureDeinterlaceFilter": ["!off"]}),
            F("PictureDeinterlaceCustom", "Deinterlace parameters", show={"PictureDeinterlacePreset": ["custom"]}, mono=True),
            F("PictureDenoiseFilter", "Denoise", "select", ["off", "nlmeans", "hqdn3d"]),
            F("PictureDenoisePreset", "Denoise preset", "select",
              {"depends": "PictureDenoiseFilter", "map": {"nlmeans": f("nlmeans") + ["custom"], "hqdn3d": f("hqdn3d") + ["custom"]}},
              show={"PictureDenoiseFilter": ["!off"]}),
            F("PictureDenoiseTune", "Denoise tune", "select", f("nlmeans", "tunes"), show={"PictureDenoiseFilter": ["nlmeans"]}),
            F("PictureDenoiseCustom", "Denoise parameters", show={"PictureDenoisePreset": ["custom"]}, mono=True),
            F("PictureChromaSmoothPreset", "Chroma smooth", "select", ["off"] + f("chroma-smooth") + ["custom"]),
            F("PictureChromaSmoothTune", "Chroma smooth tune", "select", f("chroma-smooth", "tunes"),
              show={"PictureChromaSmoothPreset": ["!off", "!custom"]}),
            F("PictureChromaSmoothCustom", "Chroma smooth parameters", show={"PictureChromaSmoothPreset": ["custom"]}, mono=True),
            F("PictureSharpenFilter", "Sharpen", "select", ["off", "unsharp", "lapsharp"]),
            F("PictureSharpenPreset", "Sharpen preset", "select",
              {"depends": "PictureSharpenFilter", "map": {"unsharp": f("unsharp") + ["custom"], "lapsharp": f("lapsharp") + ["custom"]}},
              show={"PictureSharpenFilter": ["!off"]}),
            F("PictureSharpenTune", "Sharpen tune", "select",
              {"depends": "PictureSharpenFilter", "map": {"unsharp": f("unsharp", "tunes"), "lapsharp": f("lapsharp", "tunes")}},
              show={"PictureSharpenFilter": ["!off"]}),
            F("PictureSharpenCustom", "Sharpen parameters", show={"PictureSharpenPreset": ["custom"]}, mono=True),
            F("PictureDeblockPreset", "Deblock", "select", ["off"] + f("deblock") + ["custom"]),
            F("PictureDeblockTune", "Deblock tune", "select", f("deblock", "tunes"), show={"PictureDeblockPreset": ["!off", "!custom"]}),
            F("PictureDeblockCustom", "Deblock parameters", show={"PictureDeblockPreset": ["custom"]}, mono=True),
            F("PictureColorspacePreset", "Color space", "select", ["off"] + f("colorspace") + ["custom"]),
            F("PictureColorspaceCustom", "Color space parameters", show={"PictureColorspacePreset": ["custom"]}, mono=True),
            F("VideoGrayScale", "Grayscale", "bool"),
            F("UsesPictureFilters", "Use the filters above", "bool"),
        ]),
        ("Video", [
            F("VideoEncoder", "Encoder", "select", caps["video_encoders"]),
            F("VideoPreset", "Encoder speed", "select", by_encoder("encoder_presets"), "Slower = smaller files at the same quality."),
            F("VideoTune", "Tune", "select", by_encoder("encoder_tunes", [""])),
            F("VideoProfile", "Profile", "select", by_encoder("encoder_profiles")),
            F("VideoLevel", "Level", "select", by_encoder("encoder_levels")),
            F("VideoQualityType", "Quality", "select", QUALITY_TYPES, cast=int),
            F("VideoQualitySlider", "RF value", "float", "Lower = better quality, bigger files. Typical for x265: 20–24.",
              show={"VideoQualityType": ["2"]}, min=0, max=63, step=0.5),
            F("VideoAvgBitrate", "Bitrate (kbit/s)", "int", show={"VideoQualityType": ["1"]}, min=100, step=100),
            F("VideoMultiPass", "Multi-pass encoding", "bool", show={"VideoQualityType": ["1"]}),
            F("VideoTurboMultiPass", "Turbo first pass", "bool", show={"VideoQualityType": ["1"]}),
            F("VideoFramerate", "Frame rate", "select", FRAMERATES),
            F("VideoFramerateMode", "Frame rate mode", "select", ["vfr", "cfr", "pfr"]),
            F("VideoOptionExtra", "Extra encoder options", "text",
              "Passed to the encoder as is, e.g. for x265: aq-mode=3:psy-rd=2.", mono=True),
            F("VideoHWDecode", "Hardware decoding", "int", "0 = software decoding. Other values enable a hardware decoder "
              "if HandBrakeCLI supports one here (as set by the HandBrake app).", min=0),
            F("VideoQSVDecode", "Intel Quick Sync decoding", "bool"),
            F("VideoColorMatrixCodeOverride", "Color matrix override", "int", "0 = as in the source.", min=0),
            F("VideoScaler", "Scaler", "select", ["swscale", "zscale"]),
            F("VideoColorRange", "Color range", "select", {"auto": "Same as source", "limited": "Limited (TV, usual)", "full": "Full (PC)"}),
            F("VideoPasshtruHDRDynamicMetadata", "Keep HDR dynamic metadata", "select",   # sic: HandBrake's key
              {"off": "Off", "hdr10plus": "HDR10+", "dolbyvision": "Dolby Vision", "all": "HDR10+ and Dolby Vision"},
              "Passes HDR10+ / Dolby Vision metadata of HDR sources through (HandBrake 1.10 and later)."),
        ]),
        ("Audio", [
            F("AudioTrackSelectionBehavior", "Tracks", "select", TRACK_BEHAVIORS),
            F("AudioLanguageList", "Languages", "langs", "ISO 639-2 codes, e.g. ger, eng. Empty = any language."),
            F("AudioSecondaryEncoderMode", "Use only the first track's encoder for further languages", "bool"),
            F("AudioCopyMask", "Allowed passthru (copy)", "multi", copy_encoders,
              "Codecs that \"copy\" tracks keep as they are; other tracks use the fallback encoder."),
            F("AudioEncoderFallback", "Passthru fallback encoder", "select", plain_encoders + ["none"]),
            F("AudioTrackNamePassthru", "Keep the source's track names", "bool"),
            F("AudioAutomaticNamingBehavior", "Name tracks automatically", "select",
              {"off": "Off", "unnamed": "Only tracks without a name", "all": "All tracks"},
              "Names like \"Stereo\" or \"Surround 5.1\" (HandBrake 1.10 and later)."),
        ]),
        ("Subtitles", [
            F("SubtitleTrackSelectionBehavior", "Tracks", "select", TRACK_BEHAVIORS),
            F("SubtitleLanguageList", "Languages", "langs", "ISO 639-2 codes. Empty = any language."),
            F("SubtitleAddForeignAudioSearch", "Foreign audio search (finds forced subtitles for foreign-language scenes)", "bool"),
            F("SubtitleAddForeignAudioSubtitle", "Add a subtitle track for foreign audio", "bool"),
            F("SubtitleAddCC", "Add closed captions", "bool"),
            F("SubtitleBurnBehavior", "Burn in", "select", BURN_BEHAVIORS),
            F("SubtitleBurnBDSub", "Burn in Blu-ray subtitles (PGS)", "bool"),
            F("SubtitleBurnDVDSub", "Burn in DVD subtitles (VobSub)", "bool"),
            F("SubtitleTrackNamePassthru", "Keep the source's track names", "bool"),
        ]),
    ]


def audio_track_schema(caps):
    """Fields of one audio track (AudioList entry)."""
    return [
        F("AudioEncoder", "Encoder", "select", caps["audio_encoders"]),
        F("AudioMixdown", "Mixdown", "select", caps["mixdowns"] + ["none"]),
        F("AudioSamplerate", "Sample rate (kHz)", "select", ["auto"] + list(caps.get("samplerates") or FALLBACK["samplerates"])),
        F("AudioTrackQualityEnable", "Use quality instead of bitrate", "bool"),
        F("AudioBitrate", "Bitrate (kbit/s)", "int", show={"AudioTrackQualityEnable": ["!1"]}, min=0, step=16),
        F("AudioTrackQuality", "Quality", "float", show={"AudioTrackQualityEnable": ["1"]}, step=0.1),
        F("AudioTrackGainSlider", "Gain (dB)", "float", "Negative = quieter. Not for passthru.", min=-20, max=20, step=0.5),
        F("AudioTrackDRCSlider", "Dynamic range compression", "float", "0 = off; 1.5–2.5 makes soft sounds louder.", min=0, max=4, step=0.1),
        F("AudioCompressionLevel", "Compression level", "float", "-1 = encoder default.", step=1),
        F("AudioDitherMethod", "Dither", "select", list(caps.get("dithers") or FALLBACK["dithers"])),
        F("AudioNormalizeMixLevel", "Normalize mix level", "bool"),
    ]


def _options(options, value):
    """[(value, label)] of a static option list, with the current value added if missing."""
    if options is None:
        return []
    items = list(options.items()) if isinstance(options, dict) else [(o, LABELS.get(o, o) if o != "" else "None") for o in options]
    items = [(str(v), str(l) + (f" · {platform_note(v)}" if platform_note(v) else "")) for v, l in items]
    if value is not None and str(value) not in {v for v, _ in items}:
        note = (f"uses {AUDIO_ALIASES[value]} there" if value in AUDIO_ALIASES else "not available") + \
            " with the encoding HandBrakeCLI" + (f"; {platform_note(value)}" if platform_note(value) else "")
        items.insert(0, (str(value), f"{value} ({note})"))
    return items


def _resolve(field, value, values=None):
    out = dict(field, value=value)
    opts = field["options"]
    if isinstance(opts, dict) and "depends" in opts:
        out["depends"] = opts["depends"]
        out["dmap"] = {k: _options(v, None) for k, v in opts["map"].items()}
        parent = (values or {}).get(opts["depends"])        # the choices for the current value of the other field
        out["options"] = _options(opts["map"].get(str(parent), []), value)
    elif field["kind"] == "multi":
        out["options"] = _options(opts, None)
    else:
        out["options"] = _options(opts, value) if field["kind"] == "select" else []
    return out


def _rotate(preset):
    m = dict(kv.split("=", 1) for kv in str(preset.get("PictureRotate") or "angle=0:hflip=0").split(":") if "=" in kv)
    return m.get("angle", "0"), m.get("hflip", "0") == "1"


def _generic(key, value):
    """A "More" field for a setting the editor doesn't know, by its type."""
    kind = ("bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "float" if isinstance(value, float)
            else "langs" if isinstance(value, list) and all(isinstance(x, str) for x in value)
            else "text" if isinstance(value, str) or value is None else "json")
    label = f"{key} (old setting, {LEGACY[key]})" if key in LEGACY else key
    return dict(F(key, label, kind, mono=True), value=value)


def _mark(field, known):
    """Flag a field whose setting the encoding HandBrakeCLI doesn't know (it ignores it)."""
    key = "PictureRotate" if field["key"].startswith("_rotate") else field["key"]
    if known and key not in known:
        field["unsupported"] = True
    return field


def editor(preset, caps):
    """The preset editor: [(tab, [field with value])], audio track fields, the tracks.
    Fields whose setting the encoding HandBrakeCLI doesn't know get "unsupported"."""
    angle, flip = _rotate(preset)
    values = dict(preset, _rotate_angle=angle, _rotate_flip=flip)
    known, track_known = set(caps.get("preset_keys") or []), set(caps.get("track_keys") or [])
    tabs = [(tab, [_mark(_resolve(f, values.get(f["key"]), values), known) for f in fields])
            for tab, fields in editor_schema(caps)]
    schema_keys = {f["key"] for _, fields in editor_schema(caps) for f in fields} | {"AudioList", "PictureRotate"} | INTERNAL
    more = [_mark(_generic(k, v), known) for k, v in preset.items() if k not in schema_keys]
    if more:
        tabs.append(("More", more))
    track_fields = audio_track_schema(caps)
    tracks = [[_mark(_resolve(f, t.get(f["key"])), track_known) for f in track_fields] for t in (preset.get("AudioList") or [])]
    template = [_mark(_resolve(f, DEFAULT_TRACK.get(f["key"])), track_known) for f in track_fields]
    return tabs, tracks, template


def compatibility(preset, caps, remote, preset_format=None):
    """What the editor shows about the HandBrakeCLI that encodes: {"worker": description,
    "notes": [...], "missing": settings that HandBrakeCLI knows but the editor has no
    field for}. Each note says what won't work, or works differently, when encoding."""
    known = set(caps.get("preset_keys") or [])
    where = f"HandBrakeCLI {caps.get('handbrake') or '?'} on {caps.get('platform') or '?'}"
    worker = where + (" (the encoding worker; its capabilities were published to this web UI)" if remote else "")
    notes = []
    fmt = (caps.get("version") or {}).get("VersionMajor")
    if preset_format and fmt and preset_format > fmt:
        notes.append(f"This preset comes from a newer HandBrake (preset format {preset_format}); the encoding "
                     f"HandBrakeCLI reads format {fmt} and ignores settings it doesn't know (marked below).")
    ignored = sorted(k for k in preset if known and k not in known and k not in INTERNAL)
    if ignored:
        notes.append("Ignored when encoding (unknown to the encoding HandBrakeCLI): " + ", ".join(ignored) + ".")
    for problem in unsupported(preset, caps):
        notes.append(f"Not available with the encoding HandBrakeCLI: {problem}.")
    for value in ([preset.get("VideoEncoder"), preset.get("AudioEncoderFallback")]
                  + [t.get("AudioEncoder") for t in preset.get("AudioList") or []]):
        if platform_note(value):
            notes.append(f"{value}: {platform_note(value)}; " + (f"HandBrakeCLI on other platforms uses {AUDIO_ALIASES[value]} instead."
                         if value in AUDIO_ALIASES else "a worker on another platform can't encode with this preset."))
    schema = {f["key"] for _, fields in editor_schema(caps) for f in fields} | {"AudioList", "PictureRotate"} | INTERNAL | set(LEGACY)
    missing = sorted(k for k in known if k not in schema and k not in preset)
    return {"worker": worker, "notes": list(dict.fromkeys(notes)), "missing": missing}


def audit(caps):
    """Compatibility report of the editor and the stored presets against a HandBrakeCLI
    (encoding.py preset-check). Run it after every new HandBrake release: settings the
    editor has no field for need one in editor_schema() / audio_track_schema()."""
    fmt = (caps.get("version") or {}).get("VersionMajor")
    lines = [f"HandBrakeCLI {caps.get('handbrake') or '?'} on {caps.get('platform') or '?'}, preset format {fmt}"]
    known, track_known = set(caps.get("preset_keys") or []), set(caps.get("track_keys") or [])
    schema = {f["key"] for _, fields in editor_schema(caps) for f in fields if not f["key"].startswith("_")}
    track_schema = {f["key"] for f in audio_track_schema(caps)}
    missing = compatibility({}, caps, False)["missing"] + sorted(track_known - track_schema)
    lines.append("Settings without an editor field: " + (", ".join(missing) or "none"))
    unknown = sorted((schema - known) | (track_schema - track_known)) if known else []
    lines.append("Editor fields this HandBrakeCLI doesn't know (marked as ignored): " + (", ".join(unknown) or "none"))
    for item in all_presets():
        pid = item[0] if isinstance(item, (tuple, list)) else item["id"]
        try:
            notes = compatibility(load(pid), caps, False, file_format(pid))["notes"]
        except PresetError as e:
            notes = [str(e)]
        lines.append(f"Preset {name_of(pid)}: " + (" ".join(notes) if notes else "fully supported"))
    return lines


DEFAULT_TRACK = {"AudioEncoder": "av_aac", "AudioMixdown": "dpl2", "AudioSamplerate": "auto", "AudioBitrate": 160,
                 "AudioTrackQualityEnable": False, "AudioTrackQuality": -1.0, "AudioTrackGainSlider": 0.0,
                 "AudioTrackDRCSlider": 0.0, "AudioCompressionLevel": -1.0, "AudioDitherMethod": "auto",
                 "AudioNormalizeMixLevel": False}


def _parse(field, form, name, old):
    """The form's value for one field (name = its input name); old if it's empty or missing."""
    kind, label = field["kind"], field["label"]
    if kind == "bool":
        return form.get(name) == "1"
    if kind == "multi":
        return form.getlist(name)
    if name not in form:
        return old
    raw = form.get(name, "").strip()
    try:
        if kind == "int":
            return int(float(raw)) if raw else old
        if kind == "float":
            return float(raw) if raw else old
    except ValueError:
        raise PresetError(f"{label}: \"{raw}\" isn't a number.")
    if kind == "langs":
        return langs(raw)
    if kind == "json":
        try:
            return json.loads(raw) if raw else old
        except ValueError as e:
            raise PresetError(f"{label}: invalid JSON ({e}).")
    if kind == "select" and field["attrs"].get("cast") is int:
        return int(raw)
    if kind == "select" and isinstance(old, (int, float)) and not isinstance(old, bool) and re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return type(old)(float(raw))
    return raw


def _store(d, key, value, default=None):
    """Set d[key], but don't add a setting the preset doesn't have unless it differs from
    the default, and keep an unchanged number as it was (0 stays 0, not 0.0), so saving
    an unchanged form changes nothing."""
    if key not in d and (value in (None, "", [], False) or value == default):
        return
    old = d.get(key)
    if isinstance(old, (int, float)) and not isinstance(old, bool) and isinstance(value, (int, float)) and old == value:
        return
    d[key] = value


def apply_form(preset, form, caps=None):
    """A copy of the preset with every setting of the editor form applied."""
    caps = caps or capabilities()
    p = json.loads(json.dumps(preset))
    angle, flip = _rotate(p)
    for _, fields in editor_schema(caps):
        for f in fields:
            k = f["key"]
            if k == "_rotate_angle":
                angle = _parse(f, form, k, angle)
            elif k == "_rotate_flip":
                flip = _parse(f, form, k, flip)
            else:
                _store(p, k, _parse(f, form, k, p.get(k)))
    _store(p, "PictureRotate", f"angle={angle}:hflip={1 if flip else 0}", "angle=0:hflip=0")
    p["PresetName"] = (p.get("PresetName") or "").strip()
    known = {f["key"] for _, fields in editor_schema(caps) for f in fields} | {"AudioList", "PictureRotate"} | INTERNAL
    for k, v in list(p.items()):                # settings the editor doesn't know ("More")
        if k not in known:
            p[k] = _parse(_generic(k, v), form, k, v)
    enc = p.get("VideoEncoder")                  # tune, profile and level must belong to the encoder
    for key, kind, empty in (("VideoTune", "encoder_tunes", ""), ("VideoProfile", "encoder_profiles", "auto"),
                             ("VideoLevel", "encoder_levels", "auto"), ("VideoPreset", "encoder_presets", None)):
        allowed = caps.get(kind, {}).get(enc)
        if allowed and p.get(key) not in allowed and p.get(key) != empty:
            p[key] = empty if empty is not None else ("medium" if "medium" in allowed else allowed[len(allowed) // 2])
    # Audio tracks: a<n>-<field>; a<n>-_orig is the track's index in the saved preset (-1 = new)
    old_tracks = p.get("AudioList") or []
    indices = sorted({int(m.group(1)) for k in form.keys() if (m := re.fullmatch(r"a(\d+)-_orig", k))})
    tracks = []
    for i in indices:
        orig = int(form.get(f"a{i}-_orig", -1))
        track = dict(old_tracks[orig]) if 0 <= orig < len(old_tracks) else dict(DEFAULT_TRACK)
        for f in audio_track_schema(caps):
            _store(track, f["key"], _parse(f, form, f"a{i}-{f['key']}", track.get(f["key"])), DEFAULT_TRACK.get(f["key"]))
        tracks.append(track)
    if indices or form.get("audio-tracks") == "0":
        p["AudioList"] = tracks
    return p


def quality_tag(preset):
    """The preset's quality for file names: "rf20" (constant quality) or "avg2000" (average
    bitrate in kbit/s). encode.sh computes the same when it encodes."""
    if preset.get("VideoQualityType", 2) == 1:
        return "avg%d" % int(preset.get("VideoAvgBitrate") or 0)
    return "rf" + format(float(preset.get("VideoQualitySlider") or 0), "g")


def summary(preset):
    """One-line description of the key settings."""
    q = (f"RF {preset.get('VideoQualitySlider')}" if preset.get("VideoQualityType", 2) == 2
         else f"{preset.get('VideoAvgBitrate')} kbit/s")
    audio = (preset.get("AudioList") or [{}])[0]
    parts = [preset.get("VideoEncoder", "?"), preset.get("VideoPreset") or "", q, resolution_of(preset),
             (preset.get("FileFormat") or "").replace("av_", "").upper(),
             f"audio {audio.get('AudioEncoder', '?')}"]
    return " · ".join(x for x in parts if x)
