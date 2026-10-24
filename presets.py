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
import tempfile
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
}


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


@lru_cache(maxsize=1)
def capabilities():
    """What the installed HandBrakeCLI supports. Cached for the lifetime of the process."""
    if not HANDBRAKE:
        return dict(FALLBACK, available=False, version=None)
    text = _run("--help")
    caps = {
        "available": True,
        "formats": _help_list(text, "Select container format:"),
        "video_encoders": _help_list(text, "Select video encoder:"),
        "audio_encoders": [e for e in _help_list(text, "Select audio encoder(s):") if e != "none"],
        "mixdowns": _help_list(text, "Format(s) for audio downmixing/upmixing:"),
        "encoder_presets": {},
    }
    for enc in caps["video_encoders"]:          # one value per line below the heading
        text = _run("--encoder-preset-list", enc)
        values = [] if "encoder:" not in text else re.findall(r"^\s+(\S+)\s*$", text.split("encoder:", 1)[1], re.M)
        if values:
            caps["encoder_presets"][enc] = values
    caps["version"] = None
    with tempfile.TemporaryDirectory() as tmp:     # preset format version via a throwaway export
        out = Path(tmp) / "v.json"
        _run("--preset-export", "v", "--preset-export-file", str(out))
        try:
            data = json.loads(out.read_text())
            caps["version"] = {k: data[k] for k in ("VersionMajor", "VersionMinor", "VersionMicro") if k in data}
        except (OSError, ValueError):
            pass
    for key, value in FALLBACK.items():          # never leave a list empty if parsing failed
        if not caps.get(key):
            caps[key] = value
    return caps


@lru_cache(maxsize=1)
def builtin_presets():
    """HandBrake's built-in presets: [(category, name, description)]."""
    if not HANDBRAKE:
        return []
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


# Apple's AAC encoders only exist on macOS; HandBrake elsewhere uses av_aac for them
AUDIO_ALIASES = {"ca_aac": "av_aac", "ca_haac": "av_aac"}


def unsupported(preset):
    """Settings the installed HandBrakeCLI doesn't support. HandBrakeCLI itself loads
    presets with unknown encoders without complaint, so they're checked here."""
    caps = capabilities()
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
        audio = AUDIO_ALIASES.get(track.get("AudioEncoder"), track.get("AudioEncoder"))
        if audio not in caps["audio_encoders"] + ["none"]:
            problems.append(f"audio encoder {track.get('AudioEncoder')!r}")
        if track.get("AudioMixdown") and track["AudioMixdown"] not in caps["mixdowns"] + ["none"]:
            problems.append(f"mixdown {track['AudioMixdown']!r}")
    return problems


def verify(preset):
    """Check the preset against the installed HandBrakeCLI and let it load it. Returns
    an error message, or None if it's fine (or HandBrakeCLI isn't installed, so it
    can't be checked here)."""
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


def apply_form(preset, form):
    """Apply the form's common settings to a copy of the preset."""
    p = json.loads(json.dumps(preset))
    p["PresetName"] = form.get("PresetName", "").strip()
    p["PresetDescription"] = form.get("PresetDescription", "").strip()
    p["FileFormat"] = form.get("FileFormat") or p.get("FileFormat")

    encoder = form.get("VideoEncoder") or p.get("VideoEncoder")
    if encoder != p.get("VideoEncoder"):         # profile/level/tune are encoder-specific
        p.update(VideoProfile="auto", VideoLevel="auto", VideoTune="")
    p["VideoEncoder"] = encoder
    p["VideoPreset"] = form.get("VideoPreset", p.get("VideoPreset", ""))
    if form.get("QualityMode") == "bitrate":
        p["VideoQualityType"] = 1
        p["VideoAvgBitrate"] = int(float(form.get("VideoAvgBitrate") or p.get("VideoAvgBitrate") or 6000))
    else:
        p["VideoQualityType"] = 2
        p["VideoQualitySlider"] = float(form.get("VideoQualitySlider") or p.get("VideoQualitySlider") or 22)
    fps = form.get("VideoFramerate", "auto")
    p["VideoFramerate"] = fps
    p["VideoFramerateMode"] = "vfr" if fps == "auto" else "pfr"

    res = form.get("Resolution", "Source")
    if res in RESOLUTIONS:
        w, h = RESOLUTIONS[res] or (0, 0)
        p.update(PictureWidth=w, PictureHeight=h, PictureUseMaximumSize=True, PictureAllowUpscaling=False)

    p["AudioTrackSelectionBehavior"] = form.get("AudioTrackSelectionBehavior", "first")
    p["AudioLanguageList"] = langs(form.get("AudioLanguageList", ""))
    audio = dict((p.get("AudioList") or [{}])[0])
    audio["AudioEncoder"] = form.get("AudioEncoder") or audio.get("AudioEncoder", "av_aac")
    if form.get("AudioBitrate"):
        audio["AudioBitrate"] = int(form["AudioBitrate"])
    if form.get("AudioMixdown"):
        audio["AudioMixdown"] = form["AudioMixdown"]
    p["AudioList"] = [audio] + list(p.get("AudioList") or [])[1:]

    p["SubtitleTrackSelectionBehavior"] = form.get("SubtitleTrackSelectionBehavior", "none")
    p["SubtitleLanguageList"] = langs(form.get("SubtitleLanguageList", ""))
    p["SubtitleBurnBehavior"] = form.get("SubtitleBurnBehavior", p.get("SubtitleBurnBehavior", "none"))
    p["ChapterMarkers"] = bool(form.get("ChapterMarkers"))
    return p


def summary(preset):
    """One-line description of the key settings."""
    q = (f"RF {preset.get('VideoQualitySlider')}" if preset.get("VideoQualityType", 2) == 2
         else f"{preset.get('VideoAvgBitrate')} kbit/s")
    audio = (preset.get("AudioList") or [{}])[0]
    parts = [preset.get("VideoEncoder", "?"), preset.get("VideoPreset") or "", q, resolution_of(preset),
             (preset.get("FileFormat") or "").replace("av_", "").upper(),
             f"audio {audio.get('AudioEncoder', '?')}"]
    return " · ".join(x for x in parts if x)
