"""Sending alerts: ntfy, Telegram and Discord."""
import html as html_lib
from datetime import timedelta
from urllib.parse import urlparse

import requests

from .config import APP_NAME, BRAND_DISCORD_COLOR
from .core import fmt_price, fmt_when, now_ist, redact, seats_text, shorten
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


def send_task_notification(task, shows, place, alert_idx=1, max_alerts=1, test=False):
    """Send to every channel on the task. `place` is the cinema, or "Cinemas in <city>".

    Returns [(channel, ok, detail), ...].
    """
    first = shows[0]
    titles = list(dict.fromkeys(s["title"] for s in shows))
    formats = list(dict.fromkeys(str(s["format"]) for s in shows))
    movie = f"{' / '.join(titles)} ({', '.join(formats)})"
    counter = f"[{alert_idx}/{max_alerts}] " if max_alerts > 1 and not test else ""
    headline = f"🧪 Test alert from {APP_NAME}" if test else f"🚨 {counter}Tickets open: {' / '.join(titles)}"
    intro = "Your alerts work. A real alert will look like this:" if test else f"{len(shows)} matching show(s) have seats right now."
    lines = show_lines(shows)
    links = alert_links(first)
    open_link = links[0][1]
    results = []

    # 1. ntfy.sh
    topic = (task.get("ntfy_topic") or "").strip()
    if topic:
        try:
            res = requests.post(
                "https://ntfy.sh",
                json={
                    "topic": topic,
                    "title": headline,
                    "message": "\n".join([intro, f"🎬 {movie}", f"🏛️ {place}", ""] + lines),
                    "priority": 5,
                    "tags": ["test_tube"] if test else ["rotating_light", "ticket"],
                    "click": open_link,  # tapping the notification opens the theatre page
                },
                timeout=10,
            )
            results.append(("ntfy", res.ok, "sent" if res.ok else response_error(res)))
        except Exception as e:
            results.append(("ntfy", False, str(e)))

    # 2. Telegram
    tg_token = (task.get("telegram_token") or "").strip()
    tg_chat = (task.get("telegram_chat_id") or "").strip()
    if tg_token and tg_chat:
        esc = html_lib.escape
        text = "\n".join(
            [f"<b>{esc(headline)}</b>", "", esc(intro), f"🎬 <b>{esc(movie)}</b>", f"🏛️ {esc(place)}", ""]
            + [esc(line) for line in lines]
            + ["", "👉 " + " · ".join(f'<a href="{esc(link, quote=True)}"><b>{esc(label)}</b></a>' for label, link in links)]
        )
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            results.append(("Telegram", res.ok, "sent" if res.ok else response_error(res)))
        except Exception as e:
            results.append(("Telegram", False, redact(e, tg_token)))

    # 3. Discord
    discord_url = (task.get("discord_webhook") or "").strip()
    if discord_url:
        embed = {
            "title": headline,
            "url": open_link,
            "color": 0x6366F1 if test else BRAND_DISCORD_COLOR,
            "description": "\n".join([intro, f"**{movie}**", place, ""] + lines
                                     + ["", " · ".join(f"[{label}]({link})" for label, link in links)]),
            "footer": {"text": APP_NAME},
        }
        content = "🧪 Test alert" if test else "@everyone 🔥 Tickets are open!"
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
