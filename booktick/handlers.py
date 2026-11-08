"""What the page's buttons and fields do.

`session` is a per-tab gr.State: {"owner": hash of this browser's key, "admin": bool,
"sent": hashes of what the live regions last showed}. It lives on the server, so a visitor
can't change it, and every handler checks it before showing or changing a watcher.
"""
import hashlib
import re
import time
import uuid

import gradio as gr

from . import access, district, engine
from .alerts import describe_results, sample_shows, send_task_notification
from .config import DEFAULT_CITY, MAX_INTERVAL, MAX_RANGE_DAYS, MIN_INTERVAL, PREVIEW_CACHE, TIME_SLOTS
from .core import (add_log, dates_label, log_lock, now_ist, parse_dates, plural, shorten, state, task_dates_label,
                   tasks_lock, today_ist, wake_engine)
from .storage import save_tasks
from .views import (admin_status_html, callout, cinema_card, city_card, engine_note_html, esc, hint, icon,
                    logs_html, show_groups, status_html, watcher_title, watchers_html)

SLOT_CHOICES = [(label, key) for key, label, _, _ in TIME_SLOTS]
CITY_PREFIX = "city:"


def keep():
    """Leave a component as it is (and send nothing to the browser for it)."""
    return gr.update()


def toast(ok, text):
    """Pop-up message. Gradio renders toasts as HTML, so the text is escaped here."""
    (gr.Info if ok else gr.Warning)(esc(shorten(text, 240)))


def movie_text(value):
    """The movie picker returns a list; watchers store the names comma-separated."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def clean_channels(ntfy, tg_token, tg_chat, discord):
    """Validate alert settings. Returns (channels, error)."""
    ntfy = re.sub(r"^(https?://)?ntfy\.sh/", "", (ntfy or "").strip()).strip("/")
    tg_token, tg_chat, discord = (tg_token or "").strip(), (tg_chat or "").strip(), (discord or "").strip()
    if ntfy and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ntfy):
        return None, "ntfy topic names can only use letters, numbers, <code>-</code> and <code>_</code>."
    if bool(tg_token) != bool(tg_chat):
        return None, "Telegram needs both the bot token and your chat ID."
    if discord and not discord.startswith("https://"):
        return None, "The Discord webhook URL should start with <code>https://</code>."
    if not (ntfy or tg_token or discord):
        return None, "Add at least one way to alert you: ntfy, Telegram or Discord."
    return {"ntfy_topic": ntfy, "telegram_token": tg_token, "telegram_chat_id": tg_chat, "discord_webhook": discord}, None


# ---------------------------------------------------------------------------
# Live regions: send HTML only when it changed since this tab last got it
# ---------------------------------------------------------------------------
def fresh(session, region, html):
    digest = hashlib.blake2b(html.encode("utf-8"), digest_size=12).hexdigest()
    sent = session.setdefault("sent", {})
    if sent.get(region) == digest:
        return keep()
    sent[region] = digest
    return html


def live_views(session, force=False):
    """(status, watchers, log) for this tab, as updates."""
    if force:
        session["sent"] = {}
    return (fresh(session, "status", status_html(session)), fresh(session, "watchers", watchers_html(session)),
            fresh(session, "log", logs_html(session)))


def poll_status(session):
    if not session or not session.get("owner"):
        return session, keep(), keep(), keep()
    return (session,) + live_views(session)


# ---------------------------------------------------------------------------
# Page load and admin
# ---------------------------------------------------------------------------
def admin_controls(session):
    """Visibility of the admin-only pieces, in the order app.py lists them."""
    admin = bool(session.get("admin"))
    return (gr.update(visible=admin),                                   # engine controls
            gr.update(visible=not admin and bool(access.ADMIN_KEY)),    # unlock form
            gr.update(visible=admin),                                   # sign-out button
            gr.update(visible=admin))                                   # clear log button


def engine_button(session=None):
    if state["paused"]:
        return gr.update(value="Resume checking", variant="primary")
    return gr.update(value="Pause all checking", variant="secondary")


def on_page_load(identity):
    session = access.session_from_identity(identity)
    if not session["owner"]:  # the page script didn't run: watchers last for this tab only
        session["owner"] = access.digest(uuid.uuid4().hex, "owner")
    forget_admin = "-" if session.pop("admin_rejected", False) else keep()
    return ((session, forget_admin, today_ist(), gr.update(value=state["interval_sec"]),
             engine_note_html(session), engine_button(), admin_status_html(session))
            + admin_controls(session) + live_views(session, force=True))


def admin_login(key, session):
    session = dict(session or {})
    if access.check_admin(key):
        session["admin"] = True
        add_log("🔑 An admin signed in.", owners=())
        return ((session, key.strip(), "", admin_status_html(session), engine_note_html(session))
                + admin_controls(session) + live_views(session, force=True))
    time.sleep(1)  # slows down guessing
    message = (callout("err", "That key didn't work", "Check the <code>ADMIN_KEY</code> secret on the Space.")
               if access.ADMIN_KEY else admin_status_html(session))
    return (session, keep(), "", message, keep()) + admin_controls(session) + (keep(), keep(), keep())


def admin_logout(session):
    session = dict(session or {}, admin=False)
    return ((session, "-", admin_status_html(session, hint("Signed out of admin on this browser.")),
             engine_note_html(session)) + admin_controls(session) + live_views(session, force=True))


# ---------------------------------------------------------------------------
# Picking a cinema
# ---------------------------------------------------------------------------
def cinema_options(cinemas, city):
    any_cinema = (f"Any cinema in {district.city_label(city)}", CITY_PREFIX + city)
    return [any_cinema] + [(district.cinema_label(c, city), c["url"]) for c in cinemas]


def movie_update(cinemas, cinema, picked):
    """New movie suggestions that keep the movies already picked.

    Gradio 4.44 remembers multiselect picks by their position in the list, so the picks
    are sent again with the new list. Otherwise they would shift onto other movies.
    """
    picked = [m for m in (picked or []) if str(m).strip()]
    return gr.update(choices=district.movie_choices(cinemas, cinema), value=picked)


def on_city_change(city, picked):
    """New city: list its cinemas. The cinema has to be picked again."""
    try:
        cinemas = district.city_cinemas(city)
    except Exception as e:
        info = callout("err", f"Couldn't load cinemas in {esc(district.city_label(city))}",
                       f"{esc(str(e))}. You can paste a cinema link below instead.")
        return gr.update(choices=[], value=None), keep(), info, gr.update(open=True), ""
    info = hint(f"{plural(len(cinemas), 'cinema')} in {esc(district.city_label(city))}. "
                "Pick one, or <b>Any cinema</b> to watch the whole city.")
    return gr.update(choices=cinema_options(cinemas, city), value=None), movie_update(cinemas, None, picked), info, keep(), ""


def on_cinema_change(city, value, picked):
    """Cinema picked: use its link and put its movies first. "Any cinema" watches the whole city."""
    if not value:
        return "", keep(), ""
    try:
        directory = district.city_directory(city)
    except Exception:
        directory = {"cinemas": [], "movies": []}
    if str(value).startswith(CITY_PREFIX):
        return "", movie_update(directory["cinemas"], None, picked), city_card(city, directory)
    cinema = next((c for c in directory["cinemas"] if c["url"] == value), None)
    return value, movie_update(directory["cinemas"], cinema, picked), cinema_card(cinema)


def on_link_input(url, city, picked):
    """A pasted link: select that cinema in the list when it's in the chosen city."""
    cid = district.cinema_id_from_url(url)
    try:
        cinemas = district.city_cinemas(city)
    except Exception:
        cinemas = []
    cinema = next((c for c in cinemas if cid and c["id"] == cid), None)
    if cinema:
        return gr.update(value=cinema["url"]), movie_update(cinemas, cinema, picked), cinema_card(cinema)
    clean = district.clean_cinema_url(url)
    info = hint(f"Using the pasted link: <b>{esc(district.cinema_name_from_url(clean))}</b>") if clean else ""
    return gr.update(value=None), keep(), info


def load_directory(city, url, picked):
    """On page load: fill the city list, that city's cinemas, and the movie suggestions."""
    city = city or DEFAULT_CITY
    cities = gr.update(choices=district.city_choices(), value=city)
    try:
        cinemas = district.city_cinemas(city)
    except Exception as e:
        info = callout("warn", "Couldn't load the cinema list right now", f"{esc(str(e))}. The link below still works.")
        return cities, keep(), keep(), info
    cid = district.cinema_id_from_url(url)
    cinema = next((c for c in cinemas if cid and c["id"] == cid), None)
    return (cities, gr.update(choices=cinema_options(cinemas, city), value=cinema["url"] if cinema else None),
            movie_update(cinemas, cinema, picked), cinema_card(cinema))


def watch_target(cinema_value, cin_url):
    """Where to watch: ("cinema", url), ("city", city), or (None, error message)."""
    url = district.clean_cinema_url(cin_url)
    if url:
        return "cinema", url
    if str(cinema_value or "").startswith(CITY_PREFIX):
        return "city", district.valid_city(str(cinema_value)[len(CITY_PREFIX):])
    return None, "Pick a cinema (or Any cinema) from the list, or paste a cinema's link from district.in."


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def page_shows(kind, url):
    """Shows on a page, reused for a few seconds so repeated previews don't refetch it."""
    return district.cached(f"page:{url}", PREVIEW_CACHE, lambda: district.fetch_shows(kind, url))


def preview_shows(cinema_value, cin_url, movie_names, fmt_val, slots, date_val):
    movie_names = movie_text(movie_names)
    try:
        scope, where = watch_target(cinema_value, cin_url)
    except ValueError as e:
        return callout("err", "Which city?", esc(str(e)))
    if not scope:
        return callout("err", "Which cinema?", where)
    dates = parse_dates(date_val)
    if not dates:
        return callout("err", "Pick show dates", f"Choose a day, or up to {MAX_RANGE_DAYS} days in a row.")
    date = dates[0]
    task = {"movie_filter": movie_names, "format_filter": fmt_val, "time_slots": slots or []}
    range_note = (f" · BookTick watches all of {dates_label(*dates)}" if dates[1] != dates[0] else "")

    if scope == "city":
        return city_preview(where, task, date, range_note)
    try:
        place, shows = page_shows("cinema", district.query_url(where, date))
    except Exception as e:
        return callout("err", "Couldn't load that page", esc(str(e)))
    return preview_html(place, shows, task, date, range_note, by="title")


def city_preview(city, task, date, range_note):
    keywords = engine.split_keywords(task["movie_filter"])
    if not keywords:
        return callout("err", "Which movie?", "Type the movie to watch across the city. Part of its name is enough.")
    try:
        directory = district.city_directory(city)
    except Exception as e:
        return callout("err", f"Couldn't load cinemas in {esc(district.city_label(city))}", esc(str(e)))
    movies = district.matching_movies(directory["movies"], keywords)
    place = f"Cinemas in {district.city_label(city)}"
    if not movies:
        title = (f'<div><div class="bt-eyebrow">{icon("city")}Whole city</div><h3>{esc(place)}</h3>'
                 f'<p>{esc(dates_label(date))}{esc(range_note)}</p></div>')
        note = callout("info", f"{esc(task['movie_filter'])} isn't showing in {esc(district.city_label(city))} yet",
                       "That's normal before bookings open. A watcher will notice the moment any cinema lists it.")
        return f'<div class="bt-preview"><div class="bt-preview-head">{title}</div>{note}</div>'
    shows = []
    for movie in movies[:3]:
        try:
            shows += page_shows("city", district.city_movie_url(movie["url"], city, date))[1]
        except Exception as e:
            return callout("err", "Couldn't load District's listings", esc(str(e)))
    return preview_html(place, shows, task, date, range_note, by="cinema", eyebrow=("city", "Whole city"))


def preview_html(place, shows, task, date, range_note, by, eyebrow=("pin", "Showtimes")):
    listed = [s for s in shows if engine.on_date(s, date)]
    title = (f'<div><div class="bt-eyebrow">{icon(eyebrow[0])}{eyebrow[1]}</div><h3>{esc(place)}</h3>'
             f'<p>{esc(dates_label(date))} · {plural(len(listed), "show")} listed{esc(range_note)}</p></div>')
    if not listed:
        note = callout("info", f"No shows are listed for {esc(dates_label(date))} yet",
                       "That's normal before bookings open. A watcher will keep checking.")
        return f'<div class="bt-preview"><div class="bt-preview-head">{title}</div>{note}</div>'

    matched = [s for s in listed if engine.wanted(task, s, date)]
    matched_ids = {id(s) for s in matched}
    bookable = [s for s in matched if engine.seats_open(s)]
    others = [s for s in listed if id(s) not in matched_ids]

    head = (f'<div class="bt-preview-head">{title}<div class="bt-preview-stats">'
            f'<span><b>{len(matched)}</b>match</span><span><b>{len(bookable)}</b>with seats</span></div></div>')
    if bookable:
        note = callout("hot", "Tickets are already open for this", "A watcher would alert you on its very first check.")
    elif matched:
        note = callout("warn", "Every matching show is sold out right now", "A watcher will alert you if seats free up.")
    else:
        movies = "".join(f'<span class="bt-tag">{esc(t)}</span>' for t in sorted({s["title"] for s in listed}))
        formats = "".join(f'<span class="bt-tag">{esc(str(f))}</span>' for f in sorted({str(s["format"]) for s in listed}))
        note = callout("info", "Nothing matches your filters yet",
                       f'<div class="bt-kv"><b>Movies</b><div class="bt-tags">{movies}</div></div>'
                       f'<div class="bt-kv"><b>Formats</b><div class="bt-tags">{formats}</div></div>')

    limit = 8 if by == "cinema" else None
    body = ""
    if matched:
        body += show_groups(matched, matched_ids, '<span class="bt-pill">Matches your filters</span>', by=by, limit=limit)
    if others:
        label = "Other shows" if by == "cinema" else "Other shows at this cinema"
        body += (f'<details class="bt-others"{"" if matched else " open"}><summary>'
                 f'{label} <span class="bt-count">{len(others)}</span></summary>'
                 f'{show_groups(others, matched_ids, by=by, limit=limit)}</details>')
    return f'<div class="bt-preview">{head}{note}{body}</div>'


# ---------------------------------------------------------------------------
# Test alerts and new watchers
# ---------------------------------------------------------------------------
def test_user_notification(session, user_name, cinema_value, cin_url, ntfy_top, tg_tok, tg_chat, disc_url):
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return callout("err", "Can't send a test yet", error)
    wait = access.claim_test(channels, (session or {}).get("admin"))
    if wait:
        return callout("warn", "Hold on a moment", esc(wait))
    task = dict(channels, user_label=(user_name or "").strip() or "Test")
    url = district.clean_cinema_url(cin_url)
    if url:
        place = district.cinema_name_from_url(url)
    elif str(cinema_value or "").startswith(CITY_PREFIX):
        place = f"Cinemas in {district.city_label(str(cinema_value)[len(CITY_PREFIX):])}"
    else:
        place = "Sample Cinema"
    results = send_task_notification(task, sample_shows(url, place), place, test=True)
    if all(ok for _, ok, _ in results):
        return callout("ok", "Test alert sent. Check your phone.", esc(describe_results(results)))
    return callout("warn", "Some alerts didn't go through", esc(describe_results(results)))


def add_new_watcher(session, user_name, cinema_value, cin_url, movie_names, fmt_val, slots, date_val, max_alerts_val,
                    ntfy_top, tg_tok, tg_chat, disc_url, request: gr.Request):
    session = session or {}
    fail = lambda title, body: (callout("err", title, body), keep(), keep())  # noqa: E731
    if not session.get("owner"):
        return fail("Reload the page", "BookTick couldn't tell which watchers are yours. Reload and try again.")
    movie_names = movie_text(movie_names)
    try:
        scope, where = watch_target(cinema_value, cin_url)
    except ValueError as e:
        return fail("Which city?", esc(str(e)))
    if not scope:
        return fail("Which cinema?", where)
    if scope == "city" and not movie_names:
        return fail("Which movie?", "Pick or type the movie to watch across the city.")
    dates = parse_dates(date_val)
    if not dates:
        return fail("Pick show dates", f"Choose a day, or up to {MAX_RANGE_DAYS} days in a row.")
    if dates[1] < today_ist():
        return fail("Pick a date from today on", "That date has already passed.")
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return fail("Where should we alert you?", error)
    network = access.network_id(request)
    limit = access.watcher_limit_error(session, network)
    if limit:
        return fail("Can't add another watcher yet", esc(limit))

    task_id = "w_" + uuid.uuid4().hex[:10]
    name = shorten((user_name or "").strip() or "Me", 40)
    cinema_name = (district.known_cinema_name(where) or district.cinema_name_from_url(where)) if scope == "cinema" else None
    task = dict(
        channels,
        id=task_id,
        owner=session["owner"],
        network=network,
        user_label=name,
        scope=scope,
        city=where if scope == "city" else None,
        cinema_url=where if scope == "cinema" else None,
        cinema_name=cinema_name,
        target_date=max(dates[0], today_ist()),
        target_date_to=dates[1],
        movie_filter=(movie_names or "").strip(),
        format_filter=(fmt_val or "ANY").strip().upper(),
        time_slots=[s for s in (slots or []) if s in dict(SLOT_CHOICES).values()],
        max_alerts=int(max_alerts_val or 5),
        alert_count=0,
        status="Active",
        created_at=now_ist().strftime("%d-%b %H:%M"),
        created_ts=time.time(),
    )
    with tasks_lock:
        state["tasks"][task_id] = task
        save_tasks()
    wake_engine.set()

    movie = task["movie_filter"] or "any movie"
    where_text = engine.task_place(task)
    add_log(f"➕ [{name}] watching for {movie} ({task['format_filter']}) at {where_text}, {task_dates_label(task)}.",
            (task["owner"],))
    msg = callout(
        "ok", f"{esc(name)}'s watcher is live",
        f"Watching for <b>{esc(movie)}</b> ({esc(task['format_filter'])}) at <b>{esc(where_text)}</b>, "
        f"<b>{esc(task_dates_label(task))}</b>. The first check runs now. "
        f'<button type="button" class="bt-link" data-bt-goto="watchers">See it in Watchers{icon("arrow")}</button>',
    )
    session.setdefault("sent", {}).pop("watchers", None)
    session["sent"].pop("status", None)
    return msg, fresh(session, "watchers", watchers_html(session)), fresh(session, "status", status_html(session))


# ---------------------------------------------------------------------------
# Buttons on the watcher cards, and the engine
# ---------------------------------------------------------------------------
def toggle_watcher(task):
    status = task.get("status")
    if status not in ("Active", "Paused"):
        return False, "This watcher has finished. Re-arm it to start again."
    task["status"] = "Paused" if status == "Active" else "Active"
    paused = task["status"] == "Paused"
    add_log(f"{'⏸' if paused else '▶'} [{task.get('user_label')}] {'paused' if paused else 'resumed'}.", (task.get("owner"),))
    return True, f"{'Paused' if paused else 'Resumed'} the {watcher_title(task)} watcher."


def rearm_watcher(task):
    if task.get("status") == "Expired":
        return False, "This watcher's dates have passed. Create a new one instead."
    task.update(alert_count=0, status="Active", last_result="", last_checked="", last_checked_ts=None, last_alert_ts=0)
    add_log(f"🔄 [{task.get('user_label')}] re-armed: alert count reset and watching again.", (task.get("owner"),))
    return True, f"Re-armed the {watcher_title(task)} watcher. It's watching again."


def run_watcher_action(payload, session):
    """Handles the buttons inside the HTML cards (they send 'action:watcher_id')."""
    session = session or {}
    action, _, task_id = str(payload or "").partition(":")
    if action == "engine":
        if not session.get("admin"):
            toast(False, "Only the admin can pause checking for everyone.")
            return (session, keep(), keep(), keep(), keep())
        toast(True, flip_engine())
        session.get("sent", {}).clear()
        return (session,) + live_views(session)[:2] + (engine_note_html(session), engine_button())
    if action not in ("pause", "rearm", "delete", "test"):
        return (session, keep(), keep(), keep(), keep())

    with tasks_lock:
        task = state["tasks"].get(task_id)
        if not access.can_manage(session, task):
            ok, msg, snapshot = False, "That watcher no longer exists.", None
        elif action == "delete":
            del state["tasks"][task_id]
            save_tasks()
            add_log(f"🗑️ [{task.get('user_label')}] watcher deleted.", (task.get("owner"),))
            ok, msg, snapshot = True, f"Deleted the {watcher_title(task)} watcher.", None
        elif action == "test":
            ok, msg, snapshot = True, "", dict(task)
        else:
            ok, msg = (toggle_watcher if action == "pause" else rearm_watcher)(task)
            if ok:
                save_tasks()
            snapshot = None
    if action in ("pause", "rearm") and ok:
        wake_engine.set()
    if action == "test" and snapshot:
        ok, msg = send_test(snapshot, session)
    toast(ok, msg)
    session.get("sent", {}).pop("watchers", None)
    return (session,) + live_views(session)[:2] + (keep(), keep())


def send_test(task, session):
    wait = access.claim_test(task, session.get("admin"))
    if wait:
        return False, wait
    place = engine.task_place(task)
    results = send_task_notification(task, sample_shows(task.get("cinema_url"), place), place, test=True)
    summary = describe_results(results)
    add_log(f"🧪 [{task.get('user_label')}] test alert: {summary}", (task.get("owner"),))
    if results and all(ok for _, ok, _ in results):
        return True, f"Test alert sent: {summary}"
    return False, f"Some test alerts failed: {summary}"


def flip_engine():
    state["paused"] = not state["paused"]
    if state["paused"]:
        add_log("⏸ Checking paused for everyone by the admin.")
        return "Paused. No watcher is being checked until you resume."
    add_log("▶ Checking resumed.")
    engine.reset_page_timers()
    return "Running. Checks resumed."


def toggle_engine(session):
    session = session or {}
    if not session.get("admin"):
        toast(False, "Only the admin can pause checking for everyone.")
        return session, keep(), keep(), keep(), keep()
    toast(True, flip_engine())
    session.get("sent", {}).clear()
    return (session, engine_note_html(session), engine_button()) + live_views(session)[:2]


def change_interval(val, session):
    session = session or {}
    if not session.get("admin"):
        return engine_note_html(session), gr.update(value=state["interval_sec"]), keep()
    state["interval_sec"] = max(MIN_INTERVAL, min(MAX_INTERVAL, int(val)))
    engine.reset_page_timers()
    add_log(f"⏱️ Check interval set to {state['interval_sec']}s.", owners=())
    toast(True, f"Checking every {state['interval_sec']} seconds.")
    session.get("sent", {}).pop("status", None)
    return engine_note_html(session), keep(), fresh(session, "status", status_html(session))


def clear_logs(session):
    """Admins only: the log is shared, so clearing it clears it for everyone."""
    session = session or {}
    if session.get("admin"):
        with log_lock:
            state["logs"].clear()
    return logs_html(session)


def refresh_watchers(session):
    session = session or {}
    session.setdefault("sent", {}).pop("watchers", None)
    return session, fresh(session, "watchers", watchers_html(session))


def refresh_logs(session):
    session = session or {}
    session.setdefault("sent", {}).pop("log", None)
    return session, fresh(session, "log", logs_html(session))
