"""Is District really taking bookings where a page first shows seats?

District can list a cinema's shows with seats before the cinema takes bookings: tapping one
says "This showtime isn't available right now". So before a page counts as open for the
first time, BookTick opens one listed show's seat page (as a guest, like a seat map) and
holds the alert until it loads. What it learns is kept per cinema and day.

It never holds back a show that is on sale: when it can't tell (no browser on the server,
the page didn't load, District turned BookTick away), the shows count as open.
"""
import threading
import time

from . import district, seatmap
from .core import add_log, shorten

YES_FOR = 1800      # a cinema's day that took bookings isn't asked about again for this long
NO_FOR = 180        # one that didn't is asked again after this, or as soon as its seat counts move
UNKNOWN_FOR = 600   # couldn't tell: counts as on sale, and isn't asked about again for this long
ASKING_FOR = 90     # an answer that never came is given up on after this
ASK_AT_ONCE = 2     # cinemas on one page (a city's listings) asked about at the same time

_lock = threading.Lock()
_known = {}  # (cinema, date) -> {"state", "ts", "why", "seats", "pages"}


def cinema_of(show):
    return district.cinema_id_from_url(show.get("cinema_url")) or str(show.get("cinema") or "").strip().lower()


def seat_counts(shows):
    return {s["url"]: s.get("avail") for s in shows}


def moved(entry, shows):
    """Have any of these shows' seat counts changed since District said no? That's people booking.
    (Watchers with other filters see other shows of the same cinema, so only shows seen before count.)"""
    seats = entry.get("seats") or {}
    if any(s["url"] in seats and seats[s["url"]] != s.get("avail") for s in shows):
        return True
    for s in shows:
        seats.setdefault(s["url"], s.get("avail"))  # so a change in these counts later, too
    return False


def current(entry, shows, now):
    """The entry's state while it still holds, else None (time to ask again)."""
    if not entry:
        return None
    age = now - entry["ts"]
    state = entry["state"]
    if state == "yes":
        return state if age < YES_FOR else None
    if state == "unknown":
        return state if age < UNKNOWN_FOR else None
    if state == "asking":
        return state if age < ASKING_FOR else None
    return state if age < NO_FOR and not moved(entry, shows) else None


def verdict(shows, date, page, check_again):
    """For a page's shows with seats, on a page that had none at the watcher's last check:
    ("yes", "") when a cinema among them takes bookings (or BookTick can't tell), ("asking", "")
    while it finds out, or ("no", District's words) when none does. `check_again(pages)` runs
    when an answer comes, so the pages that asked are checked again straight away."""
    if not seatmap.enabled():
        return "yes", ""
    by_cinema = {}
    for s in sorted(shows, key=lambda s: s["start"].timestamp() if s.get("start") else float("inf")):
        by_cinema.setdefault(cinema_of(s), []).append(s)
    links = {c: next((s["url"] for s in group if "/seat-layout/" in s.get("url", "")), None)
             for c, group in by_cinema.items()}
    if not any(links.values()):
        return "yes", ""  # no seat page to open, so no way to tell
    now = time.time()
    ask = []
    with _lock:
        states = {c: current(_known.get((c, date)), by_cinema[c], now) if links[c] else "unknown" for c in by_cinema}
        if any(s in ("yes", "unknown") for s in states.values()):
            return "yes", ""
        asking = [c for c, s in states.items() if s == "asking"]
        for c in asking:
            _known[(c, date)]["pages"].add(page)
        for c in [c for c, s in states.items() if s is None][:max(0, ASK_AT_ONCE - len(asking))]:
            _known[(c, date)] = {"state": "asking", "ts": now, "why": "", "seats": None, "pages": {page}}
            ask.append(c)
        refused = [_known[(c, date)]["why"] for c, s in states.items() if s == "no"]
    for c in ask:
        name = by_cinema[c][0].get("cinema") or "a cinema"
        if not seatmap.check_on_sale(links[c], lambda v, why, c=c, name=name: answered(
                (c, date), v, why, seat_counts(by_cinema[c]), name, check_again)):
            answered((c, date), "unknown", "the seat page queue is full", None, name, None)
    if asking or ask:
        return "asking", ""
    return "no", next((w for w in refused if w), "")


def answered(key, state, why, seats, name, check_again):
    with _lock:
        pages = (_known.get(key) or {}).get("pages") or set()
        _known[key] = {"state": state, "ts": time.time(), "why": why, "seats": seats, "pages": set()}
        for k in [k for k, e in _known.items() if time.time() - e["ts"] > 2 * YES_FOR]:
            del _known[k]
    if state == "no":
        add_log(f"⏳ {district.short_cinema_name(name)}, {key[1]}: listed with seats, but District isn't taking "
                f"bookings yet ({why}). Asking again when its seats move.", owners=())
    elif state == "unknown":
        add_log(f"🔎 Couldn't open a seat page at {district.short_cinema_name(name)} ({shorten(why, 100)}), "
                "so its shows count as open.", owners=())
    if check_again and pages:
        check_again(pages)


def reset():
    with _lock:
        _known.clear()
