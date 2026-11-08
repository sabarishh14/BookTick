"""HTML for the page. Anything a visitor typed (names, movies, links) is escaped here."""
import html as html_lib
import re

from . import access, district, engine
from .config import (ADMIN_KEY, APP_NAME, APP_TAGLINE, AUTHOR_NAME, DATABASE_URL, GITHUB_AVATAR, GITHUB_URL,
                     GITHUB_USER, MAX_RANGE_DAYS, TIME_SLOTS)
from .core import (ago, date_is_past, dates_label, fmt_clock, fmt_price, logs_for, plural, shorten, state, task_dates,
                   task_dates_label, tasks_lock, to_int)

esc = html_lib.escape

ICONS = {
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "play": '<path d="M6 3.5v17l14-8.5z"/>',
    "rotate": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "film": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/>',
    "pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "eye": '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "ticket": '<path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M13 5v2M13 17v2M13 11v2"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "arrow": '<path d="M5 12h14M12 5l7 7-7 7"/>',
    "seat": '<path d="M7 18v3M17 18v3"/><path d="M5 11V6a3 3 0 0 1 3-3h8a3 3 0 0 1 3 3v5"/><rect x="3" y="11" width="18" height="7" rx="2"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "city": '<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"/><path d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/><path d="M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2"/><path d="M10 6h4M10 10h4M10 14h4M10 18h4"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
}

GITHUB_MARK = (
    '<svg class="bt-gh-mark" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0c4.42 0 8 3.58 8 8a8.013 '
    "8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 "
    "0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-"
    "2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46"
    ".21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3"
    '.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
)


def icon(name, cls=""):
    return f'<svg class="bt-i{" " + cls if cls else ""}" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def logo_svg(uid, cls="bt-logo"):
    """The BookTick mark: a ticket with a tick. `uid` keeps gradient ids unique on the page.

    The xmlns is ignored inline, but without it the browser won't draw the SVG as an image
    (the favicon).
    """
    g = f"btg-{uid}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="0 0 48 48" aria-hidden="true">'
        f'<defs><linearGradient id="{g}" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#FF6B4A"/><stop offset="1" stop-color="#F43F7A"/></linearGradient></defs>'
        f'<rect width="48" height="48" rx="14" fill="url(#{g})"/>'
        '<path d="M13 14h22a3 3 0 0 1 3 3v3a4 4 0 0 0 0 8v3a3 3 0 0 1-3 3H13a3 3 0 0 1-3-3v-3a4 4 0 0 0 0-8v-3a3 3 0 0 1 3-3Z" fill="#fff"/>'
        f'<path d="M18 24.5l4 4 8-8.5" fill="none" stroke="url(#{g})" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>'
        "</svg>"
    )


def safe_href(url):
    """Only plain web links go into href attributes."""
    url = str(url or "").strip()
    return esc(url, quote=True) if re.match(r"^https?://", url, re.I) else None


def callout(tone, title, body=""):
    """A coloured message box. `title` and `body` are HTML, so escape user text first."""
    icon_name = {"ok": "check", "err": "alert", "warn": "alert", "info": "info", "hot": "zap"}[tone]
    body_html = f'<div class="bt-callout-body">{body}</div>' if body else ""
    return (f'<div class="bt-callout {tone}" role="status">{icon(icon_name)}'
            f'<div><div class="bt-callout-title">{title}</div>{body_html}</div></div>')


def hint(text):
    return f'<div class="bt-hint-line">{icon("info")}<span>{text}</span></div>'


def step_head(badge, title, sub):
    return (f'<div class="bt-step-head"><span class="bt-step-num">{badge}</span>'
            f'<div><h3>{title}</h3><p>{sub}</p></div></div>')


def watcher_title(task):
    return task.get("movie_filter") or "Any movie"


# --- Loading placeholders -----------------------------------------------------
# Shown until the page-load calls fill in the real content, so the boxes never sit there
# with stale numbers from when the server started.
def skel(width, cls=""):
    return f'<span class="bt-skel{" " + cls if cls else ""}" style="width:{width}"></span>'


def status_skeleton():
    tile = (f'<div class="bt-stat">{skel("40px", "bt-skel-icon")}<div class="bt-stat-body">'
            f'{skel("44%", "bt-skel-label")}{skel("58%", "bt-skel-value")}{skel("76%", "bt-skel-sub")}</div></div>')
    return f'<div class="bt-stats bt-loading" aria-busy="true" aria-label="Loading">{tile * 4}</div>'


def watchers_skeleton():
    card = (
        '<div class="bt-card bt-loading">'
        f'<div class="bt-card-head">{skel("84px", "bt-skel-pill")}{skel("64px")}</div>'
        f'<div>{skel("72%", "bt-skel-title")}{skel("54%")}</div>'
        f'<div class="bt-tags">{skel("74px", "bt-skel-tag")}{skel("96px", "bt-skel-tag")}</div>'
        f'{skel("100%", "bt-skel-block")}'
        f'<div class="bt-actions">{skel("auto", "bt-skel-btn bt-skel-grow")}{skel("72px", "bt-skel-btn")}{skel("80px", "bt-skel-btn")}</div>'
        "</div>"
    )
    chips = "".join(skel(w, "bt-skel-chip") for w in ("64px", "100px", "88px", "92px"))
    return (f'<div class="bt-watchers" aria-busy="true" aria-label="Loading watchers">'
            f'<div class="bt-toolbar"><div class="bt-chips">{chips}</div></div>'
            f'<div class="bt-grid">{card * 3}</div></div>')


def cinema_skeleton():
    return (f'<div class="bt-cinema bt-loading" aria-busy="true" aria-label="Loading cinemas">'
            f'{skel("16px", "bt-skel-dot")}{skel("150px")}{skel("110px", "bt-skel-end")}</div>')


def logs_skeleton():
    rows = "".join(f'<li class="bt-log-row">{skel("96px")}{skel(w)}</li>' for w in ("62%", "48%", "70%", "40%", "56%", "66%"))
    return f'<ol class="bt-log-list bt-loading" aria-busy="true" aria-label="Loading activity">{rows}</ol>'


# --- Show date ----------------------------------------------------------------
def date_picker_html(value):
    """The show-date field: a button that opens a month calendar (BookTick.date in app.js).

    The script draws the days and writes the pick into a hidden textbox (#bt-date), which
    is what the Python handlers read.
    """
    weekdays = "".join(f"<span>{d}</span>" for d in ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"))
    return f"""
<div class="bt-date">
  <span class="bt-field-label" id="bt-date-label">Show date</span>
  <span class="bt-field-info">One day, or up to {MAX_RANGE_DAYS} in a row. Times are IST.</span>
  <button type="button" class="bt-date-trigger" aria-haspopup="dialog" aria-expanded="false"
          aria-labelledby="bt-date-label bt-date-value">
    {icon("calendar")}<span class="bt-date-value" id="bt-date-value">{esc(dates_label(value))}</span>{icon("chevron-down")}
  </button>
  <div class="bt-cal" role="dialog" aria-label="Choose show dates" hidden>
    <div class="bt-cal-mode" role="group" aria-label="How many days">
      <button type="button" data-cal-mode="day" aria-pressed="true">One day</button>
      <button type="button" data-cal-mode="range" aria-pressed="false">A few days</button>
    </div>
    <div class="bt-cal-head">
      <button type="button" class="bt-cal-nav" data-cal-nav="-1" aria-label="Previous month">{icon("chevron-left")}</button>
      <b class="bt-cal-title" aria-live="polite"></b>
      <button type="button" class="bt-cal-nav" data-cal-nav="1" aria-label="Next month">{icon("chevron-right")}</button>
    </div>
    <div class="bt-cal-week" aria-hidden="true">{weekdays}</div>
    <div class="bt-cal-days"></div>
    <p class="bt-cal-hint" aria-live="polite"></p>
    <div class="bt-cal-quick"></div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------
def header_html():
    return f"""
<header class="bt-hero">
  <div class="bt-brand">
    {logo_svg("hero")}
    <div>
      <div class="bt-wordmark">Book<span>Tick</span></div>
      <p class="bt-tagline">{APP_TAGLINE}</p>
    </div>
  </div>
  <div class="bt-hero-side">
    <button type="button" class="bt-theme" data-bt-theme aria-label="Switch between light and dark mode" title="Light / dark mode">
      {icon("moon", "bt-i-moon")}{icon("sun", "bt-i-sun")}
    </button>
    <a class="bt-gh" href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer" title="{AUTHOR_NAME} on GitHub">
      <img src="{GITHUB_AVATAR}" alt="" width="36" height="36">
      <span class="bt-gh-text"><small>Made by</small><b>{AUTHOR_NAME}</b></span>
      {GITHUB_MARK}
    </a>
  </div>
</header>"""


def footer_html():
    return f"""
<div class="bt-footer">
  <div class="bt-footer-brand">{logo_svg("foot", "bt-logo-sm")}<b>{APP_NAME}</b><span>Movie ticket alerts for District</span></div>
  <div class="bt-footer-meta">
    <a href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer">
      <img src="{GITHUB_AVATAR}" alt="" width="22" height="22">Built by {AUTHOR_NAME}</a>
    <a href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer">{GITHUB_MARK}@{GITHUB_USER}</a>
  </div>
  <p class="bt-footer-note">{APP_NAME} is an independent project and isn't affiliated with District or Zomato. All times are IST.</p>
</div>"""


RESTART_TIP = (
    "Watchers are saved, so a restart doesn't lose them. But checking stops while this Space sleeps, "
    "which happens after a long time without visitors. If you're waiting days, check back now and then."
    if DATABASE_URL else
    "Watchers are wiped when this Space restarts or goes to sleep after a long time without visitors. "
    "If you're waiting days, check back now and then."
)

HELP_HTML = f"""
<aside class="bt-help">
  <h3>How it works</h3>
  <ol class="bt-steps">
    <li><b>Pick your city and cinema</b><span>Every cinema on <a href="https://www.district.in" target="_blank" rel="noopener noreferrer">District</a> is listed. Pick <em>Any cinema</em> to hear the moment your movie opens anywhere in the city.</span></li>
    <li><b>Pick the movie, format, time and dates</b><span>Bookings not open yet? Type the movie's name. Tap <em>Preview shows</em> to see what's listed right now.</span></li>
    <li><b>Choose where to get alerts</b><span>ntfy is quickest: install the app, subscribe to a topic, and enter the same topic here.</span></li>
    <li><b>Send a test, then start watching</b><span>When a matching show has seats, you get the alerts you chose, each with a link straight to seat selection. Then the watcher stops; re-arm it from <em>Watchers</em>.</span></li>
  </ol>
  <h4>Good to know</h4>
  <ul class="bt-tips">
    <li>{icon("user")}<span>Your watchers are private to this browser. To manage them on another device, use <em>Copy my link</em> on the Watchers tab and open it there.</span></li>
    <li>{icon("lock")}<span>Use a topic name nobody would guess, like <code>arun_tickets_982</code>. Anyone who knows it can read your alerts.</span></li>
    <li>{icon("alert")}<span>{RESTART_TIP}</span></li>
    <li>{icon("clock")}<span>All times are Indian Standard Time.</span></li>
  </ul>
</aside>"""


def about_html():
    return f"""
<div class="bt-about">
  {logo_svg("about", "bt-logo-md")}
  <h3>About {APP_NAME}</h3>
  <p>{APP_NAME} keeps an eye on District cinema pages and pings your phone the moment seats open for the show you want.</p>
  <a class="bt-gh-card" href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer">
    <img src="{GITHUB_AVATAR}" alt="" width="44" height="44">
    <span><b>{AUTHOR_NAME}</b><small>github.com/{GITHUB_USER}</small></span>
    {GITHUB_MARK}
  </a>
</div>"""


def status_html(session):
    session = session or {}
    admin = bool(session.get("admin"))
    tasks = [t for _, t in access.visible_tasks(session)]
    watching = sum(1 for t in tasks if t.get("status") == "Active")
    alerts = sum(to_int(t.get("alert_count")) or 0 for t in tasks)
    running = not state["paused"]
    blocked_for = engine.health()["blocked_for"]
    last = state["last_check_time"]
    if last:
        last_value, last_sub = fmt_clock(last), f"IST · {ago(last.timestamp())}"
    else:
        last_value, last_sub = "—", "No checks yet"

    if running and blocked_for:
        engine_value = '<span class="bt-live off"></span>Waiting'
        engine_sub = f"District asked us to slow down · back in {max(1, blocked_for // 60)} min"
    else:
        engine_value = (f'<span class="bt-live {"on" if running else "off"}"></span>'
                        f'{"Running" if running else "Paused"}')
        engine_sub = f"Every {state['interval_sec']}s · {plural(state['check_count'], 'check')}"
    toggle = ""
    if admin:
        toggle = (f'<button type="button" class="bt-mini" data-bt-action="engine" '
                  f'title="{"Pause" if running else "Resume"} checking for every watcher">'
                  f'{icon("pause" if running else "play")}{"Pause" if running else "Resume"}</button>')
    scope = "in total" if admin else "of yours"
    tiles = [
        ("activity", "brand", "Engine", engine_value, engine_sub, toggle),
        ("eye", "ok", "Watching", str(watching), f"{plural(len(tasks), 'watcher')} {scope}", ""),
        ("bell", "hot", "Alerts sent", str(alerts), "Across all watchers" if admin else "Across your watchers", ""),
        ("clock", "info", "Last check", last_value, last_sub, ""),
    ]
    body = "".join(
        f'<div class="bt-stat"><div class="bt-stat-icon tone-{tone}">{icon(ic)}</div>'
        f'<div class="bt-stat-body"><div class="bt-stat-label">{label}</div>'
        f'<div class="bt-stat-value">{value}</div><div class="bt-stat-sub">{sub}</div></div>{extra}</div>'
        for ic, tone, label, value, sub, extra in tiles
    )
    return f'<div class="bt-stats">{body}</div>'


# --- Watcher cards ------------------------------------------------------------
STATUS_META = {
    "Active": ("Watching", "active", "Active"),
    "Paused": ("Paused", "paused", "Paused"),
    "Done": ("Alerted", "done", "finished"),
    "Expired": ("Date passed", "expired", "finished"),
}
FILTERS = [("all", "All"), ("Active", "Watching"), ("Paused", "Paused"), ("finished", "Finished")]
EMPTY_FILTER_TEXT = {
    "Active": "No watcher is checking right now.",
    "Paused": "No paused watchers.",
    "finished": "No watcher has finished yet.",
}


def action_button(action, task_id, label, icon_name, kind="", title=None, confirm=None):
    attrs = f'data-bt-action="{action}" data-bt-id="{esc(task_id)}" title="{esc(title or label)}"'
    if confirm:
        attrs += (f' data-bt-confirm="{esc(confirm)}" data-bt-confirm-title="Delete this watcher?"'
                  f' data-bt-confirm-ok="Delete"')
    return f'<button type="button" class="bt-btn {kind}" {attrs}>{icon(icon_name)}<span>{label}</span></button>'


def channel_names(task):
    return [n for n, key in (("ntfy", "ntfy_topic"), ("Telegram", "telegram_chat_id"), ("Discord", "discord_webhook"))
            if task.get(key)]


def channel_chips(task):
    names = channel_names(task)
    if not names:
        return '<span class="bt-chan none">No alert channel</span>'
    return "".join(f'<span class="bt-chan">{n}</span>' for n in names)


def slots_label(slots):
    names = [label for key, label, _, _ in TIME_SLOTS if key in (slots or [])]
    return ", ".join(names) if names and len(names) < len(TIME_SLOTS) else ""


def place_html(task):
    if task.get("scope") == "city":
        return f'{icon("city")}<span>Any cinema in {esc(district.city_label(task.get("city")))}</span>'
    cinema = esc(shorten(task.get("cinema_name") or district.cinema_name_from_url(task.get("cinema_url", "")), 52))
    href = safe_href(task.get("cinema_url"))
    inner = f'<a href="{href}" target="_blank" rel="noopener noreferrer">{cinema}{icon("external")}</a>' if href else cinema
    return f'{icon("pin")}{inner}'


def watcher_card(task_id, t, session):
    status = t.get("status", "")
    label, tone, group = STATUS_META.get(status, (status or "Unknown", "paused", "finished"))
    movie = esc(watcher_title(t))
    owner = esc(t.get("user_label") or "Watcher")
    if session.get("admin") and t.get("owner") != session.get("owner"):
        owner += " · not yours"
    fmt = str(t.get("format_filter") or "ANY")
    tags = [f'<span class="bt-tag">{icon("film")}{"Any format" if fmt == "ANY" else esc(fmt)}</span>',
            f'<span class="bt-tag">{icon("calendar")}{esc(task_dates_label(t))}</span>']
    slots = slots_label(t.get("time_slots"))
    if slots:
        tags.append(f'<span class="bt-tag">{icon("clock")}{esc(slots)}</span>')
    if status != "Expired" and date_is_past(task_dates(t)[-1]):
        tags.append('<span class="bt-tag warn">Date has passed</span>')

    sent = to_int(t.get("alert_count")) or 0
    max_alerts = to_int(t.get("max_alerts")) or 5
    pct = min(100, round(100 * sent / max_alerts)) if max_alerts else 0

    if status == "Expired":
        last_html = (f'<div class="bt-last">{icon("calendar")}<div><b>Its dates have passed</b>'
                     f'<small>It will be removed automatically after a week.</small></div></div>')
    elif t.get("last_checked"):
        result = str(t.get("last_result") or "Checked")
        last_tone = "hot" if result.startswith("🔥") else "warn" if result.startswith("⚠") else ""
        last_icon = {"hot": "zap", "warn": "alert"}.get(last_tone, "clock")
        result = result.lstrip("🔥⚠️ ").strip() or "Checked"
        rel = ago(t.get("last_checked_ts"))
        when = f"Checked {esc(t['last_checked'])} IST" + (f" · {rel}" if rel else "")
        last_html = f'<div class="bt-last {last_tone}">{icon(last_icon)}<div><b>{esc(result)}</b><small>{when}</small></div></div>'
    else:
        waiting = "Waiting for the first check…" if status == "Active" else "Not checked yet"
        last_html = f'<div class="bt-last">{icon("clock")}<div><b>{waiting}</b><small>Added {esc(t.get("created_at", ""))}</small></div></div>'

    actions = []
    if status == "Active":
        actions.append(action_button("pause", task_id, "Pause", "pause", "primary", "Stop checking this watcher for now"))
    elif status == "Paused":
        actions.append(action_button("pause", task_id, "Resume", "play", "primary", "Start checking again"))
    elif status == "Done":
        actions.append(action_button("rearm", task_id, "Re-arm", "rotate", "primary", "Reset the alert count and watch again"))
    if status in ("Active", "Paused") and sent > 0:
        actions.append(action_button("rearm", task_id, "Re-arm", "rotate", "", "Reset the alert count and watch again"))
    if status != "Expired":
        actions.append(action_button("test", task_id, "Test", "send", "", "Send a test alert to this watcher's channels"))
    actions.append(action_button(
        "delete", task_id, "Delete", "trash", "danger", "Delete this watcher",
        confirm=f"BookTick stops watching for “{watcher_title(t)}” ({t.get('user_label') or 'Watcher'}). This can't be undone.",
    ))

    pulse = '<span class="bt-pulse"></span>' if status == "Active" else ""
    return f"""
<article class="bt-card" data-status="{esc(status)}" data-group="{group}">
  <div class="bt-card-head">
    <span class="bt-badge s-{tone}">{pulse}{label}</span>
    <span class="bt-owner">{icon("user")}{owner}</span>
  </div>
  <div>
    <h3 class="bt-card-title">{movie}</h3>
    <div class="bt-card-cinema">{place_html(t)}</div>
  </div>
  <div class="bt-tags">{"".join(tags)}</div>
  <div class="bt-progress">
    <div class="bt-progress-top"><span>Alerts sent</span><b>{sent} / {max_alerts}</b></div>
    <div class="bt-bar"><i style="width:{pct}%"></i></div>
  </div>
  {last_html}
  <div class="bt-chans">{icon("bell")}{channel_chips(t)}</div>
  <div class="bt-actions">{"".join(actions)}</div>
</article>"""


DEVICE_LINK = (f'<button type="button" class="bt-btn" data-bt-device-link '
               f'title="A link that opens your watchers on another device. Anyone with it can manage them, so keep it to yourself.">'
               f'{icon("link")}<span>Copy my link</span></button>')


def watchers_html(session):
    session = session or {}
    items = access.visible_tasks(session)
    if not items:
        return f"""
<div class="bt-empty">
  {logo_svg("empty", "bt-logo-lg")}
  <h3>No watchers yet</h3>
  <p>Set one up and {APP_NAME} will keep checking District for you, day and night.</p>
  <button type="button" class="bt-btn primary" data-bt-goto="new">{icon("plus")}<span>Create a watcher</span></button>
  <small class="bt-empty-note">Made watchers on another device? Open your watcher link from there.</small>
</div>"""

    groups = {k: STATUS_META.get(t.get("status"), ("", "", "finished"))[2] for k, t in items}
    counts = {k: sum(1 for g in groups.values() if g == k) for k in ("Active", "Paused", "finished")}
    counts["all"] = len(items)
    chips = "".join(
        f'<button type="button" class="bt-chip" data-bt-filter="{key}">{label}<b>{counts[key]}</b></button>'
        for key, label in FILTERS
    )
    banner = ""
    if state["paused"]:
        resume = (f'<button type="button" class="bt-link" data-bt-action="engine">{icon("play")}Resume now</button>'
                  if session.get("admin") else "")
        banner = callout("warn", "Checking is paused for everyone", f"No watcher is checked until it resumes. {resume}")
    elif engine.health()["blocked_for"]:
        banner = callout("warn", "District asked us to slow down",
                         "Checks resume in a few minutes. Your watchers are safe and will pick up where they left off.")
    cards = "".join(watcher_card(tid, t, session) for tid, t in sorted(items, key=lambda i: i[1].get("created_ts", 0), reverse=True))
    empties = "".join(f'<div class="bt-filter-empty" data-for="{k}">{text}</div>'
                      for k, text in EMPTY_FILTER_TEXT.items() if counts[k] == 0)
    return f"""
<div class="bt-watchers">
  <div class="bt-toolbar">
    <div class="bt-chips" role="group" aria-label="Filter watchers">{chips}</div>
    <div class="bt-toolbar-actions">
      {DEVICE_LINK}
      <button type="button" class="bt-btn primary" data-bt-goto="new">{icon("plus")}<span>New watcher</span></button>
    </div>
  </div>
  {banner}
  <div class="bt-grid">{cards}</div>
  {empties}
</div>"""


# --- Activity log -------------------------------------------------------------
LOG_TONES = (("🔥", "hot"), ("⚠", "warn"), ("❌", "warn"), ("Engine error", "warn"), ("✅", "ok"),
             ("➕", "info"), ("🔄", "info"), ("🧪", "info"), ("▶", "info"), ("🗄", "info"), ("⏸", "muted"),
             ("🗑", "muted"), ("📅", "muted"))


def logs_html(session):
    session = session or {}
    entries = logs_for(session.get("owner"), session.get("admin"))[:300]
    if not entries:
        return f'<div class="bt-log-empty">{icon("activity")}<span>Nothing yet. Your checks, alerts and changes show up here.</span></div>'
    rows = []
    for e in entries:
        msg = e["msg"]
        tone = "sub" if msg.startswith("   ") else next((t for key, t in LOG_TONES if key in msg), "")
        line = "[" + e["t"] + "] " + msg
        rows.append(f'<li class="bt-log-row {tone}" data-line="{esc(line)}">'
                    f'<time>{esc(e["t"])}</time><span>{esc(msg.strip())}</span></li>')
    return f'<ol class="bt-log-list">{"".join(rows)}</ol>'


# --- Show preview -------------------------------------------------------------
def show_chip(show, matched):
    href = safe_href(show["url"])
    avail, total = show["avail"], show["total"]
    sold_out = avail == 0
    if sold_out:
        seats, pct = "Sold out", 0
    elif avail is None:
        seats, pct = "Seats open", None
    elif total:
        seats, pct = f"{avail} / {total} left", round(100 * avail / total)
    else:
        seats, pct = f"{avail} left", None
    meta = " · ".join(esc(str(x)) for x in (show["format"], show["audi"]) if x)
    bar = ""
    if pct is not None:
        bar = f'<span class="bt-seatbar{" low" if pct < 20 else ""}"><i style="width:{max(pct, 3)}%"></i></span>'
    price = f"from {fmt_price(show['price'])}" if show["price"] else ""
    book = f'<span class="bt-book">Book {icon("arrow")}</span>' if href and not sold_out else ""
    classes = "bt-show" + (" match" if matched else "") + (" soldout" if sold_out else "")
    inner = (f'<span class="bt-show-time">{fmt_clock(show["start"]) if show["start"] else "Time unknown"}</span>'
             f'<span class="bt-show-meta">{meta}</span>'
             f'<span class="bt-show-seats">{icon("seat")}{seats}</span>{bar}'
             f'<span class="bt-show-foot"><span>{price}</span>{book}</span>')
    if href:
        return f'<a class="{classes}" href="{href}" target="_blank" rel="noopener noreferrer">{inner}</a>'
    return f'<div class="{classes}">{inner}</div>'


def show_groups(shows, matched_ids, tag="", by="title", limit=None):
    """Shows grouped by movie (one cinema) or by cinema (a whole city)."""
    groups = {}
    for s in shows:
        groups.setdefault(s.get(by) or "Other", []).append(s)
    out = []
    for title, group in list(groups.items())[:limit]:
        chips = "".join(show_chip(s, id(s) in matched_ids) for s in group)
        out.append(f'<section class="bt-movie"><header><h4>{esc(title)}</h4>{tag}'
                   f'<span class="bt-count">{plural(len(group), "show")}</span></header>'
                   f'<div class="bt-shows">{chips}</div></section>')
    if limit is not None and len(groups) > limit:
        out.append(f'<p class="bt-more">…and {plural(len(groups) - limit, "more cinema")}.</p>')
    return "".join(out)


def cinema_card(cinema):
    """One line under the cinema picker: how many movies it's showing, and a link to it on District."""
    if not cinema:
        return ""
    href = safe_href(cinema["url"])
    link = (f'<a class="bt-cinema-link" href="{href}" target="_blank" rel="noopener noreferrer">'
            f'Open on District{icon("external")}</a>') if href else ""
    count = len(cinema["movies"])
    showing = f"<b>{plural(count, 'movie')}</b> showing now" if count else "Nothing showing right now"
    return f'<div class="bt-cinema">{icon("film")}<span>{showing}</span>{link}</div>'


def city_card(city, directory):
    """Under the cinema picker when "Any cinema" is picked."""
    cinemas = len(directory["cinemas"]) if directory else 0
    return (f'<div class="bt-cinema">{icon("city")}<span>Watching <b>{plural(cinemas, "cinema")}</b> in '
            f'{esc(district.city_label(city))} for your movie</span></div>')


# --- Settings -----------------------------------------------------------------
def engine_note_html(session):
    session = session or {}
    blocked = engine.health()["blocked_for"]
    if state["paused"]:
        note = callout("warn", "Paused for everyone", "No watcher is being checked until checking resumes.")
    elif blocked:
        note = callout("warn", "Waiting on District",
                       f"District turned requests away, so checking resumes in about {max(1, blocked // 60)} min.")
    else:
        note = callout("ok", "Running", f"Every watched page is checked every {state['interval_sec']} seconds.")
    if not session.get("admin"):
        return note
    h = engine.health()
    with tasks_lock:
        tasks = list(state["tasks"].values())
    people = len({t.get("owner") for t in tasks if t.get("owner")})
    rows = [("Watchers", f"{sum(1 for t in tasks if t.get('status') == 'Active')} watching · {len(tasks)} in total"),
            ("People", str(people)),
            ("District pages", f"{h['pages']} watched · {h['inflight']} loading · {h['failing']} failing"),
            ("Checks", f"{state['check_count']} since the last restart")]
    grid = "".join(f"<div><dt>{k}</dt><dd>{esc(v)}</dd></div>" for k, v in rows)
    return note + f'<dl class="bt-health">{grid}</dl>'


def admin_status_html(session, message=""):
    session = session or {}
    if not ADMIN_KEY:
        return hint("Admin controls are off. Add an <code>ADMIN_KEY</code> secret to the Space to turn them on.")
    if session.get("admin"):
        return callout("ok", "You're the admin here", message or "You can pause checking, change the interval, and see every watcher.")
    return message or hint("Enter the admin key to pause checking, change the interval and see every watcher.")
