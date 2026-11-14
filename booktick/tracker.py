"""Seat trackers: how a cinema blocks and releases seats through a day.

Some cinemas hold seats back and release them shortly before the show. District's seat data
only says whether a seat is free or taken, so a tracker works it out from how seats change:
a sold seat never comes back, so seats that turn free again were released by the cinema, or
held by someone who didn't finish paying (those come back within minutes, a few at a time).

A tracker rides along with the watchers. Every check of its cinema's page records each
matching show's seats-left count when it changes. When a count moves, the seat-map browser
reads that show's seats (spaced out, after any alert snapshots), so the report can say which
seats changed. After the last tracked show starts, the report goes to the tracker's alert
channels, and the full report stays at /report/<token>.
"""
import copy
import html
import secrets
import threading
import time
from collections import Counter
from datetime import datetime

from . import seatmap
from .alerts import describe_results, send_report
from .config import (APP_NAME, BLOCK_MIN, HOLD_WINDOW, MAX_TRACKED_SHOWS, PUBLIC_URL, RELEASE_MIN, REPORT_AFTER,
                     TRACK_MAP_GAP, TRACK_MAP_GAP_FAR, TRACK_MAP_SPACING, TRACK_SAVE_EVERY)
from .core import (IST, add_log, date_label, fmt_clock, now_ist, plural, shorten, state, tasks_lock, today_ist)
from .storage import save_tasks

esc = html.escape


def is_tracker(task):
    return bool(task) and task.get("kind") == "tracker"


def new_token():
    return secrets.token_urlsafe(18)


def tracker_title(task):
    """'IMAX shows', 'Ramayana · IMAX', 'All shows'."""
    fmt = str(task.get("format_filter") or "ANY").upper()
    movie = (task.get("movie_filter") or "").strip()
    if movie:
        return movie if fmt == "ANY" else f"{movie} · {fmt}"
    return "All shows" if fmt == "ANY" else f"{fmt} shows"


def place_of(task):
    return task.get("cinema_name") or "the cinema"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def ts_of(iso):
    return datetime.fromisoformat(iso).timestamp()


def clock_at(ts):
    return fmt_clock(datetime.fromtimestamp(ts, IST))


def duration(seconds):
    """'44 min', '2 h 10 min', '1 day 3 h'."""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h" + (f" {minutes} min" if minutes else "")
    days, hours = divmod(hours, 24)
    return plural(days, "day") + (f" {hours} h" if hours else "")


# How long before showtime seats came back: (at least this many seconds before, phrase).
TIMING = [(86400, "more than a day before"), (21600, "6–24 hours before"), (7200, "2–6 hours before"),
          (3600, "1–2 hours before"), (1800, "30–60 min before"), (0, "in the last 30 min before")]


def timing_phrase(before):
    return next(phrase for floor, phrase in TIMING if before >= floor)


# ---------------------------------------------------------------------------
# The live history. task["track"] lives in memory; a copy goes to storage every
# TRACK_SAVE_EVERY seconds as task["track_saved"], so a restart loses only minutes.
# ---------------------------------------------------------------------------
def new_track():
    return {"shows": {}, "layouts": {}, "skipped": [], "maps": 0, "releases": 0, "released": 0, "last_event": None}


def track_of(task):
    """The tracker's history, restored from its last save after a restart. Call with tasks_lock."""
    track = task.get("track")
    if track is None:
        track = copy.deepcopy(task.get("track_saved")) or new_track()
        task["track"] = track
    return track


def show_key(show):
    """A show's seat link is unique to it; without one, its movie, time and format."""
    url = show.get("url") or ""
    if "/seat-layout/" in url:
        return url
    start = show["start"].isoformat() if show.get("start") else ""
    return f"{show.get('title')}|{start}|{show.get('format')}"


def summary_text(track):
    shows = len(track["shows"])
    if not shows:
        return "Waiting for matching shows to be listed"
    released = (f"{plural(track['releases'], 'release')} ({plural(track['released'], 'seat')})"
                if track["releases"] else "no releases yet")
    return f"Tracking {plural(shows, 'show')} · {released}"


def observe(task, place, shows):
    """One check of the tracker's cinema page. `shows` are the matching shows on its day."""
    now = time.time()
    lines = []
    with tasks_lock:
        if state["tasks"].get(task.get("id")) is not task or task.get("status") != "Active":
            return
        if place:
            task["cinema_name"] = place
        track = track_of(task)
        listed = set()
        for s in shows:
            if not s.get("start"):
                continue
            key = show_key(s)
            listed.add(key)
            start = s["start"].timestamp()
            rec = track["shows"].get(key)
            if rec is None:
                if start <= now or key in track["skipped"]:
                    continue
                if len(track["shows"]) >= MAX_TRACKED_SHOWS:
                    track["skipped"].append(key)
                    continue
                rec = track["shows"][key] = {
                    "title": s.get("title") or "Movie", "format": str(s.get("format") or ""),
                    "audi": str(s.get("audi") or ""), "start": s["start"].isoformat(), "total": s.get("total"),
                    "url": s.get("url"), "counts": [], "dirty": True, "map_ts": 0, "seats": None, "diffs": [],
                    "gone": False,
                }
            rec["gone"] = False
            if now > start + 120 or s.get("avail") is None:
                continue  # a show stops being tracked once it starts
            if s.get("total"):
                rec["total"] = s["total"]
            counts = rec["counts"]
            if counts and counts[-1][1] == s["avail"]:
                continue
            delta = s["avail"] - counts[-1][1] if counts else 0
            counts.append([int(now), s["avail"]])
            if len(counts) > 1:
                rec["dirty"] = True  # read its seats soon, to see which ones changed
            if delta >= RELEASE_MIN:
                track["releases"] += 1
                track["released"] += delta
                track["last_event"] = [int(now), delta, key]
                lines.append(f"🎟️ [{task.get('user_label', 'Tracker')}] {delta} seats came back in the "
                             f"{fmt_clock(s['start'])} {rec['format']} show at {shorten(place_of(task), 50)}, "
                             f"{duration(start - now)} before it starts.")
        for key, rec in track["shows"].items():
            if key not in listed and ts_of(rec["start"]) > now:
                rec["gone"] = True  # taken off sale before it started
        task.update(last_checked=now_ist().strftime("%H:%M:%S"), last_checked_ts=now, last_result=summary_text(track))
        if now - task.get("track_saved_ts", 0) >= TRACK_SAVE_EVERY:
            task["track_saved"] = copy.deepcopy(track)
            task["track_saved_ts"] = now
            save_tasks()
        wanted = next_map(task, track, now)
    for line in lines:
        add_log(line, (task.get("owner"),))
    if wanted:
        ask_for_map(task, *wanted)


# ---------------------------------------------------------------------------
# Seat snapshots: which seats changed
# ---------------------------------------------------------------------------
_gate = {"next": 0.0}
_gate_lock = threading.Lock()
_pending = set()  # (tracker id, show key) being read right now


def map_gap(start, now):
    return TRACK_MAP_GAP if start - now <= 3 * 3600 else TRACK_MAP_GAP_FAR


def next_map(task, track, now):
    """(show key, seat link) to read next, if one is due and the browser is free. Call with tasks_lock.

    Each show gets a first read, then another whenever its count has moved, no more often
    than map_gap. Across every tracker, reads are TRACK_MAP_SPACING apart, so trackers never
    hold up the seat maps that follow alerts.
    """
    if not seatmap.enabled():
        return None
    due = []
    for key, rec in track["shows"].items():
        start = ts_of(rec["start"])
        if (now >= start or rec["gone"] or "/seat-layout/" not in (rec.get("url") or "")
                or (task["id"], key) in _pending or now - rec["map_ts"] < map_gap(start, now)
                or (rec["seats"] is not None and not rec["dirty"])):
            continue
        due.append((start, key, rec["url"]))
    if not due:
        return None
    with _gate_lock:
        if now < _gate["next"] or seatmap.waiting() > 10:
            return None
        _gate["next"] = now + TRACK_MAP_SPACING
    _, key, url = min(due)  # the show starting soonest first
    _pending.add((task["id"], key))
    return key, url


def ask_for_map(task, key, url):
    tid = task["id"]

    def done(layout, error):
        try:
            took_map(tid, key, layout, error)
        finally:
            _pending.discard((tid, key))

    if not seatmap.request_layout(url, done):
        _pending.discard((tid, key))


def seat_list(sections):
    """[(row index, column, seat id, is free)] in drawing order. Ids are row + seat number ('H12')."""
    out, seen, index = [], set(), 0
    for _, rows in sections:
        for row, seats in rows:
            for col, free, number in seats:
                sid = f"{row}{number or col}"
                while sid in seen:  # two sections reusing a row letter
                    sid += "'"
                seen.add(sid)
                out.append((index, col, sid, free))
            index += 1
        index += 1  # a gap row between sections
    return out


def geometry(sections, front_first):
    """What the report needs to draw a show's seats: rows of (column, seat id)."""
    rows, labels = {}, {}
    index = 0
    for _, rs in sections:
        for row, _ in rs:
            labels[index] = str(row)
            index += 1
        index += 1
    for i, col, sid, _ in seat_list(sections):
        rows.setdefault(i, []).append([col, sid])
    height = max(labels, default=-1) + 1
    width = max((col for r in rows.values() for col, _ in r), default=1)
    return {"front_first": bool(front_first), "width": width,
            "rows": [[labels.get(i, ""), rows.get(i, [])] for i in range(height)]}


def took_map(tid, key, layout, error):
    """A read of one show's seats arrived: note which seats changed since the last one."""
    now = int(time.time())
    sections = seatmap.seat_rows(layout) if layout else []
    note = None
    with tasks_lock:
        task = state["tasks"].get(tid)
        if not is_tracker(task):
            return
        track = track_of(task)
        rec = track["shows"].get(key)
        if not rec:
            return
        rec["map_ts"] = now
        if not sections:
            rec["map_fails"] = rec.get("map_fails", 0) + 1
            if rec["map_fails"] == 1:
                note = (f"⚠️ [{task.get('user_label', 'Tracker')}] couldn't read the seats of the "
                        f"{clock_at(ts_of(rec['start']))} show: {shorten(error or 'no seat layout', 100)}. "
                        "The seat counts are still being tracked.")
        else:
            track["maps"] += 1
            rec["dirty"] = False
            layout_key = rec["audi"] or key
            if layout_key not in track["layouts"]:
                track["layouts"][layout_key] = geometry(sections, (layout or {}).get("screenAtTopSeatLayout"))
            rec["layout"] = layout_key
            taken = {sid for _, _, sid, free in seat_list(sections) if not free}
            before = rec["seats"]
            if before is None:
                rec["seats"] = {sid: 0 for sid in taken}  # 0: already taken when tracking began
            else:
                freed = sorted(set(before) - taken)
                added = sorted(taken - set(before))
                if freed or added:
                    held = [sid for sid in freed if before[sid] and now - before[sid] <= HOLD_WINDOW]
                    rec["diffs"].append({"ts": now, "freed": freed, "taken": added, "held": held})
                rec["seats"] = {sid: before.get(sid, now) for sid in taken}
        owner = task.get("owner")
    if note:
        add_log(note, (owner,))


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def zone_of(sids, geo):
    """Where a group of seats sits: ('rows J–L', 'back rows, centre') from the layout."""
    if not geo or not sids:
        return "", ""
    where = {sid: (i, col) for i, (_, seats) in enumerate(geo["rows"]) for col, sid in seats}
    hits = [where[s] for s in sids if s in where]
    if not hits:
        return "", ""
    labels = [geo["rows"][i][0] for i in sorted({i for i, _ in hits})]
    rows = labels[0] if len(labels) == 1 else (f"{labels[0]}–{labels[-1]}" if len(labels) > 3 else ", ".join(labels))
    seated = [i for i, (_, seats) in enumerate(geo["rows"]) if seats]
    depth = Counter()
    side = Counter()
    for i, col in hits:
        pos = seated.index(i) / max(1, len(seated) - 1) if i in seated else 0.5
        if not geo["front_first"]:
            pos = 1 - pos  # the first rows listed are at the back
        depth["front" if pos < 1 / 3 else "middle" if pos < 2 / 3 else "back"] += 1
        third = col / max(1, geo["width"])
        side["centre" if 0.25 <= third <= 0.75 else "sides"] += 1
    zone = f"{depth.most_common(1)[0][0]} rows"
    if side["centre"] >= 0.6 * len(hits):
        zone += ", centre"
    elif side["sides"] >= 0.6 * len(hits):
        zone += ", sides"
    return ("row " if len(labels) == 1 else "rows ") + rows, zone


def show_report(key, rec, layouts):
    """What happened to one show's seats."""
    start = ts_of(rec["start"])
    counts = [c for c in rec["counts"] if c[0] <= start + 120]
    geo = layouts.get(rec.get("layout") or "")
    diffs = rec.get("diffs") or []
    released_ids, blocked_ids, held = [], [], 0
    for diff in diffs:
        held_set = set(diff.get("held") or [])
        held += len(held_set)
        released_ids += [s for s in diff["freed"] if s not in held_set]
        if len(diff["taken"]) >= BLOCK_MIN:
            blocked_ids += diff["taken"]
    releases, drops, small_back = [], [], 0
    for (t0, a), (t1, b) in zip(counts, counts[1:]):
        d = b - a
        if d >= RELEASE_MIN:
            event = {"ts": t1, "seats": d, "before": start - t1, "rows": "", "zone": ""}
            # The first seat read after the release shows which seats came back.
            after = next((x for x in diffs if t1 <= x["ts"] <= t1 + TRACK_MAP_GAP_FAR + 300 and x["freed"]), None)
            if after:
                freed = [s for s in after["freed"] if s not in set(after.get("held") or [])]
                event["rows"], event["zone"] = zone_of(freed, geo)
            releases.append(event)
        elif d > 0:
            small_back += d
        elif -d >= BLOCK_MIN:
            drops.append({"ts": t1, "seats": -d, "before": start - t1})
    return {
        "key": key, "title": rec["title"], "format": rec["format"], "audi": rec["audi"], "start": rec["start"],
        "total": rec.get("total"), "first": counts[0][1] if counts else None, "last": counts[-1][1] if counts else None,
        "series": counts, "releases": releases, "drops": drops, "small_back": small_back, "held": held,
        "gone": rec.get("gone", False), "maps": len(diffs) + (1 if rec.get("seats") is not None else 0),
        "released_ids": sorted(set(released_ids)), "blocked_ids": sorted(set(blocked_ids)), "layout": geo,
    }


def show_name(s):
    return f"{fmt_clock(datetime.fromisoformat(s['start']))} {s['format']}".strip()


def insights(shows, skipped=0):
    """The findings, most useful first, as plain sentences."""
    if not shows:
        return ["No matching shows were listed for this day, so there was nothing to track."]
    lines = []
    releases = [(s, r) for s in shows for r in s["releases"]]
    total = sum(r["seats"] for _, r in releases)
    if releases:
        lines.append(f"{plural(len(releases), 'release')} across {plural(len({s['key'] for s, _ in releases}), 'show')}: "
                     f"{plural(total, 'seat')} came back in one go.")
        buckets = Counter()
        for _, r in releases:
            buckets[timing_phrase(r["before"])] += r["seats"]
        phrase, seats = buckets.most_common(1)[0]
        if len(releases) > 1 and seats < total:
            lines.append(f"Most of them ({seats} of {total} seats) came back {phrase} showtime.")
        else:
            lines.append(f"They came back {phrase} showtime.")
        s, r = max(releases, key=lambda sr: sr[1]["seats"])
        where = f" ({r['rows']}, {r['zone']})" if r["rows"] else ""
        lines.append(f"Biggest: {r['seats']} seats in the {show_name(s)} show at {clock_at(r['ts'])}, "
                     f"{duration(r['before'])} before it started{where}.")
        zones = Counter(r["zone"] for _, r in releases if r["zone"])
        if zones and len(releases) > 1:
            lines.append(f"Released seats were mostly in the {zones.most_common(1)[0][0]}.")
        lines.append(f"Next time, look {phrase} showtime.")
    else:
        lines.append("No seats came back in one go, so this cinema didn't seem to hold seats back for these shows.")
    drops = [(s, d) for s in shows for d in s["drops"]]
    if drops:
        s, d = max(drops, key=lambda sd: sd[1]["seats"])
        lines.append(f"{plural(len(drops), 'big drop')}: up to {d['seats']} seats went at once (the {show_name(s)} show at "
                     f"{clock_at(d['ts'])}), likely held back by the cinema or a large group booking.")
    held = sum(s["held"] for s in shows)
    if held:
        lines.append(f"{plural(held, 'seat')} came back within {HOLD_WINDOW // 60} min of being taken: bookings "
                     "people didn't finish. That's normal.")
    gone = [s for s in shows if s["gone"]]
    if gone:
        lines.append(f"Taken off sale before starting: {', '.join(show_name(s) for s in gone)}.")
    if skipped:
        lines.append(f"{plural(skipped, 'more show')} matched but weren't tracked (a tracker follows up to {MAX_TRACKED_SHOWS}).")
    return lines


def build_report(task, final=False):
    """The report from what's been tracked so far. Call with tasks_lock."""
    track = track_of(task)
    shows = sorted((show_report(k, r, track["layouts"]) for k, r in track["shows"].items()), key=lambda s: s["start"])
    return {"final": final, "at": int(time.time()), "shows": shows, "skipped": len(track["skipped"]),
            "maps": track["maps"], "insights": insights(shows, len(track["skipped"]))}


def report_for(token):
    """(tracker copy, report) for a report link, or (None, None)."""
    token = str(token or "")
    if len(token) < 16:
        return None, None
    with tasks_lock:
        for task in state["tasks"].values():
            own = task.get("report_token")
            if is_tracker(task) and own and secrets.compare_digest(own, token):
                return dict(task), task.get("report") or build_report(task)
    return None, None


def report_url(task, absolute=True):
    token = task.get("report_token")
    if not token:
        return None
    if not absolute:
        return f"/report/{token}"
    return f"{PUBLIC_URL}/report/{token}" if PUBLIC_URL else None


# ---------------------------------------------------------------------------
# Finishing: the end-of-day report
# ---------------------------------------------------------------------------
def due(task, now=None):
    """Is it time for this tracker's report? After its last show starts, or once its day has passed."""
    if not is_tracker(task) or task.get("status") not in ("Active", "Paused"):
        return False
    now = now or time.time()
    starts = [ts_of(r["start"]) for r in track_of(task)["shows"].values()]
    over = bool(starts) and now >= max(starts) + REPORT_AFTER
    if task.get("target_date", "") < today_ist():
        return over or not starts
    return task.get("status") == "Active" and over


def headline(task):
    return f"📊 Seat report: {tracker_title(task)} · {shorten(place_of(task), 40)} · {date_label(task.get('target_date'))}"


def finish(tid):
    """Write the final report, send it, and stop tracking."""
    with tasks_lock:
        task = state["tasks"].get(tid)
        if not due(task):
            return
        report = build_report(task, final=True)
        task.update(report=report, status="Done", last_result="Report sent",
                    report_at=now_ist().strftime("%d %b, %I:%M %p").replace(" 0", " "))
        for key in ("track", "track_saved", "track_saved_ts"):
            task.pop(key, None)
        save_tasks()
        snapshot = dict(task)
    results = send_report(snapshot, headline(snapshot), report["insights"][:5], report_url(snapshot))
    add_log(f"📊 [{snapshot.get('user_label', 'Tracker')}] the seat report for {shorten(place_of(snapshot), 50)} is ready. "
            f"{describe_results(results)}", (snapshot.get("owner"),))


def card_info(task_id):
    """Numbers for a tracker's card, read under the lock."""
    with tasks_lock:
        task = state["tasks"].get(task_id)
        if not is_tracker(task):
            return {}
        if task.get("report"):
            r = task["report"]
            return {"shows": len(r["shows"]), "releases": sum(len(s["releases"]) for s in r["shows"]),
                    "released": sum(x["seats"] for s in r["shows"] for x in s["releases"]), "maps": r.get("maps", 0),
                    "last": None, "next": None}
        track = track_of(task)
        now = time.time()
        upcoming = sorted(ts_of(r["start"]) for r in track["shows"].values() if ts_of(r["start"]) > now)
        last = track.get("last_event")
        last_text = None
        if last and last[2] in track["shows"]:
            rec = track["shows"][last[2]]
            last_text = (f"{last[1]} seats came back in the {clock_at(ts_of(rec['start']))} {rec['format']} show "
                         f"at {clock_at(last[0])}")
        return {"shows": len(track["shows"]), "releases": track["releases"], "released": track["released"],
                "maps": track["maps"], "last": last_text, "next": clock_at(upcoming[0]) if upcoming else None}


# ---------------------------------------------------------------------------
# The report page (/report/<token>)
# ---------------------------------------------------------------------------
def sparkline(s):
    """Seats left over time, with releases (green) and big drops (amber) marked."""
    series = s["series"]
    if len(series) < 2:
        return '<p class="muted">Not enough changes to draw.</p>'
    w, h, pad = 600, 120, 6
    t0, t1 = series[0][0], max(series[-1][0], series[0][0] + 1)
    top = max(max(v for _, v in series), s.get("total") or 0, 1)
    x = lambda t: pad + (t - t0) / (t1 - t0) * (w - 2 * pad)  # noqa: E731
    y = lambda v: h - pad - v / top * (h - 2 * pad)  # noqa: E731
    points, prev = [], None
    for t, v in series:
        if prev is not None:
            points.append(f"{x(t):.1f},{y(prev):.1f}")  # a step: the count holds until it changes
        points.append(f"{x(t):.1f},{y(v):.1f}")
        prev = v
    marks = "".join(f'<circle cx="{x(r["ts"]):.1f}" cy="{y(next(v for t, v in series if t == r["ts"])):.1f}" r="5" class="rel"/>'
                    for r in s["releases"])
    marks += "".join(f'<circle cx="{x(d["ts"]):.1f}" cy="{y(next(v for t, v in series if t == d["ts"])):.1f}" r="5" class="drop"/>'
                     for d in s["drops"])
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" '
            f'aria-label="Seats left over time"><polyline points="{" ".join(points)}"/>{marks}</svg>'
            f'<div class="axis"><span>{clock_at(t0)}</span><span>{clock_at(t1)}</span></div>')


def seat_grid(s):
    """The show's seats: released ones green, held-back ones amber."""
    geo = s.get("layout")
    released, blocked = set(s["released_ids"]), set(s["blocked_ids"])
    if not geo or not (released or blocked):
        return ""
    rows = []
    for label, seats in geo["rows"]:
        cells = ["<i></i>"] * geo["width"]
        for col, sid in seats:
            if 1 <= col <= geo["width"]:
                cls = "rel" if sid in released else "blk" if sid in blocked else "seat"
                cells[col - 1] = f'<i class="{cls}" title="{esc(sid)}"></i>'
        rows.append(f'<div class="srow"><b>{esc(label)}</b>{"".join(cells)}</div>')
    screen = '<div class="screen">SCREEN</div>'
    body = (screen + "".join(rows)) if geo["front_first"] else ("".join(rows) + screen)
    return (f'<div class="grid" style="--cols:{geo["width"]}">{body}</div>'
            '<div class="key"><i class="rel"></i>Came back<i class="blk"></i>Taken in one go<i class="seat"></i>Other seats</div>')


def show_card(s):
    start = datetime.fromisoformat(s["start"])
    meta = " · ".join(x for x in (s["format"], s["audi"] and f"Screen {s['audi']}") if x)
    if s["first"] is not None and s["last"] is not None:
        total = f" of {s['total']}" if s.get("total") else ""
        seats = f"{s['first']} seats left when tracking began → {s['last']}{total} at showtime"
    else:
        seats = "No seat counts recorded"
    events = []
    for r in s["releases"]:
        where = f" · {esc(r['rows'])}, {esc(r['zone'])}" if r["rows"] else ""
        events.append(f'<li class="rel"><b>+{r["seats"]}</b> at {clock_at(r["ts"])}, {duration(r["before"])} before{where}</li>')
    for d in s["drops"]:
        events.append(f'<li class="drop"><b>−{d["seats"]}</b> at {clock_at(d["ts"])}, {duration(d["before"])} before</li>')
    extra = []
    if s["held"]:
        extra.append(f"{plural(s['held'], 'seat')} back from unfinished bookings")
    if s["small_back"]:
        extra.append(f"{plural(s['small_back'], 'seat')} came back a few at a time")
    if s["gone"]:
        extra.append("taken off sale before it started")
    return f"""
<section class="show">
  <h3>{fmt_clock(start)} · {esc(s['title'])}</h3>
  <p class="muted">{esc(meta)}</p>
  <p>{esc(seats)}</p>
  {sparkline(s)}
  {f'<ul class="events">{"".join(events)}</ul>' if events else ''}
  {f'<p class="muted">{esc("; ".join(extra).capitalize())}.</p>' if extra else ''}
  {seat_grid(s)}
</section>"""


def report_page(task, report):
    if not task:
        return PAGE.format(title=f"{APP_NAME} report", body="<h1>Report not found</h1><p>It may have been deleted.</p>")
    status = ("Final report" if report["final"]
              else f"So far · updated {clock_at(report['at'])} · the final report comes after the last show starts")
    insight_items = "".join(f"<li>{esc(line)}</li>" for line in report["insights"])
    cards = "".join(show_card(s) for s in report["shows"])
    body = f"""
<header>
  <p class="eyebrow">{APP_NAME} seat report</p>
  <h1>{esc(tracker_title(task))} · {esc(place_of(task))}</h1>
  <p class="muted">{esc(date_label(task.get('target_date')))} · {esc(status)}</p>
</header>
<section class="insights"><h2>What happened</h2><ul>{insight_items}</ul></section>
<p class="note">District only shows whether a seat is free or taken. Seats that come back were released by the cinema,
or held by someone who didn't finish paying (those come back within minutes, a few at a time). Seats that go many at
once were held back by the cinema, or booked by a big group.</p>
{cards}"""
    return PAGE.format(title=esc(f"Seat report · {place_of(task)}"), body=body)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg: #f4f4f9; --card: #fff; --soft: #f6f6fa; --text: #161729; --muted: #686a82; --line: #e8e8f0;
          --ok: #15803d; --ok-bg: #e5f7ec; --warn: #b45309; --warn-bg: #fff3d9; --seat: #d5d6e0; --brand: #f9573a; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0b0c16; --card: #151624; --soft: #1c1d2e; --text: #ececf4; --muted: #9a9cb5; --line: #2a2b40;
            --ok: #4ade80; --ok-bg: rgba(74, 222, 128, .14); --warn: #fbbf24; --warn-bg: rgba(251, 191, 36, .14);
            --seat: #3a3b55; --brand: #ff8a6e; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 48px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.35rem; letter-spacing: -.01em; }}
  h2 {{ margin: 0 0 8px; font-size: 1.05rem; }} h3 {{ margin: 0; font-size: 1rem; }}
  p {{ margin: 0 0 8px; }} .muted {{ color: var(--muted); }}
  .eyebrow {{ margin: 0 0 4px; color: var(--brand); font-weight: 700; font-size: .8rem; letter-spacing: .06em; text-transform: uppercase; }}
  header {{ margin-bottom: 16px; }}
  section {{ margin: 0 0 12px; padding: 16px 18px; border-radius: 16px; background: var(--card); border: 1px solid var(--line); }}
  .insights ul {{ margin: 0; padding-left: 1.2em; }} .insights li {{ margin: 0 0 6px; }}
  .note {{ margin: 0 0 12px; font-size: .85rem; color: var(--muted); }}
  .spark {{ display: block; width: 100%; height: 110px; margin: 6px 0 2px; }}
  .spark polyline {{ fill: none; stroke: var(--brand); stroke-width: 2; vector-effect: non-scaling-stroke; }}
  .spark .rel {{ fill: var(--ok); }} .spark .drop {{ fill: var(--warn); }}
  .axis {{ display: flex; justify-content: space-between; font-size: .75rem; color: var(--muted); }}
  .events {{ margin: 10px 0 6px; padding: 0; list-style: none; display: grid; gap: 6px; }}
  .events li {{ padding: 6px 10px; border-radius: 10px; font-size: .88rem; }}
  .events li.rel {{ background: var(--ok-bg); }} .events li.drop {{ background: var(--warn-bg); }}
  .events b {{ margin-right: 4px; }} .events .rel b {{ color: var(--ok); }} .events .drop b {{ color: var(--warn); }}
  .grid {{ margin-top: 10px; overflow-x: auto; padding-bottom: 4px; }}
  .srow {{ display: grid; grid-template-columns: 18px repeat(var(--cols), minmax(5px, 9px)); gap: 2px; margin-bottom: 2px; }}
  .srow b {{ font-size: 9px; line-height: 9px; color: var(--muted); }}
  .grid i, .key i {{ display: block; height: 9px; border-radius: 2px; }}
  i.seat {{ background: var(--seat); }} i.rel {{ background: var(--ok); }} i.blk {{ background: var(--warn); }}
  .screen {{ margin: 6px 0 6px 20px; padding: 2px 0; text-align: center; font-size: 10px; letter-spacing: .3em; color: var(--muted);
            border: 1px solid var(--line); border-radius: 6px; }}
  .key {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; font-size: .78rem; color: var(--muted); }}
  .key i {{ display: inline-block; width: 10px; height: 10px; margin-right: 4px; vertical-align: -1px; }}
</style></head>
<body><main>{body}</main></body></html>"""


def register(app):
    """Serve the report pages that report alerts link to."""
    from fastapi.responses import HTMLResponse

    @app.get("/report/{token}", response_class=HTMLResponse)
    def report(token: str):
        task, data = report_for(token)
        return HTMLResponse(report_page(task, data), status_code=200 if task else 404)
