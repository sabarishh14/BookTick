"""Sending alerts: ntfy, Telegram and Discord."""
import html as html_lib
from datetime import timedelta
from urllib.parse import urlparse

import requests

from .actions import action_url
from .config import APP_NAME, BRAND_DISCORD_COLOR
from .core import fmt_price, fmt_when, now_ist, plural, redact, seats_text, shorten
from .district import query_url


def response_error(res):
    try:
        body = res.json()
        return body.get("description") or body.get("error") or body.get("message") or f"HTTP {res.status_code}"
    except ValueError:
        return f"HTTP {res.status_code}"


def show_lines(shows, limit=5):
    """One line per show; city-wide alerts also name the cinema."""
    many_cinemas = len({s.get("cinema") for s in shows}) > 1
    lines = []
    for s in shows[:limit]:
        where = f"{shorten(s.get('cinema'), 40)} · " if many_cinemas and s.get("cinema") else ""
        lines.append(f"• {fmt_when(s['start'])} · {where}{s['format']} · {seats_text(s)} · {fmt_price(s['price'])}")
    if len(shows) > limit:
        lines.append(f"…and {len(shows) - limit} more")
    return lines


def alert_links(show):
    """[(label, link), ...] for an alert, the one to open on tap first.

    The District app opens cinema pages but sends seat-selection links to its home screen,
    so the cinema page comes first. The seat link still goes straight to the seat map in a
    browser, for anyone without the app.
    """
    seats = show["url"]
    if not show.get("cinema_url"):
        return [("Open on District", seats)]
    date = show["start"].strftime("%Y-%m-%d") if show.get("start") else None
    return [("Open cinema", query_url(show["cinema_url"], date)), ("Seat map (web)", seats)]


# What each kind of alert says, and the buttons it has: (label, choice) for actions.apply.
BUTTONS = {
    "opened": [("Booked it, stop watching", "stop"), ("Got it, keep watching", "keep")],
    "reopened": [("Booked it, stop watching", "stop"), ("Got it, keep watching", "keep")],
    "reminder": [("Booked it, stop watching", "stop"), ("Got it, keep watching", "keep")],
    "more_seats": [("Booked it, stop watching", "stop")],
    "closed": [("Yes, booked. Stop watching", "stop"), ("Not yet, keep watching", "keep")],
}


def wording(kind, name, shows, count, max_alerts, reason):
    """(headline, intro) for an alert."""
    n = plural(len(shows), "matching show")
    if kind == "reopened":
        return f"🔁 Bookings reopened: {name}", f"They're back. {n} with seats right now."
    if kind == "reminder":
        return f"🚨 [{count}/{max_alerts}] Still open: {name}", f"{n} still have seats."
    if kind == "more_seats":
        return f"🎟️ More seats: {name}", "Seats just freed up in these shows:"
    if kind == "closed":
        why = ("Every matching show is sold out right now." if reason == "sold_out"
               else "The shows have been taken off sale for now.")
        return (f"⏸️ Bookings closed: {name}",
                f"{why} BookTick keeps watching and will tell you if they come back. Did you get tickets?")
    return f"🚨 Tickets open: {name}", f"{n} with seats right now."


def send_task_notification(task, shows, place, kind="opened", count=1, max_alerts=1, reason=None, date=None, test=False):
    """Send one alert to every channel on the task. Returns [(channel, ok, detail), ...].

    `place` is the cinema, or "Cinemas in <city>". `kind` is opened, reopened, reminder,
    more_seats or closed; a closed alert may have no shows (when they were taken off sale).
    """
    titles = list(dict.fromkeys(s["title"] for s in shows)) or [task.get("movie_filter") or "your movie"]
    name = " / ".join(titles)
    formats = list(dict.fromkeys(str(s["format"]) for s in shows))
    movie = f"{name} ({', '.join(formats)})" if formats else name
    if test:
        headline, intro = f"🧪 Test alert from {APP_NAME}", "Your alerts work. A real alert will look like this:"
    else:
        headline, intro = wording(kind, name, shows, count, max_alerts, reason)
    lines = show_lines(shows)
    if shows:
        links = alert_links(shows[0])
    elif task.get("cinema_url"):
        links = [("Open cinema", query_url(task["cinema_url"], date))]
    else:
        links = []
    open_link = links[0][1] if links else None
    buttons = [] if test else [(label, action_url(task, choice)) for label, choice in BUTTONS.get(kind, [])]
    buttons = [(label, url) for label, url in buttons if url]  # none without a public address
    page = None if test else action_url(task)
    loud = test or kind != "closed"
    results = []

    # 1. ntfy.sh: the buttons call BookTick straight from the notification.
    topic = (task.get("ntfy_topic") or "").strip()
    if topic:
        payload = {
            "topic": topic,
            "title": headline,
            "message": "\n".join([intro, f"🎬 {movie}", f"🏛️ {place}"] + ([""] + lines if lines else [])),
            "priority": 5 if loud else 3,
            "tags": ["test_tube"] if test else ["pause_button"] if kind == "closed" else ["rotating_light", "ticket"],
        }
        if open_link:
            payload["click"] = open_link  # tapping the notification opens the theatre page
        if buttons:
            payload["actions"] = [{"action": "http", "label": label, "url": url, "method": "POST", "clear": True}
                                  for label, url in buttons]
        try:
            res = requests.post("https://ntfy.sh", json=payload, timeout=10)
            results.append(("ntfy", res.ok, "sent" if res.ok else response_error(res)))
        except Exception as e:
            results.append(("ntfy", False, str(e)))

    # 2. Telegram: a button that opens BookTick's small "Did you get tickets?" page.
    tg_token = (task.get("telegram_token") or "").strip()
    tg_chat = (task.get("telegram_chat_id") or "").strip()
    if tg_token and tg_chat:
        esc = html_lib.escape
        link_line = " · ".join(f'<a href="{esc(link, quote=True)}"><b>{esc(label)}</b></a>' for label, link in links)
        text = "\n".join(
            [f"<b>{esc(headline)}</b>", "", esc(intro), f"🎬 <b>{esc(movie)}</b>", f"🏛️ {esc(place)}"]
            + ([""] + [esc(line) for line in lines] if lines else [])
            + (["", "👉 " + link_line] if link_line else [])
        )
        body = {"chat_id": tg_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if page and buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": "Booked it? Stop or keep watching", "url": page}]]}
        try:
            res = requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json=body, timeout=10)
            results.append(("Telegram", res.ok, "sent" if res.ok else response_error(res)))
        except Exception as e:
            results.append(("Telegram", False, redact(e, tg_token)))

    # 3. Discord: a link to the same page.
    discord_url = (task.get("discord_webhook") or "").strip()
    if discord_url:
        link_line = " · ".join(f"[{label}]({link})" for label, link in links)
        if page and buttons:
            link_line += f"{' · ' if link_line else ''}[Booked it? Stop or keep watching]({page})"
        embed = {
            "title": headline,
            "color": 0x6366F1 if test else BRAND_DISCORD_COLOR,
            "description": "\n".join([intro, f"**{movie}**", place] + ([""] + lines if lines else [])
                                     + (["", link_line] if link_line else [])),
            "footer": {"text": APP_NAME},
        }
        if open_link:
            embed["url"] = open_link
        content = "🧪 Test alert" if test else "@everyone 🔥 Tickets are open!" if loud else "Bookings closed"
        try:
            res = requests.post(discord_url, json={"content": content, "embeds": [embed]}, timeout=10)
            ok = res.status_code in (200, 204)
            results.append(("Discord", ok, "sent" if ok else response_error(res)))
        except Exception as e:
            # Error text can echo the webhook path, which contains its secret token.
            results.append(("Discord", False, redact(e, discord_url, urlparse(discord_url).path)))

    return results


def describe_results(results):
    if not results:
        return "no alert channels set"
    return " · ".join(f"✅ {ch}" if ok else f"❌ {ch}: {detail}" for ch, ok, detail in results)


def sample_shows(url, cinema="Sample Cinema"):
    """A made-up show used by test alerts."""
    start = (now_ist() + timedelta(hours=3)).replace(minute=30, second=0, microsecond=0)
    return [{
        "title": "Test Movie", "cinema": cinema, "format": "IMAX 3D", "start": start, "audi": "",
        "avail": 120, "total": 200, "price": 450.0,
        "url": url or "https://www.district.in",
    }]
