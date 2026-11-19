"""What the page's buttons and fields do.

`session` is a per-tab gr.State: {"owner": hash of this browser's key, "admin": bool,
"sent": hashes of what the live regions last showed}. It lives on the server, so a visitor
can't change it, and every handler checks it before showing or changing a watcher.
"""
import hashlib
import json
import re
import time
import uuid
from datetime import timedelta

import gradio as gr

from . import access, bms, district, engine, tracker
from .actions import new_token
from .names import name_for
from .alerts import describe_results, sample_shows, send_task_notification
from .config import (DEFAULT_CITY, DEFAULT_URL, MAX_CINEMA_DAYS, MAX_CINEMAS_PER_WATCHER, MAX_INTERVAL, MAX_RANGE_DAYS,
                     MAX_TRACKED_SHOWS, MIN_INTERVAL,
                     PREVIEW_CACHE, SEATS_MIN_DEFAULT, SEATS_MIN_MAX, TIME_SLOTS, TRACK_DAYS_AHEAD)
from .core import (add_log, date_label, dates_label, format_label, formats_label, json_list, log_lock, movie_list,
                   movie_text, now_ist,
                   parse_dates, plural, seats_min, seats_min_label, shorten, state, task_cinemas, task_dates,
                   task_formats, to_date, to_int, task_dates_label, tasks_lock, today_ist, wake_engine)
from .storage import save_tasks
from .views import (admin_status_html, callout, city_formats, engine_note_html, esc, hint, icon,
                    logs_html, movie_catalog, show_groups, status_html, trackers_html, watcher_title, watchers_html)

SLOT_CHOICES = [(label, key) for key, label, _, _ in TIME_SLOTS]
CITY_PREFIX = "city:"


def keep():
    """Leave a component as it is (and send nothing to the browser for it)."""
    return gr.update()


def toast(ok, text):
    """Pop-up message. Gradio renders toasts as HTML, so the text is escaped here."""
    (gr.Info if ok else gr.Warning)(esc(shorten(text, 240)))


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
    """(status, watchers, log, trackers) for this tab, as updates."""
    if force:
        session["sent"] = {}
    return (fresh(session, "status", status_html(session)), fresh(session, "watchers", watchers_html(session)),
            fresh(session, "log", logs_html(session)), fresh(session, "trackers", trackers_html(session)))


def poll_status(session):
    if not session or not session.get("owner"):
        return session, keep(), keep(), keep(), keep()
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
    # The name and ntfy topic this browser used last (or its own made-up name), for both forms.
    saved_name, saved_topic = access.saved_defaults(identity)
    name = saved_name or name_for(session["owner"])
    return ((session, forget_admin, today_ist(), gr.update(value=state["interval_sec"]),
             engine_note_html(session), engine_button(), admin_status_html(session))
            + admin_controls(session) + live_views(session, force=True) + (name, name, saved_topic, saved_topic))


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
    return (session, keep(), "", message, keep()) + admin_controls(session) + (keep(), keep(), keep(), keep())


def admin_logout(session):
    session = dict(session or {}, admin=False)
    return ((session, "-", admin_status_html(session, hint("Signed out of admin on this browser.")),
             engine_note_html(session)) + admin_controls(session) + live_views(session, force=True))


# ---------------------------------------------------------------------------
# Picking cinemas. The page script draws the picker (BookTick.cinemas in app.js); these send
# it the cinemas to pick from, and read what was picked: a JSON list of cinema links, or
# ["city:<city>"] for anywhere in the city.
# ---------------------------------------------------------------------------
def cinema_catalog(city, directory):
    """The cinema picker's list, as JSON: the city, and each cinema's link, name, area and movies."""
    cinemas = []
    for c in (directory or {}).get("cinemas") or []:
        name, _, area = district.cinema_label(c, city).partition(", ")
        cinemas.append({"u": c["url"], "n": name, "a": area, "m": c["movies"]})
    if directory:  # and BookMyShow's cinemas that District doesn't have, like Rohini or GK Cinemas
        for v in district.bms_cinemas(city, directory):
            name, _, area = v["name"].partition(":")
            area = ", ".join(p for p in (area.strip(), "BookMyShow") if p)
            cinemas.append({"u": v["url"], "n": name.strip(), "a": area, "m": [], "s": "bms"})
        cinemas.sort(key=lambda c: c["n"].lower())
    return json.dumps({"city": city, "label": district.city_label(city), "cinemas": cinemas,
                       "formats": city_formats(directory)}, separators=(",", ":"), ensure_ascii=False)


def on_city_change(city):
    """New city: its cinemas and movies (the picker clears the cinemas picked in the last one)."""
    try:
        directory = district.city_directory(city)
    except Exception as e:
        info = callout("err", f"Couldn't load cinemas in {esc(district.city_label(city))}",
                       f"{esc(str(e))}. You can paste a cinema link below instead.")
        return cinema_catalog(city, None), movie_catalog([]), info, gr.update(open=True)
    return cinema_catalog(city, directory), movie_catalog(directory["movies"]), "", keep()


def on_link_input(url, city):
    """A pasted cinema link: add it to the cinemas picked (the picker does that), then clear the box."""
    clean = district.clean_cinema_url(url)
    if not clean or not district.cinema_id_from_url(clean):
        return keep(), (hint("That doesn't look like a cinema's link from district.in or bookmyshow.com.")
                        if str(url or "").strip() else ""), keep()
    clean = clean.split("?")[0]
    twin = district.district_twin(clean, city) if bms.is_bms(clean) else None
    if twin:  # on District too, which BookTick reads more reliably
        name = district.short_cinema_name(twin["name"])
        add = json.dumps({"u": twin["url"], "n": twin["name"], "t": time.time()})
        return add, hint(f"<b>{esc(name)}</b> is on District too, so BookTick watches it there (more reliable). "
                         "Added it to your cinemas."), ""
    name = district.known_cinema_name(clean) or district.cinema_name_from_url(clean)
    add = json.dumps({"u": clean, "n": name, "t": time.time()})
    return add, hint(f"Added <b>{esc(name)}</b> to your cinemas."), ""


def load_directory(city):
    """On page load: the city list, that city's cinemas, and the movie picker."""
    city = city or DEFAULT_CITY
    cities = gr.update(choices=district.city_choices(), value=city)
    try:
        directory = district.city_directory(city)
    except Exception as e:
        info = callout("warn", "Couldn't load the cinema list right now", f"{esc(str(e))}. The link below still works.")
        return cities, cinema_catalog(city, None), movie_catalog([]), info
    return cities, cinema_catalog(city, directory), movie_catalog(directory["movies"]), ""


def watch_targets(cinema_value, cin_url=""):
    """Where to watch: (city or None, [cinema links], why not or None).

    `cinema_value` is the picker's JSON list; a link still in the paste box counts too. A city
    and cinemas together means anywhere in the city, with its own alert as each of those opens.
    Raises ValueError for a city BookTick doesn't know.
    """
    raw = str(cinema_value or "").strip()
    try:
        picks = json.loads(raw) if raw.startswith("[") else [raw]
    except ValueError:
        picks = []
    picks = [str(p) for p in picks if p] + ([cin_url] if district.clean_cinema_url(cin_url) else [])
    city = next((p[len(CITY_PREFIX):] for p in picks if p.startswith(CITY_PREFIX)), None)
    city = district.valid_city(city) if city else None
    links, seen = [], set()
    for p in picks:
        link = district.clean_cinema_url(p)
        twin = district.district_twin(link) if link and bms.is_bms(link) else None
        link = twin["url"] if twin else link  # a cinema on both is watched on District
        cid = district.cinema_id_from_url(link)
        if link and cid and cid not in seen:
            seen.add(cid)
            links.append(link.split("?")[0])
    if not links and not city:
        return None, [], "Pick a cinema (or anywhere in the city), or paste a cinema's link from district.in or bookmyshow.com."
    if len(links) > MAX_CINEMAS_PER_WATCHER:
        return None, [], f"A watcher can follow up to {MAX_CINEMAS_PER_WATCHER} cinemas. Remove a few, or pick anywhere in the city."
    return city, links, None


def picked_formats(value):
    """The format picker's JSON list (or a list), as the chips spell them; [] for any format."""
    return list(dict.fromkeys(format_label(f) for f in json_list(value, limit=8) if format_label(f)))


def cinema_name(link):
    """District's full name for a cinema (it's stored whole; pages show engine.cinema_label)."""
    return district.known_cinema_name(link) or district.cinema_name_from_url(link)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def page_shows(kind, url):
    """Shows on a page, reused for a few seconds so repeated previews don't refetch it."""
    return district.cached(f"page:{url}", PREVIEW_CACHE, lambda: district.fetch_shows(kind, url))


def preview_shows(cinema_value, cin_url, movie_names, formats, slots, date_val):
    movies = movie_text(movie_names)
    try:
        city, links, why = watch_targets(cinema_value, cin_url)
    except ValueError as e:
        return callout("err", "Which city?", esc(str(e)))
    if why:
        return callout("err", "Which cinema?", esc(why))
    dates = parse_dates(date_val)
    if not dates:
        return callout("err", "Pick show dates", f"Choose a day, or up to {MAX_RANGE_DAYS} days in a row.")
    date = dates[0]
    task = {"movie_filter": movies, "formats": picked_formats(formats), "time_slots": slots or []}
    range_note = (f" · BookTick watches all of {dates_label(*dates)}" if dates[1] != dates[0] else "")

    if city:
        return city_preview(city, task, date, range_note)  # the city's listings have your cinemas' shows too
    shows, places, failed = [], [], []
    for link in links:
        try:
            place, found = page_shows("cinema", district.query_url(link, date))
            places.append(place)
            shows += found
        except Exception:
            failed.append(cinema_name(link))
    if not places:
        return callout("err", "Couldn't load those cinemas", "District didn't answer. Try again in a moment.")
    note = callout("warn", "Some cinemas didn't load", esc(", ".join(failed))) if failed else ""
    if len(places) == 1:
        return note + preview_html(places[0], shows, task, date, range_note, by="title")
    return note + preview_html(f"Your {len(places)} cinemas", shows, task, date, range_note, by="cinema")


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
    editing = (session or {}).get("editing")
    if editing:  # the watcher being edited: its saved token and webhook stand behind "(saved)"
        with tasks_lock:
            task = dict(state["tasks"].get(editing) or {})
        if access.can_manage(session, task):
            tg_tok, disc_url = kept_secret(tg_tok, task, "telegram_token"), kept_secret(disc_url, task, "discord_webhook")
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return callout("err", "Can't send a test yet", error)
    wait = access.claim_test(channels, (session or {}).get("admin"))
    if wait:
        return callout("warn", "Hold on a moment", esc(wait))
    task = dict(channels, user_label=(user_name or "").strip() or "Test")
    try:
        city, links, _ = watch_targets(cinema_value, cin_url)
    except ValueError:
        city, links = None, []
    link = links[0] if links and not city else None
    place = (f"Cinemas in {district.city_label(city)}" if city
             else district.short_cinema_name(cinema_name(link)) if link else "Sample Cinema")
    results = send_task_notification(task, sample_shows(link, place), place, test=True)
    if all(ok for _, ok, _ in results):
        return callout("ok", "Test alert sent. Check your phone.", esc(describe_results(results)))
    return callout("warn", "Some alerts didn't go through", esc(describe_results(results)))


def read_watcher_form(session, cinema_value, cin_url, movie_names, formats, slots, date_val, max_alerts_val,
                      seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url):
    """The watcher form, checked: (settings, [movies], None), or (None, None, (title, body)) when
    something needs fixing. The settings are everything but the movie and the name."""
    movies = movie_list(movie_names)
    try:
        city, links, why = watch_targets(cinema_value, cin_url)
    except ValueError as e:
        return None, None, ("Which city?", esc(str(e)))
    if why:
        return None, None, ("Which cinema?", esc(why))
    if city and not movies:
        return None, None, ("Which movie?", "Pick the movie to watch across the city.")
    dates = parse_dates(date_val)
    if not dates:
        return None, None, ("Pick show dates", f"Choose a day, or up to {MAX_RANGE_DAYS} days in a row.")
    if dates[1] < today_ist():
        return None, None, ("Pick a date from today on", "That date has already passed.")
    first_day = max(dates[0], today_ist())
    if links and not session.get("admin"):
        days = (to_date(dates[1]) - to_date(first_day)).days + 1
        if len(links) * days > MAX_CINEMA_DAYS:
            return None, None, ("That's a lot to keep checking",
                                f"{plural(len(links), 'cinema')} × {plural(days, 'day')} is {len(links) * days} "
                                f"cinema-days, and a watcher can check up to {MAX_CINEMA_DAYS}. Pick fewer cinemas or days.")
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return None, None, ("Where should we alert you?", error)
    settings = dict(
        channels,
        scope="city" if city else "cinema",
        city=city,
        cinemas=[{"url": link, "name": cinema_name(link)} for link in links],
        target_date=first_day,
        target_date_to=dates[1],
        formats=picked_formats(formats),
        time_slots=[s for s in (slots or []) if s in dict(SLOT_CHOICES).values()],
        max_alerts=int(max_alerts_val or 5),
        seats_min=seats_min_choice(seats_min_val),
    )
    return settings, movies, None


def add_new_watcher(session, user_name, cinema_value, cin_url, movie_names, formats, slots, date_val, max_alerts_val,
                    seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url, request: gr.Request):
    """One watcher per movie picked, each following the same cinemas, formats and dates."""
    session = session or {}
    fail = lambda title, body: (callout("err", title, body), keep(), keep())  # noqa: E731
    if not session.get("owner"):
        return fail("Reload the page", "BookTick couldn't tell which watchers are yours. Reload and try again.")
    settings, movies, error = read_watcher_form(session, cinema_value, cin_url, movie_names, formats, slots, date_val,
                                                max_alerts_val, seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return fail(*error)
    network = access.network_id(request)
    limit = access.watcher_limit_error(session, network, count=max(1, len(movies)))
    if limit:
        return fail("Can't add another watcher yet" if len(movies) < 2 else "Can't add that many watchers",
                    esc(limit))

    name = shorten((user_name or "").strip() or name_for(session["owner"]), 40)
    city, cinemas = settings["city"], settings["cinemas"]
    now = time.time()
    created = []
    for i, movie in enumerate(movies or [""]):
        task = dict(
            settings,
            id="w_" + uuid.uuid4().hex[:10],
            owner=session["owner"],
            network=network,
            user_label=name,
            cinemas=[dict(c) for c in cinemas],
            movie_filter=movie,
            alert_count=0,
            status="Active",
            phase="waiting",  # waiting for bookings to open; then open, closed, open again…
            reminders_left=0,
            action_token=new_token(),  # lets the buttons in its alerts stop it or keep it going
            created_at=now_ist().strftime("%d-%b %H:%M"),
            created_ts=now + i / 1000,  # keeps them in the order picked
        )
        created.append(task)
    with tasks_lock:
        for task in created:
            state["tasks"][task["id"]] = task
        save_tasks()
    wake_engine.set()

    first = created[0]
    where_text, how = engine.task_place(first), formats_label(first)
    if city and cinemas:
        each = f"{cinemas[0]['name']} opens" if len(cinemas) == 1 else f"each of your {len(cinemas)} cinemas opens"
        where_text = f"any cinema in {district.city_label(city)}, with its own alert when {each}"
    for task in created:
        add_log(f"➕ [{name}] watching for {task['movie_filter'] or 'any movie'} ({how}) at {where_text}, "
                f"{task_dates_label(task)}.", (task["owner"],))
    see = f'<button type="button" class="bt-link" data-bt-goto="watchers">See {"it" if len(created) == 1 else "them"} in Watchers{icon("arrow")}</button>'
    if len(created) == 1:
        movie = first["movie_filter"] or "any movie"
        msg = callout("ok", f"{esc(name)}'s watcher is live",
                      f"Watching for <b>{esc(movie)}</b> ({esc(how)}) at <b>{esc(where_text)}</b>, "
                      f"<b>{esc(task_dates_label(first))}</b>. The first check runs now. {see}")
    else:
        titles = ", ".join(f"<b>{esc(t['movie_filter'])}</b>" for t in created)
        msg = callout("ok", f"{len(created)} watchers are live, one for each movie",
                      f"{titles}: each watches ({esc(how)}) at <b>{esc(where_text)}</b>, "
                      f"<b>{esc(task_dates_label(first))}</b>. Booking one stops only that one. {see}")
    session.setdefault("sent", {}).pop("watchers", None)
    session["sent"].pop("status", None)
    return msg, fresh(session, "watchers", watchers_html(session)), fresh(session, "status", status_html(session))


# ---------------------------------------------------------------------------
# Editing a watcher. Its card's Edit button loads it into the New watcher form (what the form
# held is put aside, and comes back afterwards); Save changes then updates it in place.
# ---------------------------------------------------------------------------
SAVED = "(saved)"  # the form's stand-in for a bot token or webhook kept from before: those never go back to the page
# What a watcher looks for. Changing any of these starts it over (as if re-armed).
FRESH_KEYS = ("page_seats", "told_cinemas", "told_quiet_until", "city_open", "seat_base", "seen_open", "closed_cinemas",
              "cinema_alert_ts", "open_places", "page_last_start", "expected_pages", "also_open_ts", "last_spread_ts",
              "last_new_ts", "last_more_ts", "settle_until")
# The form's fields, in the order app.py lists them (FORM_FIELDS there): what's put aside, and what's filled in.
FORM_KEYS = ("city", "cinema_value", "cin_url", "movie_value", "formats_value", "slots", "date", "max_alerts", "seats_min",
             "name", "ntfy", "tg_token", "tg_chat", "discord")
FORM_OUTPUTS = len(FORM_KEYS) + 3  # filling it in also sends the city's cinemas, its movies and a note


def kept_secret(value, task, key):
    """The bot token or webhook to save: the watcher's own when the form still shows the stand-in."""
    value = (value or "").strip()
    return (task or {}).get(key) or "" if value.startswith(SAVED) else value


def watcher_city(task, fallback):
    """The city to show a watcher's cinemas from: its own, or the one its first cinema is in."""
    if task.get("scope") == "city" and task.get("city"):
        return task["city"]
    cinemas = task_cinemas(task)
    return (district.city_of_cinema(cinemas[0]["url"]) if cinemas else None) or fallback


def watcher_form_values(task, fallback_city):
    """A watcher's settings as the form shows them (FORM_KEYS)."""
    city = watcher_city(task, fallback_city)
    picks = ([CITY_PREFIX + task["city"]] if task.get("scope") == "city" and task.get("city") else [])
    picks += [c["url"] for c in task_cinemas(task)]
    start, end = task_dates(task)[0], task_dates(task)[-1]
    start = max(start, today_ist())
    token, hook = task.get("telegram_token") or "", task.get("discord_webhook") or ""
    return {
        "city": city, "cinema_value": json.dumps(picks), "cin_url": "",
        "movie_value": json.dumps([task["movie_filter"]] if task.get("movie_filter") else []),
        "formats_value": json.dumps(task_formats(task)),
        "slots": [s for s in (task.get("time_slots") or []) if s in dict(SLOT_CHOICES).values()],
        "date": start if start >= end else f"{start}..{end}",
        "max_alerts": max(1, min(20, to_int(task.get("max_alerts")) or 5)),
        "seats_min": seats_min(task),
        "name": task.get("user_label") or "",
        "ntfy": task.get("ntfy_topic") or "", "tg_token": SAVED if token else "",
        "tg_chat": task.get("telegram_chat_id") or "", "discord": f"{SAVED} …{hook[-4:]}" if hook else "",
    }


def form_updates(values, current_city):
    """The form filled with `values` (FORM_KEYS), as app.py's FORM_FIELDS updates: the city's
    cinema and movie lists come too when it's another city."""
    if not values:
        values = {}
    city = values.get("city")
    if city and city != current_city:
        cinemas, movies, info, _ = on_city_change(city)
        head = (city, cinemas, movies, info)
    else:
        head = (keep(), keep(), keep(), keep())
    return head + tuple(values[k] if k in values else keep() for k in FORM_KEYS[1:])


def form_mode(session, task=None, go=False):
    """(edit info for the page script, banner, Save/Start button, Cancel button) for editing `task`, or not."""
    if task is None:
        return (json.dumps({"id": "", "go": False}), gr.update(value="", visible=False),
                gr.update(value="Start watching"), gr.update(visible=False))
    names = {district.cinema_id_from_url(c["url"]): engine.cinema_label(c) for c in task_cinemas(task)}
    whose = "" if task.get("owner") == session.get("owner") else f" ({task.get('user_label') or 'someone'}'s)"
    banner = callout("info", f"Editing the {esc(watcher_title(task))} watcher{esc(whose)}",
                     "Change anything below, then tap <b>Save changes</b>. Changing the movie, cinemas, formats, show "
                     "times or dates starts it over, so if bookings are already open you hear straight away.")
    return (json.dumps({"id": task["id"], "go": go, "names": names}), gr.update(value=banner, visible=True),
            gr.update(value="Save changes"), gr.update(visible=True))


def start_edit(task_id, session, *form):
    """The card's Edit button: load the watcher into the form. `form` is what the form holds now
    (FORM_KEYS), put aside until editing ends."""
    session = session if session is not None else {}
    with tasks_lock:
        task = state["tasks"].get(task_id)
        task = dict(task) if task else None
    if not access.can_manage(session, task) or tracker.is_tracker(task):
        toast(False, "That watcher no longer exists.")
        return (session, json.dumps({"id": "", "go": False})) + (keep(),) * (4 + FORM_OUTPUTS)
    if not session.get("editing"):
        session["form_before_edit"] = dict(zip(FORM_KEYS, form))
    session["editing"] = task_id
    values = watcher_form_values(task, form[0])
    return ((session,) + form_mode(session, dict(task, id=task_id), go=True) + ("",)
            + form_updates(values, form[0]))


def leave_edit(session, current_city):
    """Back to adding watchers, with the form as it was before editing."""
    session.pop("editing", None)
    before = session.pop("form_before_edit", None) or {"tg_token": "", "discord": ""}  # never leave a stand-in behind
    for key in ("tg_token", "discord"):
        if str(before.get(key) or "").startswith(SAVED):
            before[key] = ""
    return form_mode(session, None), form_updates(before, current_city)


def cancel_edit(session, current_city):
    session = session if session is not None else {}
    mode, form = leave_edit(session, current_city)
    return (session,) + mode + ("",) + form


CHANGE_LABELS = [
    ("movie", lambda t: str(t.get("movie_filter") or "").strip().lower()),
    ("cinemas", lambda t: (t.get("scope"), t.get("city") if t.get("scope") == "city" else None,
                         sorted(district.cinema_id_from_url(c["url"]) or c["url"] for c in task_cinemas(t)))),
    ("formats", lambda t: sorted(f.lower() for f in task_formats(t))),
    ("show times", lambda t: sorted(t.get("time_slots") or [])),
    ("dates", lambda t: (max(task_dates(t)[0], today_ist()), task_dates(t)[-1])),
    ("alerts when bookings open", lambda t: to_int(t.get("max_alerts"))),
    ("seats released alerts", lambda t: seats_min(t)),
    ("alert channels", lambda t: tuple(t.get(k) or "" for k in ("ntfy_topic", "telegram_token", "telegram_chat_id",
                                                                  "discord_webhook"))),
    ("name", lambda t: t.get("user_label") or ""),
]
WATCH_CHANGES = {"movie", "cinemas", "formats", "show times", "dates"}


def edit_watcher(task_id, session, user_name, cinema_value, cin_url, movie_names, formats, slots, date_val,
                 max_alerts_val, seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url, current_city, request):
    """Save changes: (result, watchers, status, session, edit info, banner, Save button, Cancel button, *form)."""
    stay = (json.dumps({"id": task_id, "go": False}),) + (keep(),) * (3 + FORM_OUTPUTS)  # still editing
    fail = lambda title, body: (callout("err", title, body), keep(), keep(), session) + stay  # noqa: E731
    with tasks_lock:
        task = state["tasks"].get(task_id)
        task = dict(task) if task else None
    if not access.can_manage(session, task) or tracker.is_tracker(task):
        return fail("That watcher is gone", "It may have been deleted. Tap <b>Cancel</b> to add a new one instead.")
    settings, movies, error = read_watcher_form(
        session, cinema_value, cin_url, movie_names, formats, slots, date_val, max_alerts_val, seats_min_val,
        ntfy_top, kept_secret(tg_tok, task, "telegram_token"), tg_chat, kept_secret(disc_url, task, "discord_webhook"))
    if error:
        return fail(*error)
    if len(movies) > 1:
        return fail("One movie per watcher", "This watcher follows one movie. To watch another as well, save this one, "
                                             "then add a new watcher for it.")
    names = {district.cinema_id_from_url(c["url"]): c.get("name") for c in task_cinemas(task)}
    for c in settings["cinemas"]:  # keep the names it had (a name from the URL is only a guess)
        c["name"] = names.get(district.cinema_id_from_url(c["url"])) or c["name"]
    label = shorten((user_name or "").strip() or task.get("user_label") or name_for(task.get("owner") or ""), 40)
    after = dict(task, **settings, movie_filter=movies[0] if movies else "", user_label=label)
    for key in ("cinema_url", "cinema_name", "format_filter"):  # how watchers from before kept these
        after.pop(key, None)
    changed = [what for what, read in CHANGE_LABELS if read(task) != read(after)]
    restart = bool(WATCH_CHANGES & set(changed))
    revive = restart and task.get("status") in ("Done", "Expired")
    if revive:
        limit = access.watcher_limit_error(session, task.get("network"))
        if limit:
            return fail("Can't start it again yet", esc(limit))

    with tasks_lock:
        live = state["tasks"].get(task_id)
        if live is None:
            return fail("That watcher is gone", "It was deleted while you were editing it.")
        for key in ("cinema_url", "cinema_name", "format_filter"):
            live.pop(key, None)
        live.update({k: after[k] for k in list(settings) + ["movie_filter", "user_label"]})
        if restart:
            for key in FRESH_KEYS:
                live.pop(key, None)
            live.update(phase="waiting", reminders_left=0, last_result="", last_checked="", last_checked_ts=None)
            if revive:
                live.update(status="Active", booked=False)
        if changed:
            save_tasks()
    if restart:
        wake_engine.set()

    title = watcher_title(after)
    if changed:
        add_log(f"✏️ [{label}] edited the {title} watcher: {', '.join(changed)}.", (task.get("owner"),))
    see = '<button type="button" class="bt-link" data-bt-goto="watchers">See it in Watchers' + icon("arrow") + "</button>"
    if not changed:
        msg = callout("info", "Nothing to save", f"The {esc(title)} watcher already had these settings. {see}")
    else:
        note = (" It starts over with these, so if bookings are already open you hear straight away."
                if restart and live.get("status") == "Active" else "")
        msg = callout("ok", f"Saved the {esc(title)} watcher", f"Changed: {esc(', '.join(changed))}.{note} {see}")
    mode, form = leave_edit(session, current_city)
    session.setdefault("sent", {}).pop("watchers", None)
    session["sent"].pop("status", None)
    return ((msg, fresh(session, "watchers", watchers_html(session)), fresh(session, "status", status_html(session)),
             session) + mode + form)


def save_watcher(session, user_name, cinema_value, cin_url, movie_names, formats, slots, date_val, max_alerts_val,
                 seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url, current_city, request: gr.Request):
    """The form's main button: Start watching, or Save changes while a watcher is being edited.
    Returns (result, watchers, status, session, edit info, banner, Save button, Cancel button, *form)."""
    session = session if session is not None else {}
    if session.get("editing"):
        return edit_watcher(session["editing"], session, user_name, cinema_value, cin_url, movie_names, formats, slots,
                            date_val, max_alerts_val, seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url, current_city,
                            request)
    result = add_new_watcher(session, user_name, cinema_value, cin_url, movie_names, formats, slots, date_val,
                             max_alerts_val, seats_min_val, ntfy_top, tg_tok, tg_chat, disc_url, request)
    return result + (session,) + (keep(),) * (4 + FORM_OUTPUTS)


# ---------------------------------------------------------------------------
# Buttons on the watcher cards, and the engine
# ---------------------------------------------------------------------------
def describe(task):
    """('the Ramayana watcher', 'watcher') or ('the IMAX shows tracker', 'tracker')."""
    if tracker.is_tracker(task):
        return f"the {tracker.tracker_title(task)} tracker", "tracker"
    return f"the {watcher_title(task)} watcher", "watcher"


def toggle_watcher(task):
    status = task.get("status")
    name, what = describe(task)
    if status not in ("Active", "Paused"):
        return False, ("This tracker has sent its report." if what == "tracker"
                       else "This watcher has finished. Re-arm it to start again.")
    task["status"] = "Paused" if status == "Active" else "Active"
    paused = task["status"] == "Paused"
    add_log(f"{'⏸' if paused else '▶'} [{task.get('user_label')}] {what} {'paused' if paused else 'resumed'}.",
            (task.get("owner"),))
    return True, f"{'Paused' if paused else 'Resumed'} {name}."


def rearm_watcher(task):
    if task.get("status") == "Expired":
        return False, "This watcher's dates have passed. Create a new one instead."
    task.update(alert_count=0, status="Active", phase="waiting", reminders_left=0, booked=False, last_result="",
                last_checked="", last_checked_ts=None, last_alert_ts=0)
    for key in FRESH_KEYS + ("alert_log",):
        task.pop(key, None)  # start fresh, so bookings that are open now alert again
    add_log(f"🔄 [{task.get('user_label')}] re-armed: alert count reset and watching again.", (task.get("owner"),))
    return True, f"Re-armed the {watcher_title(task)} watcher. It's watching again."


def seats_min_choice(value, fallback=SEATS_MIN_DEFAULT):
    """The "Seats released alerts" number, from the form's slider or a card: 0 (off) to SEATS_MIN_MAX."""
    n = to_int(value)
    return n if n is not None and 0 <= n <= SEATS_MIN_MAX else fallback


def set_seats_min(task, value):
    n = seats_min_choice(value, None)
    if n is None:
        return False, f"Pick 0 to {SEATS_MIN_MAX} seats."
    task["seats_min"] = n
    title = watcher_title(task)
    if n == 0:
        text = f"The {title} watcher won't send seats released alerts."
    else:
        come = "any seat comes" if n == 1 else f"{n} or more seats come"
        text = f"The {title} watcher sends a seats released alert when {come} back in a show."
    add_log(f"🎟️ [{task.get('user_label')}] seats released alerts: {seats_min_label(n).lower()}.", (task.get("owner"),))
    return True, text


def run_watcher_action(payload, session):
    """Handles the buttons inside the HTML cards (they send 'action:watcher_id'; a choice made
    on a card, 'action-value:watcher_id')."""
    session = session or {}
    action, _, task_id = str(payload or "").partition(":")
    action, _, value = action.partition("-")
    if action == "engine":
        if not session.get("admin"):
            toast(False, "Only the admin can pause checking for everyone.")
            return (session, keep(), keep(), keep(), keep(), keep())
        toast(True, flip_engine())
        session.get("sent", {}).clear()
        views = live_views(session)
        return (session,) + views[:2] + (engine_note_html(session), engine_button(), views[3])
    if action not in ("pause", "rearm", "delete", "test", "seats"):
        return (session, keep(), keep(), keep(), keep(), keep())

    with tasks_lock:
        task = state["tasks"].get(task_id)
        if not access.can_manage(session, task):
            ok, msg, snapshot = False, "That no longer exists.", None
        elif tracker.is_tracker(task) and action in ("rearm", "test", "seats"):
            ok, msg, snapshot = False, "Trackers can only be paused, resumed or deleted.", None
        elif action == "seats":
            ok, msg = set_seats_min(task, value)
            if ok:
                save_tasks()
            snapshot = None
        elif action == "delete":
            del state["tasks"][task_id]
            save_tasks()
            name, what = describe(task)
            add_log(f"🗑️ [{task.get('user_label')}] {what} deleted.", (task.get("owner"),))
            ok, msg, snapshot = True, f"Deleted {name}.", None
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
    sent = session.setdefault("sent", {})
    sent.pop("watchers", None)
    sent.pop("trackers", None)
    views = live_views(session)
    return (session,) + views[:2] + (keep(), keep(), views[3])


def send_test(task, session):
    wait = access.claim_test(task, session.get("admin"))
    if wait:
        return False, wait
    place = engine.task_place(task)
    cinemas = task_cinemas(task) if task.get("scope") != "city" else []
    results = send_task_notification(task, sample_shows(cinemas[0]["url"] if cinemas else None, place), place, test=True)
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


# ---------------------------------------------------------------------------
# Seat trackers
# ---------------------------------------------------------------------------
ALL_MOVIES = "All movies"


def tracker_days():
    """Today and the next few days, as (label, YYYY-MM-DD) choices."""
    today = now_ist().date()
    return [(date_label(str(today + timedelta(days=i))), str(today + timedelta(days=i))) for i in range(TRACK_DAYS_AHEAD)]


def tracker_cinemas(cinemas, city):
    return [(district.cinema_label(c, city), c["url"]) for c in cinemas]


def tracker_movies(cinema):
    return gr.update(choices=[ALL_MOVIES] + (list(cinema["movies"]) if cinema else []), value=ALL_MOVIES)


def load_tracker_form(city, cinema_value):
    """On page load: cities, the city's cinemas, the coming week, and the cinema's movies."""
    city = city or DEFAULT_CITY
    cities = gr.update(choices=district.city_choices(), value=city)
    days = gr.update(choices=tracker_days(), value=today_ist())
    try:
        cinemas = district.city_cinemas(city)
    except Exception:
        return cities, keep(), days, keep()
    cid = district.cinema_id_from_url(cinema_value or DEFAULT_URL)
    cinema = next((c for c in cinemas if cid and c["id"] == cid), None)
    return (cities, gr.update(choices=tracker_cinemas(cinemas, city), value=cinema["url"] if cinema else None),
            days, tracker_movies(cinema))


def tracker_city_change(city):
    try:
        cinemas = district.city_cinemas(city)
    except Exception:
        cinemas = []
    return gr.update(choices=tracker_cinemas(cinemas, city), value=None), tracker_movies(None)


def tracker_cinema_change(city, cinema_value):
    try:
        cinemas = district.city_cinemas(city)
    except Exception:
        cinemas = []
    return tracker_movies(next((c for c in cinemas if c["url"] == cinema_value), None))


def test_tracker_channels(session, user_name, cinema_value, ntfy_top, tg_tok, tg_chat, disc_url):
    return test_user_notification(session, user_name, None, cinema_value, ntfy_top, tg_tok, tg_chat, disc_url)


def tracker_plan(url, day, filters):
    """(cinema name, note on what it will follow, problem) from the cinema's page right now."""
    try:
        place, shows = page_shows("cinema", district.query_url(url, day))
    except Exception:
        return None, "Couldn't load the cinema's page just now, so it keeps trying.", None
    now = time.time()
    upcoming = [s for s in shows if engine.wanted(filters, s, day) and s["start"] and s["start"].timestamp() > now]
    if not upcoming:
        if day == today_ist():
            return place, None, "Every matching show today has started, or none match. Pick another day, format or movie."
        return place, "No matching shows are listed for that day yet. It starts as soon as they are.", None
    count = min(len(upcoming), MAX_TRACKED_SHOWS)
    first, last = upcoming[0]["start"], upcoming[count - 1]["start"]
    note = (f"It follows {plural(count, 'show')}, {first.strftime('%I:%M %p').lstrip('0')} to "
            f"{last.strftime('%I:%M %p').lstrip('0')}")
    if len(upcoming) > MAX_TRACKED_SHOWS:
        note += f" (the first {MAX_TRACKED_SHOWS} of {len(upcoming)}: pick a format or movie to narrow it down)"
    return place, note + ".", None


def add_tracker(session, user_name, cinema_value, day, fmt_val, movie, ntfy_top, tg_tok, tg_chat, disc_url,
                request: gr.Request):
    session = session or {}
    fail = lambda title, body: (callout("err", title, body), keep())  # noqa: E731
    if not session.get("owner"):
        return fail("Reload the page", "BookTick couldn't tell which trackers are yours. Reload and try again.")
    url = district.clean_cinema_url(cinema_value)
    if not url:
        return fail("Which cinema?", "Pick a cinema from the list.")
    if not district.is_district(url):
        return fail("Which cinema?", "Seat trackers follow District cinemas: BookMyShow doesn't say how many seats are left.")
    if day not in {d for _, d in tracker_days()}:
        return fail("Pick a day", f"Choose today or one of the next {TRACK_DAYS_AHEAD - 1} days.")
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return fail("Where should the report go?", error)
    limit = access.tracker_limit_error(session)
    if limit:
        return fail("Can't add another tracker yet", esc(limit))
    movie = "" if not movie or movie == ALL_MOVIES else movie_text(movie)
    fmt = (fmt_val or "ANY").strip().upper() or "ANY"
    filters = {"movie_filter": movie, "format_filter": fmt, "time_slots": []}
    place, note, problem = tracker_plan(url, day, filters)
    if problem:
        return fail("Nothing left to track that day", esc(problem))

    task_id = "t_" + uuid.uuid4().hex[:10]
    name = shorten((user_name or "").strip() or name_for(session["owner"]), 40)
    task = dict(
        channels, **filters,
        id=task_id, kind="tracker", owner=session["owner"], network=access.network_id(request), user_label=name,
        scope="cinema", city=None, cinema_url=url,
        cinema_name=place or district.known_cinema_name(url) or district.cinema_name_from_url(url),
        target_date=day, target_date_to=day, status="Active", report_token=tracker.new_token(),
        created_at=now_ist().strftime("%d-%b %H:%M"), created_ts=time.time(),
    )
    with tasks_lock:
        state["tasks"][task_id] = task
        save_tasks()
    wake_engine.set()
    title, where = tracker.tracker_title(task), tracker.place_of(task)
    add_log(f"📊 [{name}] tracking seats for {title} at {where}, {date_label(day)}.", (task["owner"],))
    msg = callout("ok", f"Tracking {esc(title)} at {esc(where)}",
                  f"{esc(date_label(day))}. {esc(note)} The report goes to your channels after the last show starts. "
                  "Its card is at the top of this tab.")
    session.setdefault("sent", {}).pop("trackers", None)
    return msg, fresh(session, "trackers", trackers_html(session))


def refresh_trackers(session):
    session = session or {}
    session.setdefault("sent", {}).pop("trackers", None)
    return session, fresh(session, "trackers", trackers_html(session))
