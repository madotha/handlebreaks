"""Fun facts about the library for the Stats page: one is picked at random on every page
view. gather() reads the numbers from the database once (the web UI caches the result until
the data changes), facts() turns them into sentences. A fact whose numbers are missing (e.g.
a library that was never probed) is left out. Values from files go in escaped; titles are
shown as their folder name, never as a path."""
import re
from datetime import datetime, timedelta

from markupsafe import Markup

MAX_DURATION = 6 * 3600         # longer runtimes are probe glitches, left out of the facts
MAX_BITRATE = 150e6             # the same for absurd bitrates
FPS = 24                        # for the frame count ("roughly")
EVENING = 2 * 3600              # one movie night
DOWNLOAD = 100e6                # 100 Mbit/s
DVD, DVD_MM = 4.7e9, 1.2        # bytes and thickness of a DVD in its sleeve-less stack
FLOPPY, FLOPPY_MM = 1_474_560, 3.3
UHD_PIXELS = 3840 * 2160
MOVIE_MIN = 40 * 60             # shorter files that aren't episodes are extras, trailers or samples
OLDEST_MTIME = 631152000        # 1990: older file dates are wrong clocks, not old files
LEGACY = {                      # codec -> name shown
    "mpeg4": "DivX/Xvid", "msmpeg4v1": "early DivX", "msmpeg4v2": "early DivX", "msmpeg4v3": "early DivX",
    "wmv1": "WMV", "wmv2": "WMV", "wmv3": "WMV", "vc1": "VC-1", "mpeg1video": "MPEG-1",
    "mpeg2video": "MPEG-2", "h263": "H.263", "rv10": "RealVideo", "rv20": "RealVideo", "rv30": "RealVideo",
    "rv40": "RealVideo", "theora": "Theora", "flv1": "Flash video", "vp6": "Flash video", "vp6f": "Flash video",
}
LANDMARKS = [(93, "the Statue of Liberty"), (330, "the Eiffel Tower"), (828, "the Burj Khalifa"),
             (8_849, "Mount Everest"), (11_000, "a cruising airliner"), (100_000, "the edge of space"),
             (408_000, "the International Space Station"), (384_400_000, "the Moon")]
EPISODE_RE = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?!\d)")
YEAR_RE = re.compile(r"\((1[89]\d\d|20\d\d)\)")
SEASON_RE = re.compile(r"^(season|staffel|series|specials?)\b", re.I)


def title(key):
    """A readable title for a file: its folder (the show for episodes, plus SxxEyy), without
    {tmdb-…} or [ENG] tags."""
    parts = key.split("/")
    folder = parts[-3] if len(parts) > 3 and SEASON_RE.match(parts[-2]) else parts[-2] if len(parts) > 2 else ""
    name = folder or parts[-1].rsplit(".", 1)[0]
    name = re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", name)).strip() or parts[-1]
    ep = EPISODE_RE.search(parts[-1])
    return f"{name} S{int(ep.group(1)):02d}E{int(ep.group(2)):02d}" if ep and folder else name


def show(key):
    """The show of an episode: the folder above its season folder (or its own folder)."""
    parts = key.split("/")
    folder = parts[-3] if len(parts) > 3 and SEASON_RE.match(parts[-2]) else parts[-2] if len(parts) > 2 else ""
    return re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", folder)).strip()


def gather(con, optimized, share_names, encoded=0, sizes=()):
    """The numbers behind the facts. optimized: codecs that count as optimized; encoded: files
    Handlebreaks encoded so far; sizes: (original, replacement) byte pairs of replaces."""
    good = sorted(optimized)
    marks = ", ".join("?" * len(good))
    s = {"encoded": encoded, "freed": sum(o - n for o, n in sizes if o and n)}
    s["files"], s["size"], s["duration"] = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(size), 0), COALESCE(SUM(CASE WHEN duration > 0 AND duration <= ? "
        "THEN duration END), 0) FROM files", (MAX_DURATION,)).fetchone()
    if not s["files"]:
        return s
    for cls, where in (("good", f"video_codec IN ({marks})"), ("other", f"video_codec IS NOT NULL AND video_codec NOT IN ({marks})")):
        s[cls] = con.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0), AVG(CASE WHEN bitrate > 0 AND bitrate <= ? "
                             f"THEN bitrate END) FROM files WHERE {where}", [MAX_BITRATE] + good).fetchone()
    s["legacy"] = con.execute(f"SELECT video_codec, COUNT(*) FROM files WHERE video_codec IN ({', '.join('?' * len(LEGACY))}) "
                              "GROUP BY 1", list(LEGACY)).fetchall()
    s["biggest"] = con.execute("SELECT key, size FROM files WHERE size > 0 ORDER BY size DESC LIMIT 1").fetchone()
    s["frugal"] = con.execute("SELECT key, size, duration FROM files WHERE size > 0 AND duration >= 600 AND duration <= ? "
                              "ORDER BY size * 1.0 / duration LIMIT 1", (MAX_DURATION,)).fetchone()
    s["fastest"] = con.execute("SELECT key, bitrate FROM files WHERE bitrate > 0 AND bitrate <= ? "
                               "ORDER BY bitrate DESC LIMIT 1", (MAX_BITRATE,)).fetchone()
    s["shares"] = [(share_names.get(p, p), n) for p, n in con.execute(
        "SELECT substr(key, 1, instr(key, '/') - 1), COUNT(*) FROM files GROUP BY 1 ORDER BY 2 DESC")]
    now = datetime.now().timestamp()
    s["recent"] = con.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN duration > 0 AND duration <= ? THEN duration END), 0), "
                              "COALESCE(SUM(size), 0) FROM files WHERE added >= ? AND added <= ?",
                              (MAX_DURATION, now - 30 * 86400, now + 86400)).fetchone()
    s["longest"] = con.execute("SELECT key, duration FROM files WHERE duration > 0 AND duration <= ? "
                               "ORDER BY duration DESC LIMIT 1", (MAX_DURATION,)).fetchone()
    s["uhd"] = con.execute("SELECT COUNT(*) FROM files WHERE width >= 3200 OR height >= 2000").fetchone()[0]
    s["probed"] = con.execute("SELECT COUNT(*) FROM files WHERE width > 0").fetchone()[0]
    s["old_file"] = con.execute("SELECT key, mtime FROM files WHERE mtime >= ? ORDER BY mtime LIMIT 1",
                                (OLDEST_MTIME,)).fetchone()
    s["busiest"] = con.execute("SELECT substr(at, 1, 10), COUNT(DISTINCT key) FROM results WHERE status = 'OK' "
                               "GROUP BY 1 ORDER BY 2 DESC, 1 DESC LIMIT 1").fetchone()
    s["week"] = con.execute("SELECT COUNT(DISTINCT key) FROM results WHERE status = 'OK' AND at >= ?",
                            ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M"),)).fetchone()[0]
    s["sd"] = con.execute("SELECT COUNT(*), AVG(width * height) FROM files WHERE width > 0 AND height > 0 "
                          "AND width < 1200 AND height < 700").fetchone()
    episodes, years, shows, ep_time, movie_time = 0, {}, {}, [], []
    max_year = datetime.now().year + 1
    for key, d in con.execute("SELECT key, duration FROM files"):
        plausible = d and 0 < d <= MAX_DURATION
        if EPISODE_RE.search(key.rsplit("/", 1)[-1]):
            episodes += 1
            name = show(key)
            if name:
                shows[name] = shows.get(name, 0) + 1
            if plausible:
                ep_time.append(d)
        elif plausible and d >= MOVIE_MIN:
            movie_time.append(d)
        m = YEAR_RE.search(key)
        if m and int(m.group(1)) <= max_year:
            years[key[:m.end()]] = int(m.group(1))     # one year per title (its folder), not per file
    s["episodes"] = episodes
    s["top_show"] = max(shows.items(), key=lambda kv: (kv[1], kv[0])) if shows else None
    s["shows"] = len(shows)
    s["ep_avg"] = sum(ep_time) / len(ep_time) if len(ep_time) >= 20 else None
    s["movie_avg"] = sum(movie_time) / len(movie_time) if len(movie_time) >= 10 else None
    s["movies"] = len(movie_time)
    s["years"] = sorted(years.values())
    s["oldest"] = min(years, key=years.get) if years else None
    return s


def _b(value):
    return Markup("<strong>{}</strong>").format(value)


def _t(value):
    return Markup("<em>{}</em>").format(value)


def _n(n):
    return f"{n:,.0f}"


def _span(seconds):
    days = seconds / 86400
    if days >= 365.25:
        years = days / 365.25
        return f"{years:,.0f} years" if years >= 20 else f"{years:.1f} years"
    if days >= 2:
        return f"{days:.0f} days"
    return f"{seconds / 3600:.0f} hours"


def _height(m):
    """A stack height with something to compare it with."""
    shown = f"{m / 1000:,.0f} km" if m >= 10_000 else f"{m:,.0f} m" if m >= 1 else f"{m * 100:.0f} cm"
    if m < 3:
        return Markup("{} high").format(_b(shown))
    if m < LANDMARKS[0][0]:
        return Markup("{} high, as tall as a {}-storey building").format(_b(shown), round(m / 3))
    name = [n for h, n in LANDMARKS if h <= m][-1]
    return Markup("{} high, reaching past {}").format(_b(shown), name)


def _minutes(seconds):
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} minutes"


def facts(s, size, disk=None):
    """All facts the numbers allow, as safe HTML. size formats bytes; disk is the web UI's free
    space ({"free", "total"}, see webui.disk_view()) or None."""
    out = []
    if not s.get("files"):
        return out
    now = datetime.now()
    dur, total = s["duration"], s["size"]
    if dur > 86400:
        out.append(Markup("Your library runs for {} without a break. Start today and the credits roll in {}.").format(
            _b(_span(dur)), _b((now + timedelta(seconds=dur)).strftime("%B %Y"))))
        evenings = dur / EVENING
        out.append(Markup("At two hours per evening, you'd need {} to watch everything, about {} of movie nights.").format(
            _b(f"{_n(evenings)} evenings"), _b(f"{evenings / 365.25:,.0f} years" if evenings >= 730 else f"{evenings:,.0f} nights")))
        frames = dur * FPS
        out.append(Markup("That's roughly {} frames. Looking at one per second would take {}.").format(
            _b(f"{frames / 1e9:,.1f} billion" if frames >= 1e9 else f"{frames / 1e6:,.0f} million"), _b(_span(frames))))
    if s["episodes"] >= 100:
        out.append(Markup("{} in total. At one a day, you'd finish in the year {}.").format(
            _b(f"{_n(s['episodes'])} episodes"), _b(now.year + int(s["episodes"] / 365.25))))
    if total >= DVD * 10:
        out.append(Markup("{} would fill {}, a stack {}.").format(
            _b(size(total)), _b(f"{_n(total / DVD)} DVDs"), _height(total / DVD * DVD_MM / 1000)))
        out.append(Markup("On floppy disks, that's {}: a stack {}.").format(
            _b(f"{total / FLOPPY / 1e6:,.1f} million disks" if total / FLOPPY >= 1e6 else f"{_n(total / FLOPPY)} disks"),
            _height(total / FLOPPY * FLOPPY_MM / 1000)))
        out.append(Markup("Downloading all of it over 100 Mbit/s internet would take {}.").format(
            _b(_span(total * 8 / DOWNLOAD))))
    good_n, good_size, good_rate = s["good"]
    other_n, other_size, other_rate = s["other"]
    if good_n and other_n and total:
        out.append(Markup("{} of your files are already H.265 or AV1. The other {} take up {} of the space.").format(
            _b(f"{good_n / s['files'] * 100:.0f} %"), _n(other_n), _b(f"{other_size / total * 100:.0f} %")))
    elif good_n and not other_n:
        out.append(Markup("Every single one of your {} probed files is already H.265 or AV1. Nothing left to squeeze.").format(_b(_n(good_n))))
    if good_rate and other_rate and other_rate > good_rate * 1.2 and other_size:
        out.append(Markup("Your optimized files average {}, the others {}. Encoding the rest could free roughly {}.").format(
            _b(f"{good_rate / 1e6:.1f} Mbit/s"), _b(f"{other_rate / 1e6:.1f} Mbit/s"),
            _b(size(other_size * (1 - good_rate / other_rate)))))
    if s["encoded"]:
        avg = total / s["files"]
        if s["freed"] >= 2 * avg:
            out.append(Markup("Handlebreaks has encoded {} so far. Its replaces freed {}, room for {} more files of your average size.").format(
                _b(f"{_n(s['encoded'])} files"), _b(size(s["freed"])), _b(_n(s["freed"] / avg))))
        elif s["freed"] > 0:
            out.append(Markup("Handlebreaks has encoded {} so far, and its replaces freed {}.").format(
                _b(f"{_n(s['encoded'])} files"), _b(size(s["freed"]))))
        else:
            out.append(Markup("Handlebreaks has encoded {} for you so far.").format(_b(f"{_n(s['encoded'])} files")))
    if s["legacy"]:
        groups = {}
        for codec, n in s["legacy"]:
            groups[LEGACY[codec]] = groups.get(LEGACY[codec], 0) + n
        listed = sorted(groups.items(), key=lambda g: -g[1])[:3]
        names = [Markup("{} {}").format(_b(_n(n)), name) for name, n in listed]
        joined = names[0] if len(names) == 1 else Markup(", ").join(names[:-1]) + Markup(" and ") + names[-1]
        out.append(Markup("Still in the collection: {} file{}, from the days when MSN Messenger was cool.").format(
            joined, "" if sum(n for _, n in listed) == 1 else "s"))
    if s["biggest"] and s["files"] > 10:
        key, big = s["biggest"]
        ratio = big / (total / s["files"])
        out.append(Markup("Your biggest file is {} with {}, as big as {} average files of your library.").format(
            _t(title(key)), _b(size(big)), _b(f"{ratio:,.0f}")))
    if s["frugal"]:
        key, small, d = s["frugal"]
        out.append(Markup("Most frugal: {} fits {} into {}.").format(_t(title(key)), _b(f"{d / 60:.0f} minutes"), _b(size(small))))
    if s["fastest"]:
        key, rate = s["fastest"]
        out.append(Markup("Highest bitrate: {} at {}.").format(_t(title(key)), _b(f"{rate / 1e6:.1f} Mbit/s")))
    if len(s["years"]) >= 10:
        years = s["years"]
        out.append(Markup("Your oldest title is from {}. Half of your titles are newer than {}.").format(
            _b(years[0]), _b(years[len(years) // 2])))
    if len(s["shares"]) > 1:
        name, n = s["shares"][0]
        others = len(s["shares"]) - 1
        out.append(Markup("{} holds {} of all files. The other {} share{} split the rest.").format(
            _t(name), _b(f"{n / s['files'] * 100:.0f} %"), others, "" if others == 1 else "s"))
    n, d, added = s["recent"]
    if n:
        per_day = d / 30 / 3600
        out.append(Markup("In the last 30 days you added {} ({}), about {} of new video per day. Good luck keeping up.").format(
            _b(f"{_n(n)} file{'' if n == 1 else 's'}"), f"{d / 3600:,.0f} h",
            _b(f"{per_day:.1f} hours" if per_day >= 1 else f"{per_day * 60:.0f} minutes")))
    n, pixels = s["sd"]
    if n and pixels:
        out.append(Markup("{} are still in SD. A single 4K frame has as many pixels as {} of theirs.").format(
            _b(f"{_n(n)} file{'' if n == 1 else 's'}"), _b(f"{UHD_PIXELS / pixels:.0f}")))
    out += more_facts(s, size, disk, now)
    return out


def more_facts(s, size, disk, now):
    """Facts about free space, runtimes, shows, decades, resolutions and the encoding history."""
    out = []
    total, files = s["size"], s["files"]
    avg = total / files if files else 0
    if disk and disk.get("free") and avg:
        free = disk["free"]
        out.append(Markup("The free space on your disks would hold {} more files of your average size ({}).").format(
            _b(_n(free / avg)), size(avg)))
        added = s.get("recent", (0, 0, 0))[2]
        if added > 0:
            days = free / (added / 30)
            if days > 3650:
                when = Markup("more than {}").format(_b("ten years"))
            else:
                when = Markup("{}, around {}").format(_b(_span(days * 86400)), _b((now + timedelta(days=days)).strftime("%B %Y")))
            out.append(Markup("At the pace of the last 30 days ({} added), your free space lasts {}.").format(
                size(added), when))
    if s.get("longest") and files > 10:
        key, d = s["longest"]
        if d >= 2 * 3600:
            out.append(Markup("Your longest video is {}: {} in one go. Bring snacks.").format(_t(title(key)), _b(_minutes(d))))
    if s.get("ep_avg") and s.get("movie_avg"):
        ratio = s["movie_avg"] / s["ep_avg"]
        out.append(Markup("Your average episode runs {}, your average movie {}. One movie is worth about {} episodes.").format(
            _b(_minutes(s["ep_avg"])), _b(_minutes(s["movie_avg"])), _b(f"{ratio:.1f}")))
    if s.get("top_show") and s["top_show"][1] >= 20:
        name, n = s["top_show"]
        out.append(Markup("{} is your biggest show with {}. Binged non-stop, that's a commitment.").format(
            _t(name), _b(f"{_n(n)} episodes")))
    if s.get("shows", 0) >= 5:
        out.append(Markup("You collect {} with {} between them, about {} per show.").format(
            _b(f"{_n(s['shows'])} shows"), _b(f"{_n(s['episodes'])} episodes"), f"{s['episodes'] / s['shows']:.0f}"))
    years = s.get("years") or []
    if len(years) >= 20:
        decades = {}
        for y in years:
            decades[y // 10 * 10] = decades.get(y // 10 * 10, 0) + 1
        dec, n = max(decades.items(), key=lambda kv: (kv[1], kv[0]))
        out.append(Markup("Your favourite decade is the {}: {} of your titles come from it.").format(
            _b(f"{dec}s"), _b(f"{n / len(years) * 100:.0f} %")))
        favourite = max(set(years), key=lambda y: (years.count(y), y))
        out.append(Markup("{} is the year with the most titles in your library: {}.").format(
            _b(favourite), _b(_n(years.count(favourite)))))
    if s.get("uhd") and s.get("probed"):
        share = s["uhd"] / s["probed"] * 100
        out.append(Markup("{} of your files are in 4K. That's {} of the probed ones.").format(
            _b(_n(s["uhd"])), _b(f"{share:.1f} %" if share < 10 else f"{share:.0f} %")))
    if s.get("old_file") and files > 10:
        key, mtime = s["old_file"]
        when = datetime.fromtimestamp(mtime)
        if (now - when).days >= 3 * 365:
            out.append(Markup("The oldest file on your disks is {}, unchanged since {}.").format(
                _t(title(key)), _b(when.strftime("%B %Y"))))
    if s.get("busiest") and s["busiest"][1] >= 5:
        day, n = s["busiest"]
        try:
            shown = datetime.strptime(day, "%Y-%m-%d").strftime("%B %d, %Y").replace(" 0", " ")
        except ValueError:
            shown = day
        out.append(Markup("Busiest day of your encoder so far: {}, with {} finished.").format(_b(shown), _b(f"{_n(n)} encodes")))
    if s.get("week", 0) >= 3:
        out.append(Markup("In the last 7 days, Handlebreaks finished {}. That's about {} per day.").format(
            _b(f"{_n(s['week'])} encodes"), _b(f"{s['week'] / 7:.1f}")))
    return out
