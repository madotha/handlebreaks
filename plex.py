"""
Plex integration: make Plex pick up files replaced by "Replace…"
=================================================================

Needs PLEX_URL (e.g. http://127.0.0.1:32400) and PLEX_TOKEN in config.env; without
them nothing happens. PLEX_URL may list several addresses separated by spaces (e.g. the
Mac's and the one seen from a Docker container); the first that answers is used. The
token is sent as a header, never in a URL or log line.

1. The worker (encoding.run_replace_jobs) moves the files and stores job_info() in the
   job's result: the new and old file as Plex sees them (PLEX_PATH_MAP translates the
   worker's paths, like Sonarr's "map paths") and the new file's size. It doesn't call Plex.
2. The web UI calls advance() every few seconds for those jobs: once a folder has had no
   new replace for SETTLE seconds (at most MAX_WAIT), it scans that folder once for all of
   them (a partial scan, like Sonarr/Radarr). A file replaced under the same name is also
   analyzed, because a scan may not notice it.
3. It then checks that Plex lists the new file (same path and size) and no longer the
   old one, scans once more if not, and reports ok or failed.

Plex keeps the item's watch history and "added" date either way.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class PlexError(Exception):
    pass


class Plex:
    def __init__(self, urls, token, timeout=20):
        self.urls = [u.rstrip("/") for u in urls.split()]
        self.url = self.urls[0]
        self.token = token
        self.timeout = timeout
        self._sections = None

    def request(self, path, method="GET"):
        errors = []
        for url in [self.url] + [u for u in self.urls if u != self.url]:
            req = urllib.request.Request(url + path, method=method,
                                         headers={"Accept": "application/json", "X-Plex-Product": "Handlebreaks",
                                                  "X-Plex-Token": self.token})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
            except urllib.error.HTTPError as e:
                raise PlexError("Plex refused the request (token wrong?)" if e.code == 401 else f"Plex answered HTTP {e.code}")
            except (urllib.error.URLError, OSError) as e:
                errors.append(f"{url}: {getattr(e, 'reason', e)}")
                continue
            self.url = url                      # remember the address that works
            return json.loads(body)["MediaContainer"] if body.strip() else {}
        raise PlexError("Plex isn't reachable (" + "; ".join(errors) + ")")

    def sections(self):
        """[(section key, title, [library folder paths])]"""
        if self._sections is None:
            mc = self.request("/library/sections")
            self._sections = [(d["key"], d["title"], [loc["path"].rstrip("/") for loc in d.get("Location", [])])
                              for d in mc.get("Directory", [])]
        return self._sections

    def locate(self, path):
        """(section key, section title, library folder) that contains path, or None."""
        path = str(path)
        for key, title, locations in self.sections():
            for loc in locations:
                if path.startswith(loc + "/"):
                    return key, title, loc
        return None

    def find_item(self, path):
        """The movie or episode whose file is path, by walking Plex's folder view, or None."""
        found = self.locate(path)
        if not found:
            return None
        key, _, loc = found
        parts = str(path)[len(loc) + 1:].split("/")
        folder = f"/library/sections/{key}/folder"
        for part in parts[:-1]:             # the folder view merges all folders of the library
            node = next((m for m in self.request(folder).get("Metadata", []) if m.get("title") == part), None)
            if node is None:
                return None
            folder = node["key"]
        for item in self.request(folder).get("Metadata", []):
            files = [p.get("file", "") for media in item.get("Media", []) for p in media.get("Part", [])]
            if any(f == str(path) or f.rsplit("/", 1)[-1] == parts[-1] for f in files):
                return item
        return None

    def analyze(self, rating_key):
        self.request(f"/library/metadata/{rating_key}/analyze", method="PUT")

    def item_files(self, path):
        """{file: size} of the movie or episode whose file is path, or None if Plex doesn't list it."""
        item = self.find_item(path)
        if item is None:
            return None
        return {p.get("file", ""): p.get("size") for media in item.get("Media", []) for p in media.get("Part", [])}

    def scan_folder(self, section_key, folder):
        q = urllib.parse.urlencode({"path": str(folder)})
        self.request(f"/library/sections/{section_key}/refresh?{q}")


def path_map(cfg):
    """[(local prefix, Plex prefix)] from PLEX_PATH_MAP ("local=plex", several separated by
    ";"), longest local prefix first."""
    pairs = []
    for item in (cfg.get("PLEX_PATH_MAP") or "").split(";"):
        local, sep, remote = item.partition("=")
        if sep and local.strip() and remote.strip():
            pairs.append((local.strip().rstrip("/"), remote.strip().rstrip("/")))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def to_plex(path, mapping):
    """The path as Plex sees it."""
    path = str(path)
    for local, remote in mapping:
        if path == local or path.startswith(local + "/"):
            return remote + path[len(local):]
    return path


def configured(cfg):
    return bool(cfg.get("PLEX_URL") and cfg.get("PLEX_TOKEN"))


def job_info(cfg, original, target, size):
    """What the web UI needs to bring a replace into Plex (stored in the job's result), or
    None if Plex isn't configured. Only path strings: no request to Plex."""
    if not configured(cfg):
        return None
    mapping = path_map(cfg)
    return {"state": "pending", "path": to_plex(target, mapping), "old_path": to_plex(original, mapping),
            "size": size, "same_name": Path(original).name == Path(target).name, "queued": time.time()}


SETTLE = 15             # s without a new replace in a folder before it's scanned
MAX_WAIT = 60           # s a replace waits at most for its folder to settle
CHECK_AFTER = 10        # s after a scan before checking what Plex lists
RETRY_AFTER = 90        # s after a scan without the new file: scan once more, then give up
UNREACHABLE = 3600      # s Plex may be unreachable before a replace counts as failed
TRIES = 2


def note(info):
    """One sentence about a job's Plex state, for messages."""
    if not info:
        return ""
    state = info.get("state")
    if state == "pending":
        return "Plex: the folder is scanned in a moment."
    if state == "scanning":
        return "Plex: scanning the folder …"
    if state == "ok":
        return "Plex: updated, it lists the new file" + (" (analyzed)." if info.get("same_name") else ".")
    return f"Plex not updated: {info.get('reason') or 'unknown reason'}."


def _scan(plex, jobs, now):
    """Scan the folder of jobs (all in one folder) once; analyze same-name files. Returns
    {job index: new info}."""
    out = {}
    first = jobs[0][1]
    found = plex.locate(first["path"])
    if not found:
        for i, info in jobs:
            out[i] = dict(info, state="skipped", reason="the folder isn't in a Plex library "
                          "(or Plex sees it under another path: see PLEX_PATH_MAP)")
        return out
    key = found[0]
    plex.scan_folder(key, str(Path(first["path"]).parent))
    for i, info in jobs:
        if info.get("same_name"):
            item = plex.find_item(info["path"])
            if item:
                plex.analyze(item["ratingKey"])
        out[i] = dict(info, state="scanning", section=key, scanned=now, tries=info.get("tries", 0) + 1)
    return out


def _check(plex, info):
    """None if Plex lists the new file and not the old one, else why not."""
    files = plex.item_files(info["path"])
    if files is None:
        return "it doesn't list the new file"
    if info["path"] not in files:
        return "it doesn't list the new file"
    if info.get("size") is not None and files[info["path"]] != info["size"]:
        return "it still has the old file's data"
    if not info.get("same_name") and info["old_path"] in files:
        return "it still lists the old file too"
    return None


def advance(cfg, infos, now=None):
    """Move the Plex state of finished replaces on: scan settled folders, check scanned
    files, retry once, give up. infos: [plex info of a job]. Returns {index: new info} for
    the ones that changed. Never raises: an unreachable Plex is tried again next time."""
    now = now or time.time()
    out = {}
    if not configured(cfg) or not infos:
        return out
    plex = Plex(cfg["PLEX_URL"], cfg["PLEX_TOKEN"])
    try:
        folders = {}
        for i, info in enumerate(infos):
            if info.get("state") == "pending":
                folders.setdefault(str(Path(info["path"]).parent), []).append((i, info))
        for jobs in folders.values():
            times = [info.get("queued", 0) for _, info in jobs]
            if now - max(times) >= SETTLE or now - min(times) >= MAX_WAIT:
                out.update(_scan(plex, jobs, now))
        retry = {}
        for i, info in enumerate(infos):
            if info.get("state") != "scanning" or now - info.get("scanned", 0) < CHECK_AFTER:
                continue
            problem = _check(plex, info)
            if problem is None:
                out[i] = dict(info, state="ok", checked=now)
            elif now - info["scanned"] >= RETRY_AFTER:
                if info.get("tries", 1) < TRIES:
                    retry.setdefault(str(Path(info["path"]).parent), []).append((i, info))
                else:
                    out[i] = dict(info, state="failed", reason=problem, checked=now)
        for jobs in retry.values():             # one more scan per folder
            out.update(_scan(plex, jobs, now))
    except PlexError as e:
        for i, info in enumerate(infos):
            if i not in out and info.get("state") in ("pending", "scanning") and now - info.get("queued", now) >= UNREACHABLE:
                out[i] = dict(info, state="failed", reason=str(e))
    return out
