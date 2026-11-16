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
from .alerts import describe_results, send_seatmap, send_task_notification
from .config import (CITY_LIST_MAX_AGE, FETCH_WORKERS, KEEP_FINISHED_DAYS, MAX_BACKOFF, MAX_BLOCK_BACKOFF,
                     MAX_SEATMAPS, MORE_SEATS_GAP, MORE_SEATS_MIN, TIME_SLOTS)
from .core import (add_log, dates_label, format_matches, now_ist, plural, shorten, state, task_cinemas, task_dates,
                   task_formats,
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
    return cinema.get("name") or district.cinema_name_from_url(cinema.get("url", ""))


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


def gained_seats(before, show):
    """Did this show get seats since the last check: new, back from sold out, or MORE_SEATS_MIN more?"""
    was = before.get(show["url"])
    if was is None:
        return True
    if show["avail"] is None:
        return False
    return (was == 0 < show["avail"]) or show["avail"] - was >= MORE_SEATS_MIN


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
        where = place if entry is not None or not city else task_place(task)

        # Seats per matching show on each of the watcher's pages (one per date, or per movie).
        pages = task.setdefault("page_seats", {})
        before = pages.get(page)
        pages[page] = seat_counts(matched)
        expected = set(task.get("expected_pages") or [page])
        known = {p: pages[p] for p in expected if p in pages}
        is_open = any(n > 0 for seats in known.values() for n in seats.values())
        phase = task.get("phase") or "waiting"

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
        newly_open = page_base(page) in open_places and page_base(page) not in (task.get("open_places") or [])
        says_also = city or len(task_cinemas(task)) > 1
        also_sent = (task.get("also_open_ts") or {}).get(page_base(page), 0)  # once in a while per cinema
        if is_open and phase != "open" and open_now:
            event = "reopened" if phase == "closed" else "opened"
            task.update(phase="open", reminders_left=max_alerts - 1)
        elif not is_open and phase == "open" and len(known) == len(expected):
            # Every page has reported since bookings opened, and none has seats now.
            event = "closed"
            reason = "sold_out" if any(known.values()) else "pulled"
            alert_shows = matched
            task.update(phase="closed", reminders_left=0)
        elif phase == "open" and open_now and newly_open and says_also and now - also_sent >= MORE_SEATS_GAP:
            event = "also_open"  # bookings opened at another of its cinemas
            task["also_open_ts"] = dict(task.get("also_open_ts") or {}, **{page_base(page): now})
            task["last_more_ts"] = now
        elif (open_now and before is not None and task.get("reminders_left", 0) <= 0
              and now - task.get("last_more_ts", 0) >= MORE_SEATS_GAP):
            freed = [s for s in open_now if gained_seats(before, s)]
            if freed:
                event, alert_shows = "more_seats", freed
                task["last_more_ts"] = now
        if (event is None and open_now and task.get("reminders_left", 0) > 0
                and now - task.get("last_alert_ts", 0) >= state["interval_sec"] * 0.8):
            event = "reminder"  # at most one per round, even when several pages are open
            task["reminders_left"] -= 1
        if open_places != (task.get("open_places") or []):
            task["open_places"] = open_places
            if event is None:
                save_tasks()
        if event is None:
            return
        task["alert_count"] = int(task.get("alert_count", 0)) + 1
        task["last_alert_ts"] = now
        if not task.get("action_token"):
            task["action_token"] = new_token()  # watchers made before alerts had buttons
        save_tasks()
        snapshot = dict(task)

    owners = owners_of(snapshot)
    name = snapshot.get("user_label", "Watcher")
    # Closed means every one of its cinemas, so name them all; anything else is about this page's cinema.
    place_name = task_place(snapshot) if event == "closed" else where
    count = max_alerts - snapshot.get("reminders_left", 0)
    shows_text = plural(len(alert_shows), "matching show")
    add_log({
        "opened": f"🔥 [{name}] bookings are open at {place_name}: {shows_text} with seats! Alerting…",
        "reopened": f"🔁 [{name}] bookings reopened at {place_name}: {shows_text} with seats! Alerting…",
        "reminder": f"🔥 [{name}] still open at {place_name}. Reminder {count}/{max_alerts}…",
        "more_seats": f"🎟️ [{name}] seats freed up at {place_name} in {shows_text}. Alerting…",
        "also_open": f"🔥 [{name}] bookings are now also open at {place_name}: {shows_text} with seats! Alerting…",
        "closed": f"⏸️ [{name}] bookings closed at {place_name} "
                  f"({'sold out' if reason == 'sold_out' else 'taken off sale'}). Still watching.",
    }[event], owners)
    results = send_task_notification(snapshot, alert_shows, place_name, kind=event, count=count,
                                     max_alerts=max_alerts, reason=reason, date=date)
    add_log(f"   [{name}] {describe_results(results)}", owners)
    if event in ("opened", "reopened", "also_open"):
        request_seatmap(snapshot, alert_shows, place_name)


def request_seatmap(task, shows, place):
    """Ask for a picture of each matching show's seats, earliest first (up to MAX_SEATMAPS).

    They follow the opening alert one by one, a few seconds apart, so the alert itself is
    never held up.
    """
    with_maps = [s for s in shows if "/seat-layout/" in s.get("url", "")]
    for show in with_maps[:MAX_SEATMAPS]:
        seatmap.request(task, show, place, seatmap_ready)
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
    out = []
    for c in cinemas:
        mine = [seats for p, seats in pages.items() if page_base(p) == page_base(c["url"])]
        with_seats = sum(1 for seats in mine for n in seats.values() if n > 0)
        any_shows = any(seats for seats in mine)
        status = ("unchecked" if not mine else "open" if with_seats else "soldout" if any_shows else "waiting")
        out.append((cinema_label(c), c["url"], status, with_seats))
    return out
