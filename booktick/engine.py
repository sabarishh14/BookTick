"""The checking engine: one scheduler thread and a few fetch workers, shared by every watcher.

Every second the scheduler works out which District pages the active watchers need (one
per cinema + date, or per movie + city + date for city-wide watchers) and hands the ones
that are due to the workers. Watchers on the same page share one fetch. A page that fails
is retried later and later; if District starts turning requests away, everyone waits.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from . import district, seatmap, tracker
from .actions import new_token
from .alerts import alert_links, describe_results, send_seatmap, send_task_notification
from .config import (ALERT_LOG_KEEP, CINEMA_FLAP_GAP, CITY_LIST_MAX_AGE, CITY_SPREAD_GAP, DAY_END_MARGIN, FETCH_WORKERS, KEEP_FINISHED_DAYS, MAX_BACKOFF, MAX_BLOCK_BACKOFF,
                     MAX_SEATMAPS, MORE_SEATS_GAP, NEW_SHOWS_GAP, SEATS_BACK_GAP, SEATS_MIN_DEFAULT, TIME_SLOTS)
from .core import (add_log, dates_label, format_matches, now_ist, plural, seats_min, shorten, state, task_cinemas,
                   task_dates, task_formats,
                   tasks_lock, to_date, today_ist, wake_engine)
from .storage import save_tasks


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def split_keywords(text):
    return [k.strip().lower() for k in (text or "").split(",") if k.strip()]


def on_date(show, date):
    return not date or not show["start"] or show["start"].strftime("%Y-%m-%d") == date


def show_matches(show, keywords, formats):
    """The movie (any keyword in its title) in one of the formats ([] or "ANY" for any format)."""
    if keywords and not any(k in show["title"].lower() for k in keywords):
        return False
    if isinstance(formats, str):
        formats = [] if formats.strip().upper() in ("", "ANY") else [formats]
    return not formats or any(format_matches(f, show) for f in formats)


def in_time_slots(show, slots):
    """True if the show starts in one of the picked parts of the day (or none were picked)."""
    if not slots or not show["start"]:
        return True
    hour = show["start"].hour + show["start"].minute / 60
    hour = hour + 24 if hour < 5 else hour  # a 1 AM show belongs to the night before
    return any(lo <= hour < hi for key, _, lo, hi in TIME_SLOTS if key in slots)


def seats_open(show):
    return show["avail"] is None or show["avail"] > 0


def wanted(task, show, date):
    """Does this show match the watcher (ignoring whether it has seats)?"""
    return (on_date(show, date) and show_matches(show, split_keywords(task.get("movie_filter")), task_formats(task))
            and in_time_slots(show, task.get("time_slots") or []))


def cinema_label(cinema):
    """A watcher's cinema as BookTick shows it everywhere: 'PVR Palazzo, The Nexus Vijaya Mall'."""
    return district.short_cinema_name(cinema.get("name")) or district.cinema_name_from_url(cinema.get("url", ""))


def task_place(task):
    """'Cinemas in Chennai', 'PVR Palazzo', or 'PVR Palazzo and 2 more cinemas'."""
    if task.get("scope") == "city":
        return f"Cinemas in {district.city_label(task.get('city'))}"
    cinemas = task_cinemas(task)
    if not cinemas:
        return "a cinema"
    first = cinema_label(cinemas[0])
    return first if len(cinemas) == 1 else f"{first} and {plural(len(cinemas) - 1, 'more cinema')}"


def page_base(page):
    return str(page or "").split("?")[0]


def cinema_for_page(task, page):
    """The watcher's cinema entry that a cinema page belongs to, or None."""
    base = page_base(page)
    return next((c for c in task_cinemas(task) if page_base(c["url"]) == base), None)


# ---------------------------------------------------------------------------
# Checking one page for its watchers
# ---------------------------------------------------------------------------
_results = {}  # (watcher id, page) -> last result logged, so the log only shows changes


def owners_of(task):
    return (task.get("owner"),)


def set_result(task, page, result, where=None, main=True):
    """Show `result` on the watcher's card, and log it when it changes. Call with tasks_lock.

    `main` is False for the cinemas a city-wide watcher also follows: their lines on the card
    show those, so the card's own result stays the city's."""
    task["last_checked"] = now_ist().strftime("%H:%M:%S")
    task["last_checked_ts"] = time.time()
    if main:
        task["last_result"] = result
    key = (task.get("id"), page)
    if _results.get(key) != result:
        _results[key] = result
        add_log(f"[{task.get('user_label', 'Watcher')}] {where or task_place(task)}: {result}", owners_of(task))


def seat_counts(shows):
    """{show link: seats left}. A show with seats but no count (from Schema.org) counts as 1."""
    return {s["url"]: (1 if s["avail"] is None else s["avail"]) for s in shows}


def seats_came_back(was, show, need=SEATS_MIN_DEFAULT):
    """Did a show already listed get at least `need` seats back (from sold out too)? 0 means never."""
    if need <= 0 or was is None or show["avail"] is None:
        return False
    return show["avail"] - was >= need


def show_cinema(show):
    """A show's cinema: District's id from its link, else its name."""
    return district.cinema_id_from_url(show.get("cinema_url")) or str(show.get("cinema") or "").strip().lower()


def told_cinemas(task):
    """{"<date>|<cinema>": when it was named} for a city-wide watcher. Watchers saved by an
    earlier version kept a plain list of keys, without times (0)."""
    raw = task.get("told_cinemas") or {}
    if isinstance(raw, dict):
        return dict(raw)
    return {str(k): 0 for k in raw if isinstance(k, str)} if isinstance(raw, list) else {}


def named(told):
    """The cinemas a city-wide watcher has named, on any day: one already open that adds shows
    for another day gets "new shows", like a watcher on that cinema would, not "new cinema"."""
    return {k.split("|", 1)[-1] for k in told}


def shows_place(task, shows):
    """Where a city-wide alert's shows are: the cinema, or '27 cinemas in Chennai' (the alert
    then lists them, one line each)."""
    names = list(dict.fromkeys(str(s.get("cinema") or "") for s in shows))
    if len(names) == 1 and names[0]:
        return district.short_cinema_name(names[0])
    return f"{len(names)} cinemas in {district.city_label(task.get('city'))}"


def check_task(task, page, date, place, shows):
    """Compare one page's shows with one watcher, and alert on what changed.

    A watcher alerts when bookings open (and repeats it as reminders), when they close again
    (taken off sale, or sold out), when they reopen, and when seats free up. It keeps
    watching until its dates pass or someone taps "Booked it" in an alert.
    """
    listed = [s for s in shows if on_date(s, date)]
    matched = [s for s in listed if wanted(task, s, date)]
    open_now = [s for s in matched if seats_open(s)]
    day = f"{dates_label(date).split(' · ')[-1]}: " if len(task_dates(task)) > 1 else ""
    event = reason = None
    alert_shows = open_now
    now = time.time()

    with tasks_lock:
        if state["tasks"].get(task.get("id")) is not task or task.get("status") != "Active":
            return  # deleted or paused while this check was running
        city = task.get("scope") == "city"
        if not city and not task.get("cinemas") and task.get("cinema_url"):
            task["cinemas"] = task_cinemas(task)  # a watcher from before a watcher could follow several cinemas
        entry = cinema_for_page(task, page)  # None for a city's listings
        if entry is not None and place and place != district.cinema_name_from_url(entry["url"]):
            entry["name"] = place  # District's own name, when the page gives one
        where = (cinema_label(entry) if entry is not None else district.short_cinema_name(place) if not city
                 else task_place(task))

        # Seats per matching show on each of the watcher's pages (one per date, or per movie).
        pages = task.setdefault("page_seats", {})
        pages[page] = seat_counts(matched)
        expected = set(task.get("expected_pages") or [page])
        known = {p: pages[p] for p in expected if p in pages}
        is_open = any(n > 0 for seats in known.values() for n in seats.values())
        phase = task.get("phase") or "waiting"
        # When each page's last matching show starts. Once that's (nearly) here, shows going off
        # sale or leaving the page is just the day ending there, not bookings closing.
        last_start = task.setdefault("page_last_start", {})
        starts = [s["start"].timestamp() for s in matched if s.get("start")]
        if starts:
            last_start[page] = max(starts + [last_start.get(page, 0)])
        ran_out = lambda p: now >= last_start.get(p, 0) - DAY_END_MARGIN  # noqa: E731

        listed_where = "across the city" if entry is None and city else "listed"
        open_count, match_count = len(open_now), len(matched)
        if entry is None and city:
            # Its movie name can match a few movies, one city page each: the card adds up that day's.
            day_pages = [s for p, s in dict(known, **{page: pages[page]}).items()
                         if cinema_for_page(task, p) is None and p.endswith(f"fromdate={date}")]
            open_count = sum(1 for s in day_pages for n in s.values() if n > 0)
            match_count = sum(len(s) for s in day_pages)
        set_result(task, page, f"🔥 {day}{plural(open_count, 'show')} open" if open_count
                   else f"{day}Sold out · {plural(match_count, 'matching show')}" if match_count
                   else f"{day}No match · {plural(len(listed), 'show')} {listed_where}" if listed
                   else f"{day}No shows listed yet", where, main=entry is None or not city)

        max_alerts = max(1, int(task.get("max_alerts", 5)))
        # Which of its own cinemas have seats now (a city's listings aren't one of them). Each one
        # that opens after the first gets its own alert: on a watcher with several cinemas, or a
        # city-wide watcher that also follows a few.
        open_places = sorted({page_base(p) for p, seats in known.items()
                              if cinema_for_page(task, p) is not None and any(n > 0 for n in seats.values())})
        prior_open = set(task.get("open_places") or [])
        newly_open = page_base(page) in open_places and page_base(page) not in prior_open
        says_also = city or len(task_cinemas(task)) > 1
        also_sent = (task.get("also_open_ts") or {}).get(page_base(page), 0)  # once in a while per cinema

        # Each of its own cinemas, on a watcher with several: seen_open (has opened before, so
        # opening again isn't "new"), closed_cinemas (we said it closed, so say when it reopens).
        if "seen_open" not in task:
            task["seen_open"] = sorted(prior_open | set(task.get("also_open_ts") or {}))  # watchers from before
        seen = set(task["seen_open"])
        closed_at = dict(task.get("closed_cinemas") or {})
        flap_ts = dict(task.get("cinema_alert_ts") or {})
        this_cinema = page_base(page) if entry is not None else None
        closing = None  # "sold_out" or "pulled" when this cinema has just closed for real
        just_closed = this_cinema in prior_open and this_cinema not in open_places
        if entry is not None and says_also and just_closed and is_open:
            its_pages = [p for p in known if page_base(p) == this_cinema]
            if all(ran_out(p) for p in its_pages):
                seen.discard(this_cinema)  # its day is over: its next shows (another day) open as new
                closed_at.pop(this_cinema, None)
            elif now - flap_ts.get(this_cinema, 0) >= CINEMA_FLAP_GAP:
                closing = "sold_out" if any(known[p] for p in its_pages) else "pulled"

        # Across a city: each cinema that opens after the first alert gets named, once per day.
        # Ones that open close together share an alert, and none is dropped while it waits.
        # (A city-wide watcher's own cinemas have their own alert, from their own pages.)
        # told_cinemas: {"<date>|<cinema>": when it was told, or 0 if it was open before that was noted}.
        told = told_cinemas(task)
        key = lambda s: f"{date}|{show_cinema(s)}"  # noqa: E731
        spread, own = [], set()
        if entry is None and city:
            if "told_cinemas" not in task and phase == "open":
                task["told_quiet_until"] = now + 90  # open before watchers did this: each page counts what's open
            if now < task.get("told_quiet_until", 0):
                told.update({key(s): told.get(key(s), 0) for s in open_now})
            own = {district.cinema_id_from_url(c["url"]) for c in task_cinemas(task)}
            spread = [s for s in open_now if show_cinema(s) not in named(told) and show_cinema(s) not in own]
            # For the card: the cinemas with seats on this page (each page is one movie and day).
            city_rows = {}
            for s in open_now:
                row = city_rows.setdefault(show_cinema(s), [s.get("cinema") or "Cinema", s.get("cinema_url"), 0, date])
                row[2] += 1
            task.setdefault("city_open", {})[page] = city_rows

        # At cinemas that were already open: shows added since, and seats back in shows already
        # listed, compared with what the last alert saw (seat_base). A change that has to wait for
        # its gap stays out of seat_base, so the next alert has it instead of it being lost. For a
        # minute after bookings open, every page just takes in what's there: that's all "open".
        bases = task.setdefault("seat_base", {})
        base = bases.get(page)
        settling = now < task.get("settle_until", 0)
        city_page = entry is None and city
        if entry is not None:
            cinema_was_open = page_base(page) in (task.get("open_places") or [])  # on any of its days
        else:
            cinema_was_open = any(n > 0 for n in (base or {}).values())
        fresh, freed = [], []
        need = seats_min(task)
        if base is not None and phase == "open" and not settling:
            for s in open_now:
                if city_page and (show_cinema(s) not in named(told) or show_cinema(s) in own):
                    continue  # a city cinema's first shows are "more cinemas"; its own cinemas, their pages
                if not city_page and not cinema_was_open:
                    continue  # this cinema opening at all is its own alert
                if s["url"] not in base:
                    fresh.append(s)
                elif seats_came_back(base[s["url"]], s, need):
                    freed.append(dict(s, was=base[s["url"]]))
        quiet = task.get("reminders_left", 0) <= 0  # these wait until the reminders are done

        if is_open and phase != "open" and open_now:
            event = "reopened" if phase == "closed" else "opened"
            task.update(phase="open", reminders_left=max_alerts - 1, last_spread_ts=now, settle_until=now + 60)
            if entry is None and city:
                told.update({key(s): told.get(key(s), now) for s in open_now})
            if event == "opened":
                closed_at = {}
            closed_at.pop(this_cinema, None)  # the rest of its cinemas say so as each reopens
        elif not is_open and phase == "open" and len(known) == len(expected):
            # Every page has reported since bookings opened, and none has seats now.
            if all(ran_out(p) for p in known):
                # Every page's last show has started: the day is over, nothing closed. Back to
                # waiting, so another day's shows opening alerts like the first time.
                task.update(phase="waiting", reminders_left=0)
                seen, closed_at = set(), {}
                add_log(f"[{task.get('user_label', 'Watcher')}] {where}: the day's matching shows have all started. "
                        "Still watching for more.", owners_of(task))
            else:
                event = "closed"
                reason = "sold_out" if any(known.values()) else "pulled"
                alert_shows = matched
                task.update(phase="closed", reminders_left=0)
                closed_at = {c: now for c in seen | prior_open}  # each says so as it reopens
        elif phase == "open" and open_now and this_cinema in closed_at and this_cinema in open_places:
            event = "cinema_reopened"  # one we said had closed has seats again
            closed_at.pop(this_cinema)
            flap_ts[this_cinema] = now
        elif (phase == "open" and open_now and newly_open and this_cinema not in seen and says_also
              and now - also_sent >= MORE_SEATS_GAP):
            event = "also_open"  # bookings opened at another of its cinemas, the first time there
            task["also_open_ts"] = dict(task.get("also_open_ts") or {}, **{page_base(page): now})
        elif phase == "open" and closing:
            event, reason, alert_shows = "cinema_closed", closing, matched  # one cinema closed, others still open
            closed_at[this_cinema] = now
            flap_ts[this_cinema] = now
        elif phase == "open" and spread and now - task.get("last_spread_ts", 0) >= CITY_SPREAD_GAP:
            event, alert_shows = "spread", spread  # opened at more cinemas across the city
            told.update({key(s): now for s in spread})
            task["last_spread_ts"] = now
        elif fresh and quiet and now - task.get("last_new_ts", 0) >= NEW_SHOWS_GAP:
            event, alert_shows = "new_shows", fresh  # a cinema already open added shows
            task["last_new_ts"] = now
        elif freed and quiet and now - task.get("last_more_ts", 0) >= SEATS_BACK_GAP:
            event, alert_shows = "seats_back", freed  # seats came back in shows already listed
            task["last_more_ts"] = now

        current = seat_counts(matched)
        if base is None or settling or event in ("opened", "reopened", "also_open", "closed", "cinema_closed",
                                                 "cinema_reopened"):
            bases[page] = current
        else:
            sent = {s["url"] for s in alert_shows} if event in ("new_shows", "seats_back") else set()
            for s in fresh:
                if s["url"] not in sent:
                    current.pop(s["url"], None)  # still to be told
            for s in freed:
                if s["url"] not in sent:
                    current[s["url"]] = s["was"]
            bases[page] = current
        if (event is None and open_now and task.get("reminders_left", 0) > 0
                and now - task.get("last_alert_ts", 0) >= state["interval_sec"] * 0.8):
            event = "reminder"  # at most one per round, even when several pages are open
            task["reminders_left"] -= 1
        if entry is None and city and told != task.get("told_cinemas"):
            task["told_cinemas"] = told
            if event is None:
                save_tasks()
        seen |= set(open_places)
        changed = {"seen_open": sorted(seen), "closed_cinemas": closed_at, "cinema_alert_ts": flap_ts,
                   "open_places": open_places}
        changed = {k: v for k, v in changed.items() if v != (task.get(k) or ([] if isinstance(v, list) else {}))}
        if changed:
            task.update(changed)
            if event is None:
                save_tasks()
        if event is None:
            return
        # Closed means every one of its cinemas, so name them all; anything else is about this page's cinema.
        place_name = task_place(task) if event == "closed" else where
        if entry is None and city and event != "closed" and alert_shows:
            place_name = shows_place(task, alert_shows)  # a city's listings: name the cinemas, not just the city
        # The card lists the alerts sent, newest first, each linking to its cinema when there's one.
        cinemas_in = len({show_cinema(s) for s in alert_shows})
        cinema_url = entry["url"] if entry is not None else None  # this page's cinema, when it is one
        link = (alert_links(alert_shows[0])[0][1] if alert_shows and cinemas_in == 1
                else district.query_url(cinema_url, date) if cinema_url and event != "closed" else None)
        record = {"ts": now, "kind": event, "place": place_name, "shows": len(alert_shows), "cinemas": cinemas_in,
                  "link": link, "reason": reason}
        task["alert_log"] = (task.get("alert_log") or [])[-(ALERT_LOG_KEEP - 1):] + [record]
        task["alert_count"] = int(task.get("alert_count", 0)) + 1
        task["last_alert_ts"] = now
        if not task.get("action_token"):
            task["action_token"] = new_token()  # watchers made before alerts had buttons
        save_tasks()
        snapshot = dict(task)

    owners = owners_of(snapshot)
    name = snapshot.get("user_label", "Watcher")
    count = max_alerts - snapshot.get("reminders_left", 0)
    shows_text = plural(len(alert_shows), "matching show")
    add_log({
        "opened": f"🔥 [{name}] bookings are open at {place_name}: {shows_text} with seats! Alerting…",
        "reopened": f"🔁 [{name}] bookings reopened at {place_name}: {shows_text} with seats! Alerting…",
        "reminder": f"🔥 [{name}] still open at {place_name}. Reminder {count}/{max_alerts}…",
        "new_shows": f"🆕 [{name}] new shows at {place_name}: {shows_text} with seats. Alerting…",
        "seats_back": f"🎟️ [{name}] seats released at {place_name} in {shows_text}. Alerting…",
        "also_open": f"🔥 [{name}] bookings are now also open at {place_name}: {shows_text} with seats! Alerting…",
        "spread": f"🔥 [{name}] now open at more cinemas: {place_name}, {shows_text} with seats. Alerting…",
        "closed": f"⏸️ [{name}] bookings closed at {place_name} "
                  f"({'sold out' if reason == 'sold_out' else 'taken off sale'}). Still watching.",
        "cinema_closed": f"⏸️ [{name}] {place_name} {'sold out' if reason == 'sold_out' else 'took its shows off sale'}; "
                         "its other cinemas are still open. Alerting…",
        "cinema_reopened": f"🔁 [{name}] {place_name} has seats again: {shows_text}. Alerting…",
    }[event], owners)
    results = send_task_notification(snapshot, alert_shows, place_name, kind=event, count=count,
                                     max_alerts=max_alerts, reason=reason, date=date,
                                     cinema_url=cinema_url if event != "closed" else None)
    add_log(f"   [{name}] {describe_results(results)}", owners)
    with tasks_lock:
        record["ok"] = all(ok for _, ok, _ in results) if results else None  # None: no channels to send to
    if event in ("opened", "reopened", "also_open"):
        request_seatmap(snapshot, alert_shows, place_name)


def request_seatmap(task, shows, place):
    """Ask for a picture of each matching show's seats, earliest first (up to MAX_SEATMAPS).

    They follow the opening alert one by one, a few seconds apart, so the alert itself is
    never held up.
    """
    with_maps = [s for s in shows if "/seat-layout/" in s.get("url", "")]
    for show in with_maps[:MAX_SEATMAPS]:
        seatmap.request(task, show, show.get("cinema") or place, seatmap_ready)  # each map says its cinema
    if len(with_maps) > MAX_SEATMAPS:
        add_log(f"🗺️ [{task.get('user_label', 'Watcher')}] seat maps for the first {MAX_SEATMAPS} of "
                f"{len(with_maps)} shows only.", owners_of(task))


def seatmap_ready(task, show, place, image_id):
    hit = seatmap.image(image_id)
    if not hit:
        return
    with tasks_lock:
        live = state["tasks"].get(task.get("id"))
        if live:
            live.update(seatmap_id=image_id, seatmap_at=now_ist().strftime("%I:%M %p").lstrip("0"))
    results = send_seatmap(task, show, place, hit["png"], hit["free"], hit["total"], seatmap.image_url(image_id))
    add_log(f"🗺️ [{task.get('user_label', 'Watcher')}] seat map as bookings opened: {hit['free']} of {hit['total']} "
            f"seats free. {describe_results(results)}", owners_of(task))


def check_city_list(job, directory):
    """A city-wide watcher whose movie isn't in the city's listings yet."""
    for task in job["tasks"]:
        movies = district.matching_movies(directory["movies"], split_keywords(task.get("movie_filter")))
        if not movies:
            with tasks_lock:
                if state["tasks"].get(task.get("id")) is task and task.get("status") == "Active":
                    set_result(task, "list", f"Not showing in {district.city_label(task.get('city'))} yet")


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_pages = {}       # page -> {"next": time it's due, "fails": failures in a row}
_inflight = set()  # pages being fetched right now
_block = {"until": 0.0, "strikes": 0}  # set when District turns requests away
_stats = {"pages": 0, "failing": 0}
_pool = ThreadPoolExecutor(FETCH_WORKERS, thread_name_prefix="booktick-fetch")


def page_jobs(task, today):
    """The pages one watcher needs: [(kind, page, date)]. Kinds: cinema, city (a movie across a
    city) and list (a city's listings, read until a city-wide watcher's movie shows up)."""
    dates = [d for d in task_dates(task) if d >= today]
    if not dates:
        return []
    cinemas = [("cinema", district.query_url(c["url"], d), d) for c in task_cinemas(task) for d in dates]
    city = task.get("city")
    if task.get("scope") != "city" or not city:
        return cinemas
    entry = district.peek(f"city:{city}")
    movies = district.matching_movies(entry["value"]["movies"], split_keywords(task.get("movie_filter"))) if entry else []
    jobs = cinemas  # a city-wide watcher's own cinemas, if it has any
    # Read the city's listings while the movie isn't in them yet (the job reuses a list
    # that's still fresh), and refresh them now and then once it is.
    if not movies or entry["age"] > CITY_LIST_MAX_AGE:
        jobs.append(("list", f"list:{city}", None))
    for movie in movies:
        for d in dates:
            url = district.city_movie_url(movie["url"], city, d)
            if url:
                jobs.append(("city", url, d))
    return jobs


def plan(active, today):
    pages = {}
    for task in active:
        jobs = page_jobs(task, today)
        # Bookings count as closed only once every one of these pages shows no seats.
        task["expected_pages"] = [page for kind, page, _ in jobs if kind != "list"]
        for kind, page, date in jobs:
            job = pages.setdefault(page, {"kind": kind, "date": date, "city": task.get("city"), "tasks": []})
            job["tasks"].append(task)
    return pages


def blocked_status(err):
    status = getattr(getattr(err, "response", None), "status_code", None)
    return status if status in (403, 429) else None


def run_page(page, job):
    kind = job["kind"]
    try:
        if kind == "list":
            check_city_list(job, district.city_directory(job["city"], max_age=CITY_LIST_MAX_AGE))
        else:
            place, shows = district.fetch_shows(kind, page)
            if kind == "city":
                place = f"Cinemas in {district.city_label(job['city'])}"
            for task in job["tasks"]:
                if tracker.is_tracker(task):
                    tracker.observe(task, place, [s for s in shows if wanted(task, s, job["date"])])
                else:
                    check_task(task, page, job["date"], place, shows)
        page_succeeded(page, kind, job)
    except Exception as e:
        page_failed(page, job, e)
    finally:
        with _lock:
            _inflight.discard(page)


def page_succeeded(page, kind, job):
    interval = CITY_LIST_MAX_AGE if kind == "list" else state["interval_sec"]
    with _lock:
        recovered = _pages.get(page, {}).get("fails", 0) > 0
        # A little jitter so pages added together don't stay in lockstep.
        _pages[page] = {"next": time.time() + interval * random.uniform(0.9, 1.1), "fails": 0}
        _block["strikes"] = 0
    state["check_count"] += 1
    state["last_check_time"] = now_ist()
    if recovered:
        owners = {t.get("owner") for t in job["tasks"]}
        add_log(f"✅ {shorten(page_name(job, page), 60)} is loading again.", owners)


def page_failed(page, job, err):
    now = time.time()
    with _lock:
        fails = _pages.get(page, {}).get("fails", 0) + 1
        _pages[page] = {"next": now + min(state["interval_sec"] * 2 ** fails, MAX_BACKOFF), "fails": fails}
        status = blocked_status(err)
        if status:
            _block["strikes"] += 1
            wait = min(60 * 2 ** (_block["strikes"] - 1), MAX_BLOCK_BACKOFF)
            _block["until"] = now + wait
    if status:
        add_log(f"⚠️ District turned a request away (HTTP {status}). Checking pauses for {wait // 60 or 1} min.", owners=())
    with tasks_lock:
        for task in job["tasks"]:
            if state["tasks"].get(task.get("id")) is task and task.get("status") == "Active":
                set_result(task, page, "⚠️ Page didn't load. Retrying", page_name(job, page),
                           main=job["kind"] != "cinema" or task.get("scope") != "city")
    if fails == 1:
        owners = {t.get("owner") for t in job["tasks"]}
        add_log(f"⚠️ Couldn't load {shorten(page_name(job, page), 60)}: {shorten(err, 120)}", owners)


def page_name(job, page=""):
    first = job["tasks"][0] if job.get("tasks") else {}
    if job["kind"] == "list":
        return f"the cinema list for {district.city_label(job.get('city'))}"
    if job["kind"] == "city":
        return f"{shorten(first.get('movie_filter'), 30)} in {district.city_label(job.get('city'))}"
    entry = cinema_for_page(first, page) if first else None
    return cinema_label(entry) if entry else district.cinema_name_from_url(page) if page else "a cinema page"


def housekeeping(today):
    """Retire watchers whose dates have passed, send seat trackers' reports once their day is
    over, and remove finished ones a week later."""
    cutoff = str(to_date(today) - timedelta(days=KEEP_FINISHED_DAYS))
    reports = []
    with tasks_lock:
        changed = False
        for tid, task in list(state["tasks"].items()):
            last = task_dates(task)[-1]
            if last < cutoff:
                del state["tasks"][tid]
                changed = True
            elif tracker.is_tracker(task):
                if tracker.due(task):
                    reports.append(tid)
            elif last < today and task.get("status") in ("Active", "Paused"):
                task["status"] = "Expired"
                task["last_result"] = "Date passed"
                changed = True
                add_log(f"📅 [{task.get('user_label', 'Watcher')}] the date has passed, so this watcher stopped.",
                        owners_of(task))
        if changed:
            save_tasks()
    for tid in reports:  # sending takes a few seconds, so not on the scheduler's thread
        _pool.submit(tracker.finish, tid)


def scheduler():
    add_log("▶ Monitoring engine started.", owners=())
    last_housekeeping = 0.0
    while True:
        try:
            now, today = time.time(), today_ist()
            if now - last_housekeeping > 60:
                housekeeping(today)
                last_housekeeping = now
            if not state["paused"] and now >= _block["until"]:
                with tasks_lock:
                    active = [t for t in state["tasks"].values() if t.get("status") == "Active"]
                pages = plan(active, today)
                with _lock:
                    for page in [p for p in _pages if p not in pages and p not in _inflight]:
                        del _pages[page]  # nobody needs it any more
                    _stats.update(pages=len(pages), failing=sum(1 for p in _pages.values() if p["fails"]))
                    due = [p for p in pages if p not in _inflight and _pages.get(p, {}).get("next", 0) <= now]
                    _inflight.update(due)
                for page in due:
                    _pool.submit(run_page, page, pages[page])
        except Exception as e:
            add_log(f"Engine error: {e}", owners=())
        wake_engine.wait(timeout=1)
        wake_engine.clear()


def health():
    """For the admin panel."""
    with _lock:
        blocked_for = max(0, int(_block["until"] - time.time()))
        return dict(_stats, inflight=len(_inflight), blocked_for=blocked_for)


_thread = None


def start_engine():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=scheduler, daemon=True, name="booktick-engine")
        _thread.start()


def reset_page_timers():
    """Make every page due now (after the interval changes or the engine resumes)."""
    with _lock:
        for entry in _pages.values():
            if not entry["fails"]:
                entry["next"] = 0
    wake_engine.set()


# ---------------------------------------------------------------------------
# For the watcher cards
# ---------------------------------------------------------------------------
def cinema_statuses(task_id):
    """[(cinema name, link, state, shows with seats)] for a watcher on cinemas, from its last checks.

    state is "open", "soldout" (matching shows, none with seats), "waiting" (no matching shows
    yet) or "unchecked". Read under the lock, since the engine updates these while checking.
    """
    with tasks_lock:
        task = state["tasks"].get(task_id)
        if not task:
            return []
        pages = {p: dict(seats) for p, seats in (task.get("page_seats") or {}).items()}
        cinemas = [dict(c) for c in task_cinemas(task)]
        off_sale = set(task.get("closed_cinemas") or {})  # closed with an alert: shows taken off sale
    out = []
    for c in cinemas:
        mine = [seats for p, seats in pages.items() if page_base(p) == page_base(c["url"])]
        with_seats = sum(1 for seats in mine for n in seats.values() if n > 0)
        any_shows = any(seats for seats in mine)
        status = ("unchecked" if not mine else "open" if with_seats else "soldout" if any_shows
                  else "offsale" if page_base(c["url"]) in off_sale else "waiting")
        out.append((cinema_label(c), c["url"], status, with_seats))
    return out


def city_open(task_id):
    """[(cinema name, link, shows with seats, when it opened or 0)] for a city-wide watcher: the
    cinemas with seats at its last checks, newest to open first (not its own cinemas, which
    have their own lines)."""
    with tasks_lock:
        task = state["tasks"].get(task_id)
        if not task or task.get("scope") != "city":
            return []
        expected = set(task.get("expected_pages") or [])
        pages = [dict(rows) for p, rows in (task.get("city_open") or {}).items() if p in expected]
        told = told_cinemas(task)
        own ={district.cinema_id_from_url(c["url"]) for c in task_cinemas(task)}
    merged = {}
    for rows in pages:
        for cid, (name, url, shows, date) in rows.items():
            if cid in own:
                continue
            row = merged.setdefault(cid, {"name": name, "url": url, "shows": 0, "date": date, "at": 0})
            row["shows"] += shows
            row["date"] = min(row["date"], date)  # the link opens its first day with seats
    for cid, row in merged.items():  # when it was first named, on any day
        times = [at for k, at in told.items() if k.split("|", 1)[-1] == cid]
        row["at"] = min(times) if times else 0
    rows = sorted(merged.values(), key=lambda r: (-r["at"], r["name"].lower()))
    return [(r["name"], district.query_url(r["url"], r["date"]) if r["url"] else None, r["shows"], r["at"]) for r in rows]
