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

from . import district
from .alerts import describe_results, send_task_notification
from .config import CITY_LIST_MAX_AGE, FETCH_WORKERS, KEEP_FINISHED_DAYS, MAX_BACKOFF, MAX_BLOCK_BACKOFF, TIME_SLOTS
from .core import (add_log, dates_label, now_ist, plural, shorten, state, task_dates, tasks_lock, to_date,
                   today_ist, wake_engine)
from .storage import save_tasks


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def split_keywords(text):
    return [k.strip().lower() for k in (text or "").split(",") if k.strip()]


def on_date(show, date):
    return not date or not show["start"] or show["start"].strftime("%Y-%m-%d") == date


def show_matches(show, keywords, fmt_filter):
    if keywords and not any(k in show["title"].lower() for k in keywords):
        return False
    fmt = (fmt_filter or "ANY").strip().upper()
    return fmt in ("", "ANY") or fmt in str(show["format"]).upper()


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
    return (on_date(show, date) and show_matches(show, split_keywords(task.get("movie_filter")), task.get("format_filter"))
            and in_time_slots(show, task.get("time_slots") or []))


def task_place(task):
    if task.get("scope") == "city":
        return f"Cinemas in {district.city_label(task.get('city'))}"
    return task.get("cinema_name") or district.cinema_name_from_url(task.get("cinema_url", ""))


# ---------------------------------------------------------------------------
# Checking one page for its watchers
# ---------------------------------------------------------------------------
_results = {}  # (watcher id, page) -> last result logged, so the log only shows changes


def owners_of(task):
    return (task.get("owner"),)


def set_result(task, page, result):
    """Show `result` on the watcher's card, and log it when it changes. Call with tasks_lock."""
    task["last_checked"] = now_ist().strftime("%H:%M:%S")
    task["last_checked_ts"] = time.time()
    task["last_result"] = result
    key = (task.get("id"), page)
    if _results.get(key) != result:
        _results[key] = result
        add_log(f"[{task.get('user_label', 'Watcher')}] {task_place(task)}: {result}", owners_of(task))


def check_task(task, page, date, place, shows):
    """Compare one page's shows with one watcher, and alert if a show matches and has seats."""
    listed = [s for s in shows if on_date(s, date)]
    matching = [s for s in listed if wanted(task, s, date) and seats_open(s)]
    multi_day = len(task_dates(task)) > 1
    day = f"{dates_label(date).split(' · ')[-1]}: " if multi_day else ""

    with tasks_lock:
        if state["tasks"].get(task.get("id")) is not task or task.get("status") != "Active":
            return  # deleted or paused while this check was running
        if task.get("scope") != "city" and place != district.cinema_name_from_url(task.get("cinema_url")):
            task["cinema_name"] = place  # District's own name, when the page gives one
        if not matching:
            where = "across the city" if task.get("scope") == "city" else "listed"
            set_result(task, page, f"{day}No match · {plural(len(listed), 'show')} {where}" if listed
                       else f"{day}No shows listed yet")
            return
        # One alert per round, even when several dates or movies match at the same time.
        if time.time() - task.get("last_alert_ts", 0) < state["interval_sec"] * 0.8:
            return
        max_alerts = int(task.get("max_alerts", 5))
        count = int(task.get("alert_count", 0))
        if count >= max_alerts:
            task["status"] = "Done"
            save_tasks()
            return
        count += 1
        task["alert_count"] = count
        task["last_alert_ts"] = time.time()
        set_result(task, page, f"🔥 {day}{plural(len(matching), 'show')} open")
        if count >= max_alerts:
            task["status"] = "Done"
        save_tasks()
        snapshot = dict(task)

    owners = owners_of(snapshot)
    name = snapshot.get("user_label", "Watcher")
    where = task_place(snapshot)
    add_log(f"🔥 [{name}] {plural(len(matching), 'matching show')} open at {where}! Sending alert {count}/{max_alerts}…", owners)
    results = send_task_notification(snapshot, matching, where, count, max_alerts)
    add_log(f"   [{name}] {describe_results(results)}", owners)
    if count >= max_alerts:
        add_log(f"✅ [{name}] All {max_alerts} alerts sent, watcher finished. Re-arm it from the Watchers tab.", owners)


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
    if task.get("scope") != "city":
        url = task.get("cinema_url")
        return [("cinema", district.query_url(url, d), d) for d in dates] if url else []
    city = task.get("city")
    if not city:
        return []
    entry = district.peek(f"city:{city}")
    movies = district.matching_movies(entry["value"]["movies"], split_keywords(task.get("movie_filter"))) if entry else []
    jobs = []
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
        for kind, page, date in page_jobs(task, today):
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
        add_log(f"✅ {shorten(page_name(job), 60)} is loading again.", owners)


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
                set_result(task, page, "⚠️ Page didn't load. Retrying")
    if fails == 1:
        owners = {t.get("owner") for t in job["tasks"]}
        add_log(f"⚠️ Couldn't load {shorten(page_name(job), 60)}: {shorten(err, 120)}", owners)


def page_name(job):
    first = job["tasks"][0] if job.get("tasks") else {}
    if job["kind"] == "list":
        return f"the cinema list for {district.city_label(job.get('city'))}"
    if job["kind"] == "city":
        return f"{shorten(first.get('movie_filter'), 30)} in {district.city_label(job.get('city'))}"
    return task_place(first) if first else "a cinema page"


def housekeeping(today):
    """Retire watchers whose dates have passed, and remove finished ones a week later."""
    cutoff = str(to_date(today) - timedelta(days=KEEP_FINISHED_DAYS))
    with tasks_lock:
        changed = False
        for tid, task in list(state["tasks"].items()):
            last = task_dates(task)[-1]
            if last < cutoff:
                del state["tasks"][tid]
                changed = True
            elif last < today and task.get("status") in ("Active", "Paused"):
                task["status"] = "Expired"
                task["last_result"] = "Date passed"
                changed = True
                add_log(f"📅 [{task.get('user_label', 'Watcher')}] the date has passed, so this watcher stopped.",
                        owners_of(task))
        if changed:
            save_tasks()


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
