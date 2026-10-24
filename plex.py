"""
Plex integration: tell Plex about files replaced by "Replace…"
================================================================

Used by encoding.run_replace_jobs() after a finished encode replaced an original.
Needs PLEX_URL (e.g. http://127.0.0.1:32400) and PLEX_TOKEN in config.env; without
them nothing happens. PLEX_URL may list several addresses separated by spaces (e.g. the
Mac's and the one seen from a Docker container); the first that answers is used. The
token is sent as a header, never in a URL or log line.

- Same file name: Plex's scan doesn't notice a replaced file under the same name, so
  the item (movie or episode) is found by its path and analyzed: Plex reads the new
  codec, bitrate etc. and keeps everything else (watch history, date added).
- New name or extension: Plex scans just that folder, which adds the new file.

The file's path must be the same as in the Plex library (true when the worker runs on
the Plex server's machine with the same mounts).
"""

import json
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
                                         headers={"Accept": "application/json", "X-Plex-Token": self.token})
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

    def scan_folder(self, section_key, folder):
        q = urllib.parse.urlencode({"path": str(folder)})
        self.request(f"/library/sections/{section_key}/refresh?{q}")


def notify_replaced(cfg, original, target):
    """Tell Plex that target replaced original (both full paths). Returns a short message
    for the user, or "" if Plex isn't configured. Never raises: the file is already
    replaced, so a Plex problem only ends up in the message."""
    return notify_replaced_many(cfg, [(original, target)])[0]


def notify_replaced_many(cfg, pairs):
    """Like notify_replaced for several (original, target) pairs, with one folder scan per
    folder instead of one per file. Returns one message per pair."""
    if not cfg.get("PLEX_URL") or not cfg.get("PLEX_TOKEN"):
        return [""] * len(pairs)
    plex = Plex(cfg["PLEX_URL"], cfg["PLEX_TOKEN"])
    messages = [""] * len(pairs)
    scans = {}                                  # (section key, folder) -> [pair index]
    for i, (original, target) in enumerate(pairs):
        try:
            found = plex.locate(target)
            if not found:
                messages[i] = " Plex: the folder isn't in a Plex library (or Plex sees it under another path), nothing to refresh."
                continue
            key, title, _ = found
            if Path(original).name == Path(target).name:
                item = plex.find_item(target)
                if item:
                    plex.analyze(item["ratingKey"])
                    messages[i] = f" Plex: analyzing \"{item.get('title')}\" in \"{title}\" (same file name, so the item keeps its history)."
                    continue
                # not in Plex yet: a folder scan adds it
            scans.setdefault((key, title, str(Path(target).parent)), []).append(i)
        except PlexError as e:
            messages[i] = f" Plex: not refreshed ({e})."
    for (key, title, folder), idx in scans.items():
        try:
            plex.scan_folder(key, folder)
            msg = f" Plex: scanning the folder in \"{title}\" for the new file" + \
                (f" (one scan for {len(idx)} files)." if len(idx) > 1 else ".")
        except PlexError as e:
            msg = f" Plex: not refreshed ({e})."
        for i in idx:
            messages[i] = msg
    return messages
