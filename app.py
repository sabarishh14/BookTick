import os
import re
import json
import uuid
import threading
import urllib.request
import html as html_lib
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import gradio as gr

# ---------------------------------------------------------------------------
# Time: everything is shown in Indian Standard Time
# ---------------------------------------------------------------------------
# Hugging Face servers don't run on Indian time, so datetime.now() shows the
# server's clock. India has no daylight saving, so a fixed +5:30 offset is exact.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


def today_ist():
    return now_ist().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# ZeroGPU compatibility
# ---------------------------------------------------------------------------
# This app never needs a GPU. A ZeroGPU Space refuses to start unless at least one
# function is decorated with @spaces.GPU, so we decorate a no-op that nothing calls.
# Never put the decorator on a real button handler: every click spends GPU quota.
try:
    import spaces

    @spaces.GPU
    def _zerogpu_placeholder():
        pass
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Storage and shared state
# ---------------------------------------------------------------------------
TASKS_FILE = "district_tasks.json"
DEFAULT_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"
FORMAT_CHOICES = ["ANY", "IMAX", "4DX", "3D", "2D"]
MAX_LOG_LINES = 300

# RLock so helpers that take the lock can safely call each other.
tasks_lock = threading.RLock()
log_lock = threading.Lock()
wake_engine = threading.Event()  # set it to make the engine check right away


def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


app_state = {
    "tasks": load_tasks(),
    "paused": False,
    "interval_sec": 5,
    "check_count": 0,
    "last_check_time": None,
    "logs": [],
}


def save_tasks():
    """Write all watchers to disk. Call while holding tasks_lock."""
    tmp = TASKS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(app_state["tasks"], f, indent=2)
        os.replace(tmp, TASKS_FILE)
    except Exception as e:
        print(f"Could not save watchers: {e}", flush=True)


def add_log(msg):
    entry = f"[{now_ist().strftime('%d %b %H:%M:%S')}] {msg}"
    with log_lock:
        app_state["logs"].append(entry)
        del app_state["logs"][:-MAX_LOG_LINES]
    print(entry, flush=True)  # also shows up in the Space's container logs


def redact(text, *secrets):
    """Hide tokens before text reaches the public activity log."""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "•••")
    return text


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value, naive_is_utc):
    """Parse an ISO time string into an IST datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if naive_is_utc else IST)
    return dt.astimezone(IST)


def fmt_when(dt):
    if not dt:
        return "Time unknown"
    return dt.strftime("%a %d %b · ") + dt.strftime("%I:%M %p").lstrip("0")


def fmt_seats(show):
    if show["avail"] is None:
        return "Open"
    if show["total"]:
        return f"{show['avail']} / {show['total']}"
    return str(show["avail"])


def seats_text(show):
    if show["avail"] is None:
        return "seats open"
    if show["total"]:
        return f"{show['avail']} of {show['total']} seats left"
    return f"{show['avail']} seats left"


def fmt_price(price):
    if not price:
        return "—"
    return f"₹{price:.0f}" if float(price).is_integer() else f"₹{price:.2f}"


def date_label(date_str):
    if not date_str:
        return "Today"
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    today = now_ist().date()
    pretty = day.strftime("%a %d %b")
    if day == today:
        return f"Today · {pretty}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {pretty}"
    return pretty


def date_choices(days=21):
    today = now_ist().date()
    dates = [str(today + timedelta(days=i)) for i in range(days)]
    return [(date_label(d), d) for d in dates]


def normalize_date(value):
    """Accept YYYY-MM-DD (or DD-MM-YYYY / DD/MM/YYYY). Returns None if invalid."""
    value = (value or "").strip()
    if not value:
        return today_ist()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def cinema_name_from_url(url):
    slug = url.split("?")[0].rstrip("/").split("/")[-1]
    slug = re.sub(r"-CD\d+$", "", slug, flags=re.I)
    return slug.replace("-", " ").title() or "Cinema"


def shorten(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# District.in scraper
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def query_url(cinema_url, date):
    base = cinema_url.split("?")[0].strip()
    return f"{base}?fromdate={date}" if date else base


def find_sessions(data):
    """District files a cinema's shows under a key that changes with the URL
    (the cinema ID, or cinema ID + date), so take whichever block has them."""
    sessions = (data.get("pageData") or {}).get("sessions") or []
    if sessions:
        return sessions, list(data.get("groupedMovies") or [])
    for state in (data.get("serverState") or {}).values():
        if not isinstance(state, dict):
            continue
        found = (state.get("pageData") or {}).get("sessions") or []
        if found:
            meta = state.get("meta") or {}
            return found, list(meta.get("movies") or []) + list(meta.get("groupedMovies") or [])
    return [], []


def build_movie_lookup(movies):
    """Map every ID District uses for a movie (movie code, contentId) to its details."""
    lookup = {}
    for m in movies:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or m.get("label")
        if not name:
            continue
        info = {
            "name": name,
            "content_id": m.get("contentId"),
            "format": m.get("scrnFmt") if isinstance(m.get("scrnFmt"), str) else None,
        }
        keys = [m.get("id"), m.get("contentId")]
        for group in m.get("languageFormatGroups") or []:
            for fmt in group.get("screenFormats") or []:
                keys.append(fmt.get("movieCode"))
        for key in keys:
            if key not in (None, ""):
                lookup.setdefault(str(key), info)
    return lookup


def lowest_price(areas):
    prices = [to_float(a.get("price")) for a in (areas or []) if isinstance(a, dict)]
    prices = [p for p in prices if p > 0]
    return min(prices) if prices else 0.0


def session_booking_url(session, content_id, fallback):
    """Deep link to the seat picker for one show (same format District uses)."""
    s = session
    if s.get("encSessionId"):
        fid, enc = str(s.get("fid", "")), s["encSessionId"]
    elif all(s.get(k) for k in ("cid", "sid", "mid", "fid")):
        fid = str(s["fid"]).lower()
        enc = f"{s['cid']}-{s['sid']}-{str(s['mid']).lower()}-{fid}"
    else:
        return fallback
    url = f"https://www.district.in/movies/seat-layout/{fid}?encsessionid={enc}&freeseating=false&fromsessions=true&type=CINEMAS"
    if content_id:
        url += f"&contentid={content_id}"
    return url


def parse_district_shows(url):
    """Return (cinema_name, shows). Each show has an IST `start` datetime."""
    page = fetch_html(url)
    cinema_name = None
    shows = []

    # 1. Live session data (has seat counts)
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("data", {}) or {}
            cinema_name = data.get("cinemaName")
            sessions, movies = find_sessions(data)
            lookup = build_movie_lookup(movies)
            for s in sessions:
                movie = lookup.get(str(s.get("mid"))) or lookup.get(str(s.get("contentId"))) or {}
                shows.append({
                    "title": movie.get("name") or f"{s.get('lang', '')} movie".strip(),
                    "format": s.get("scrnFmt") or movie.get("format") or s.get("premiumLabel") or "2D",
                    # District's session times are UTC with no timezone marker.
                    "start": parse_time(s.get("showTime"), naive_is_utc=True),
                    "audi": s.get("audi", ""),
                    "avail": to_int(s.get("avail")),
                    "total": to_int(s.get("total")),
                    "price": lowest_price(s.get("areas")),
                    "url": session_booking_url(s, movie.get("content_id") or s.get("contentId"), url),
                })
        except Exception as e:
            add_log(f"Parser note: {e}")

    # 2. Schema.org events (backup source, carries District's own booking links)
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.DOTALL):
        try:
            item = json.loads(block)
        except ValueError:
            continue
        for ev in item if isinstance(item, list) else [item]:
            if not isinstance(ev, dict) or ev.get("@type") != "ScreeningEvent":
                continue
            name = ev.get("name", "")
            title = (ev.get("workPresented") or {}).get("name") or name
            start = parse_time(ev.get("startDate"), naive_is_utc=False)
            offers = ev.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            book_url = offers.get("url") or url

            same = next((s for s in shows if s["title"].lower() == title.lower() and s["start"] == start), None)
            if same:
                same["url"] = book_url
                continue
            sold_out = str(offers.get("availability", "")).endswith("SoldOut")
            shows.append({
                "title": title,
                "format": ev.get("videoFormat") or ("IMAX" if "IMAX" in name else "3D" if "3D" in name else "2D"),
                "start": start,
                "audi": "",
                "avail": 0 if sold_out else None,
                "total": None,
                "price": to_float(offers.get("price")),
                "url": book_url,
            })

    shows.sort(key=lambda s: s["start"].timestamp() if s["start"] else float("inf"))
    return cinema_name or cinema_name_from_url(url), shows


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


def seats_open(show):
    return show["avail"] is None or show["avail"] > 0


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def response_error(res):
    try:
        body = res.json()
        return body.get("description") or body.get("error") or body.get("message") or f"HTTP {res.status_code}"
    except ValueError:
        return f"HTTP {res.status_code}"


def show_lines(shows, limit=5):
    lines = [f"• {fmt_when(s['start'])} · {s['format']} · {seats_text(s)} · {fmt_price(s['price'])}" for s in shows[:limit]]
    if len(shows) > limit:
        lines.append(f"…and {len(shows) - limit} more")
    return lines


def send_task_notification(task, shows, cinema_name, alert_idx=1, max_alerts=1, test=False):
    """Send to every channel on the task. Returns [(channel, ok, detail), ...]."""
    first = shows[0]
    titles = list(dict.fromkeys(s["title"] for s in shows))
    formats = list(dict.fromkeys(str(s["format"]) for s in shows))
    movie = f"{' / '.join(titles)} ({', '.join(formats)})"
    counter = f"[{alert_idx}/{max_alerts}] " if max_alerts > 1 and not test else ""
    headline = "🧪 Test alert from District Ticket Monitor" if test else f"🚨 {counter}Tickets open: {' / '.join(titles)}"
    intro = "Your alerts work. A real alert will look like this:" if test else f"{len(shows)} matching show(s) have seats right now."
    lines = show_lines(shows)
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
                    "message": "\n".join([intro, f"🎬 {movie}", f"🏛️ {cinema_name}", ""] + lines),
                    "priority": 5,
                    "tags": ["test_tube"] if test else ["rotating_light", "ticket"],
                    "click": first["url"],
                    "actions": [{"action": "view", "label": "Book seats", "url": first["url"]}],
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
            [f"<b>{esc(headline)}</b>", "", esc(intro), f"🎬 <b>{esc(movie)}</b>", f"🏛️ {esc(cinema_name)}", ""]
            + [esc(line) for line in lines]
            + ["", f'👉 <a href="{esc(first["url"], quote=True)}"><b>Book seats now</b></a>']
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
            "url": first["url"],
            "color": 0x6366F1 if test else 0xE11D48,
            "description": "\n".join([intro, f"**{movie}**", cinema_name, ""] + lines),
            "footer": {"text": "District Ticket Monitor"},
        }
        content = "🧪 Test alert" if test else "@everyone 🔥 Tickets are open!"
        try:
            res = requests.post(discord_url, json={"content": content, "embeds": [embed]}, timeout=10)
            ok = res.status_code in (200, 204)
            results.append(("Discord", ok, "sent" if ok else response_error(res)))
        except Exception as e:
            results.append(("Discord", False, redact(e, discord_url)))

    return results


def describe_results(results):
    if not results:
        return "no alert channels set"
    return " · ".join(f"✅ {ch}" if ok else f"❌ {ch}: {detail}" for ch, ok, detail in results)


# ---------------------------------------------------------------------------
# Background engine (one thread, shared by every watcher)
# ---------------------------------------------------------------------------
def check_task(task, cinema_name, shows):
    date = task.get("target_date", "")
    listed = [s for s in shows if on_date(s, date)]
    keywords = split_keywords(task.get("movie_filter"))
    matching = [s for s in listed if show_matches(s, keywords, task.get("format_filter")) and seats_open(s)]
    name = task.get("user_label", "Watcher")

    with tasks_lock:
        if app_state["tasks"].get(task.get("id")) is not task or task.get("status") != "Active":
            return  # deleted or paused while this check was running
        task["cinema_name"] = cinema_name
        task["last_checked"] = now_ist().strftime("%H:%M:%S")

        if not matching:
            result = f"No match · {len(listed)} shows listed" if listed else "No shows listed yet"
            if result != task.get("last_result"):
                add_log(f"[{name}] {cinema_name}: {result.lower()}")
            task["last_result"] = result
            return

        max_alerts = int(task.get("max_alerts", 5))
        count = int(task.get("alert_count", 0))
        if count >= max_alerts:
            task["status"] = "Done"
            save_tasks()
            return
        count += 1
        task["alert_count"] = count
        task["last_result"] = f"🔥 {len(matching)} show(s) open"
        if count >= max_alerts:
            task["status"] = "Done"
        save_tasks()
        snapshot = dict(task)

    add_log(f"🔥 [{name}] {len(matching)} matching show(s) open at {cinema_name}! Sending alert {count}/{max_alerts}…")
    results = send_task_notification(snapshot, matching, cinema_name, count, max_alerts)
    add_log(f"   [{name}] {describe_results(results)}")
    if count >= max_alerts:
        add_log(f"✅ [{name}] All {max_alerts} alerts sent, watcher finished. Re-arm it from the Watchers tab.")


def run_sweep(active):
    groups = {}  # one page fetch per cinema + date, shared by everyone watching it
    for t in active:
        groups.setdefault(query_url(t["cinema_url"], t.get("target_date", "")), []).append(t)

    for url, group in groups.items():
        if app_state["paused"]:
            return
        try:
            cinema_name, shows = parse_district_shows(url)
        except Exception as e:
            with tasks_lock:
                for t in group:
                    t["last_checked"] = now_ist().strftime("%H:%M:%S")
                    t["last_result"] = "⚠️ Page didn't load"
            add_log(f"⚠️ Couldn't load {cinema_name_from_url(url)}: {e}")
            continue
        for t in group:
            check_task(t, cinema_name, shows)


def monitor_loop():
    add_log("▶ Monitoring engine started.")
    while True:
        try:
            if not app_state["paused"]:
                with tasks_lock:
                    active = [t for t in app_state["tasks"].values() if t.get("status") == "Active"]
                if active:
                    run_sweep(active)
                    app_state["check_count"] += 1
                    app_state["last_check_time"] = now_ist()
        except Exception as e:
            add_log(f"Engine error: {e}")
        wake_engine.wait(timeout=max(5, int(app_state["interval_sec"])))
        wake_engine.clear()


_engine_thread = None


def start_engine():
    global _engine_thread
    if _engine_thread is None or not _engine_thread.is_alive():
        _engine_thread = threading.Thread(target=monitor_loop, daemon=True)
        _engine_thread.start()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
TABLE_HEADERS = ["Watcher", "Cinema", "Movie", "Format", "Date", "Alerts", "Last check (IST)", "Alerts via", "Status"]
PREVIEW_HEADERS = ["Match", "Movie", "Format", "Showtime (IST)", "Seats left", "From"]
STATUS_LABELS = {"Active": "🟢 Watching", "Paused": "⏸ Paused", "Done": "✅ Alerted"}


def channels_summary(task):
    names = []
    if task.get("ntfy_topic"):
        names.append("ntfy")
    if task.get("telegram_chat_id"):
        names.append("Telegram")
    if task.get("discord_webhook"):
        names.append("Discord")
    return " · ".join(names) or "None"


def get_tasks_table():
    with tasks_lock:
        tasks = list(app_state["tasks"].values())
    rows = []
    for t in tasks:
        last = f"{t.get('last_result', '')} · {t['last_checked']}" if t.get("last_checked") else "Waiting for first check"
        rows.append([
            t.get("user_label", ""),
            shorten(t.get("cinema_name") or cinema_name_from_url(t.get("cinema_url", "")), 36),
            t.get("movie_filter") or "Any movie",
            t.get("format_filter", "ANY"),
            date_label(t.get("target_date", "")),
            f"{t.get('alert_count', 0)} / {t.get('max_alerts', 5)}",
            last,
            channels_summary(t),
            STATUS_LABELS.get(t.get("status"), t.get("status", "")),
        ])
    return pd.DataFrame(rows, columns=TABLE_HEADERS)


def watcher_choices():
    with tasks_lock:
        tasks = list(app_state["tasks"].items())
    return [
        (f"{t.get('user_label', 'Watcher')} · {t.get('movie_filter') or 'Any movie'} · "
         f"{date_label(t.get('target_date', ''))} · {STATUS_LABELS.get(t.get('status'), '')}", tid)
        for tid, t in tasks
    ]


def status_html():
    with tasks_lock:
        tasks = list(app_state["tasks"].values())
    watching = sum(1 for t in tasks if t.get("status") == "Active")
    alerts = sum(int(t.get("alert_count", 0)) for t in tasks)
    running = not app_state["paused"]
    last = app_state["last_check_time"]
    if last:
        last_value = last.strftime("%I:%M:%S %p").lstrip("0")
        last_sub = f"IST · {int((now_ist() - last).total_seconds())}s ago"
    else:
        last_value, last_sub = "—", "no checks yet"
    dot = '<span class="dot on"></span>Running' if running else '<span class="dot off"></span>Paused'
    cards = [
        ("Engine", dot, f"every {app_state['interval_sec']}s · {app_state['check_count']} checks"),
        ("Watching", str(watching), f"{len(tasks)} watcher(s) in total"),
        ("Alerts sent", str(alerts), "across all watchers"),
        ("Last check", last_value, last_sub),
    ]
    body = "".join(
        f'<div class="stat-card"><div class="stat-label">{label}</div>'
        f'<div class="stat-value">{value}</div><div class="stat-sub">{sub}</div></div>'
        for label, value, sub in cards
    )
    return f'<div class="stat-row">{body}</div>'


def logs_text():
    with log_lock:
        return "\n".join(reversed(app_state["logs"])) or "Nothing yet."


def clean_channels(ntfy, tg_token, tg_chat, discord):
    """Validate alert settings. Returns (channels, error)."""
    ntfy = re.sub(r"^(https?://)?ntfy\.sh/", "", (ntfy or "").strip()).strip("/")
    tg_token, tg_chat, discord = (tg_token or "").strip(), (tg_chat or "").strip(), (discord or "").strip()
    if ntfy and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ntfy):
        return None, "❌ ntfy topic names can only use letters, numbers, `-` and `_`."
    if bool(tg_token) != bool(tg_chat):
        return None, "❌ Telegram needs both the bot token and your chat ID."
    if discord and not discord.startswith("https://"):
        return None, "❌ The Discord webhook URL should start with `https://`."
    if not (ntfy or tg_token or discord):
        return None, "❌ Add at least one way to alert you: ntfy, Telegram or Discord."
    return {"ntfy_topic": ntfy, "telegram_token": tg_token, "telegram_chat_id": tg_chat, "discord_webhook": discord}, None


# ---------------------------------------------------------------------------
# UI actions
# ---------------------------------------------------------------------------
def preview_shows(cin_url, movie_names, fmt_val, date_val):
    hidden = gr.update(visible=False)
    cin_url = (cin_url or "").strip()
    if "district.in" not in cin_url:
        return "❌ Paste a cinema page link from district.in first.", hidden
    date = normalize_date(date_val)
    if not date:
        return "❌ The date should look like 2026-11-20.", hidden
    try:
        cinema_name, shows = parse_district_shows(query_url(cin_url, date))
    except Exception as e:
        return f"❌ Couldn't load that page: {e}", hidden

    listed = [s for s in shows if on_date(s, date)]
    if not listed:
        return (f"### {cinema_name}\nNo shows are listed for **{date_label(date)}** yet. That's normal before "
                f"bookings open, and a watcher will keep checking."), hidden

    keywords = split_keywords(movie_names)
    matched = [s for s in listed if show_matches(s, keywords, fmt_val)]
    matched_ids = {id(s) for s in matched}
    bookable = [s for s in matched if seats_open(s)]

    lines = [f"### {cinema_name}",
             f"**{date_label(date)}** · {len(listed)} shows listed · **{len(matched)} match your filters** "
             f"({len(bookable)} with seats)"]
    if bookable:
        lines.append("🔥 Tickets are already open for this. A watcher would alert you on its first check.")
    elif not matched:
        movies = sorted({s["title"] for s in listed})
        formats = sorted({str(s["format"]) for s in listed})
        lines.append(f"Nothing matches yet. **Movies here:** {', '.join(movies)}  \n**Formats:** {', '.join(formats)}")

    rows = sorted(listed, key=lambda s: id(s) not in matched_ids)
    table = pd.DataFrame(
        [["✅" if id(s) in matched_ids else "", s["title"], s["format"], fmt_when(s["start"]), fmt_seats(s), fmt_price(s["price"])]
         for s in rows],
        columns=PREVIEW_HEADERS,
    )
    return "\n\n".join(lines), gr.update(value=table, visible=True)


def test_user_notification(user_name, cin_url, ntfy_top, tg_tok, tg_chat, disc_url):
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return error
    task = dict(channels, user_label=(user_name or "").strip() or "Test")
    start = (now_ist() + timedelta(hours=3)).replace(minute=30, second=0, microsecond=0)
    show = [{
        "title": "Test Movie", "format": "IMAX 3D", "start": start, "audi": "",
        "avail": 120, "total": 200, "price": 450.0,
        "url": (cin_url or "").strip() or "https://www.district.in",
    }]
    results = send_task_notification(task, show, "District Cinema", test=True)
    if all(ok for _, ok, _ in results):
        return f"🔔 Test sent: {describe_results(results)}. Check your phone."
    return f"⚠️ Some alerts failed: {describe_results(results)}"


def add_new_watcher(user_name, cin_url, movie_names, fmt_val, date_val, max_alerts_val, ntfy_top, tg_tok, tg_chat, disc_url):
    keep = gr.update()
    cin_url = (cin_url or "").strip()
    if "district.in" not in cin_url:
        return "❌ Paste a cinema page link from district.in.", keep, keep
    date = normalize_date(date_val)
    if not date:
        return "❌ The date should look like 2026-11-20.", keep, keep
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return error, keep, keep

    task_id = "w_" + uuid.uuid4().hex[:6]
    name = (user_name or "").strip() or f"User {len(app_state['tasks']) + 1}"
    task = dict(
        channels,
        id=task_id,
        user_label=name,
        cinema_url=cin_url,
        cinema_name=cinema_name_from_url(cin_url),
        target_date=date,
        movie_filter=(movie_names or "").strip(),
        format_filter=(fmt_val or "ANY").strip().upper(),
        max_alerts=int(max_alerts_val or 5),
        alert_count=0,
        status="Active",
        created_at=now_ist().strftime("%d-%b %H:%M"),
    )
    with tasks_lock:
        app_state["tasks"][task_id] = task
        save_tasks()
    wake_engine.set()

    movie = task["movie_filter"] or "any movie"
    add_log(f"➕ [{name}] watching for {movie} ({task['format_filter']}) on {date_label(date)} via {channels_summary(task)}.")
    msg = (f"✅ **{name}** is watching for **{movie}** ({task['format_filter']}) on **{date_label(date)}**. "
           f"The first check runs now. Follow it in the Watchers tab.")
    return msg, get_tasks_table(), gr.update(choices=watcher_choices())


def manage_result(msg, selected):
    choices = watcher_choices()
    value = selected if any(v == selected for _, v in choices) else None
    return msg, get_tasks_table(), gr.update(choices=choices, value=value)


def toggle_watcher(task_id):
    if not task_id:
        return manage_result("Pick a watcher first.", None)
    with tasks_lock:
        task = app_state["tasks"].get(task_id)
        if not task:
            return manage_result("That watcher no longer exists.", None)
        status = task.get("status")
        if status == "Done":
            return manage_result("This watcher already sent all its alerts. Use **Re-arm** to start it again.", task_id)
        task["status"] = "Paused" if status == "Active" else "Active"
        save_tasks()
        name, new_status = task.get("user_label"), task["status"]
    wake_engine.set()
    add_log(f"{'⏸' if new_status == 'Paused' else '▶'} [{name}] {'paused' if new_status == 'Paused' else 'resumed'}.")
    return manage_result(f"{STATUS_LABELS[new_status]}: **{name}**", task_id)


def rearm_watcher(task_id):
    if not task_id:
        return manage_result("Pick a watcher first.", None)
    with tasks_lock:
        task = app_state["tasks"].get(task_id)
        if not task:
            return manage_result("That watcher no longer exists.", None)
        task.update(alert_count=0, status="Active", last_result="", last_checked="")
        save_tasks()
        name = task.get("user_label")
    wake_engine.set()
    add_log(f"🔄 [{name}] re-armed: alert count reset and watching again.")
    return manage_result(f"🔄 **{name}** is re-armed and watching again.", task_id)


def delete_watcher(task_id):
    if not task_id:
        return manage_result("Pick a watcher first.", None)
    with tasks_lock:
        task = app_state["tasks"].pop(task_id, None)
        if task:
            save_tasks()
    if not task:
        return manage_result("That watcher no longer exists.", None)
    add_log(f"🗑️ [{task.get('user_label')}] watcher deleted.")
    return manage_result(f"🗑️ Deleted **{task.get('user_label')}**.", None)


def refresh_watchers(selected):
    _, table, dropdown = manage_result("", selected)
    return table, dropdown


def toggle_engine():
    app_state["paused"] = not app_state["paused"]
    wake_engine.set()
    if app_state["paused"]:
        add_log("⏸ Engine paused. No watcher is being checked.")
        return "⏸ Paused. No watcher is being checked until you resume.", status_html()
    add_log("▶ Engine resumed.")
    return "▶ Running. Checks resumed.", status_html()


def change_interval(val):
    app_state["interval_sec"] = max(5, int(val))
    add_log(f"⏱️ Check interval set to {app_state['interval_sec']}s.")
    return f"⏱️ Checking every {app_state['interval_sec']} seconds.", status_html()


def clear_logs():
    with log_lock:
        app_state["logs"].clear()
    return logs_text()


def poll_status():
    return status_html(), get_tasks_table(), logs_text()


def on_page_load():
    return (
        gr.update(choices=date_choices(), value=today_ist()),
        gr.update(value=app_state["interval_sec"]),
        get_tasks_table(),
        gr.update(choices=watcher_choices()),
        status_html(),
    )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
CSS = """
.gradio-container { max-width: 1180px !important; margin: 0 auto !important; }
.app-header h1 { font-size: 1.9rem; font-weight: 700; margin: 0 0 4px; }
.app-header p { margin: 0; color: var(--body-text-color-subdued); font-size: 1rem; }
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.stat-card { background: var(--block-background-fill); border: 1px solid var(--block-border-color);
             border-radius: 14px; padding: 14px 16px; }
.stat-label { font-size: .72rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
              color: var(--body-text-color-subdued); }
.stat-value { font-size: 1.4rem; font-weight: 700; margin: 4px 0 2px; color: var(--body-text-color); }
.stat-sub { font-size: .8rem; color: var(--body-text-color-subdued); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }
.dot.on { background: #22c55e; box-shadow: 0 0 0 4px rgba(34, 197, 94, .18); }
.dot.off { background: #f59e0b; box-shadow: 0 0 0 4px rgba(245, 158, 11, .18); }
.help-card { background: var(--block-background-fill); border: 1px solid var(--block-border-color);
             border-radius: 14px; padding: 4px 18px 10px; }
"""

HELP_MD = """
### How it works
1. **Find your cinema** on [district.in](https://www.district.in), open its page and copy the link.
2. **Type the movie** and pick a format and date. Tap **Preview shows** to see what's listed right now.
3. **Choose where to get alerts.** ntfy is quickest: install the app, subscribe to a topic, and enter the same topic here.
4. **Send a test alert**, then **Start watching**.

When a matching show has seats, you get the number of alerts you chose, each with a link straight to seat selection. Then the watcher stops; re-arm it from the **Watchers** tab.

---

**Tips**
- Use a topic name nobody would guess, like `arun_tickets_982`. Anyone who knows it can read your alerts.
- Watchers are wiped when this Space restarts or goes to sleep after a long time without visitors. If you're waiting days, check back now and then.
- All times are Indian Standard Time.
"""

theme = gr.themes.Soft(
    primary_hue="rose",
    secondary_hue="slate",
    neutral_hue="slate",
    font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
)

with gr.Blocks(title="District Ticket Monitor", theme=theme, css=CSS) as demo:
    gr.HTML(
        '<div class="app-header"><h1>🎟️ District Ticket Monitor</h1>'
        "<p>Get a phone alert the moment tickets open for your movie on District.</p></div>"
    )
    status_box = gr.HTML(status_html())

    with gr.Tabs():
        # --- New watcher -----------------------------------------------------
        with gr.Tab("➕ New watcher"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3):
                    gr.Markdown("### 🎬 What to watch")
                    cin_url_input = gr.Textbox(
                        label="Cinema page on District",
                        value=DEFAULT_URL,
                        placeholder="https://www.district.in/movies/...",
                        info="Open your cinema on district.in and copy the link from the address bar.",
                    )
                    with gr.Row():
                        movie_filter_input = gr.Textbox(
                            label="Movie name", value="Ramayana", placeholder="e.g. Ramayana, Jailer 2",
                            info="Part of the name is enough. Separate several with commas.", scale=2,
                        )
                        format_filter_input = gr.Dropdown(
                            label="Format", choices=FORMAT_CHOICES, value="IMAX", allow_custom_value=True, scale=1,
                        )
                        target_date_input = gr.Dropdown(
                            label="Show date", choices=date_choices(), value=today_ist(), allow_custom_value=True, scale=1,
                        )
                    preview_btn = gr.Button("🔍 Preview shows on this date", variant="secondary")
                    preview_summary = gr.Markdown()
                    preview_table = gr.Dataframe(
                        headers=PREVIEW_HEADERS, interactive=False, wrap=True, visible=False, height=360,
                    )

                    gr.Markdown("### 📱 Where to alert you")
                    user_name_input = gr.Textbox(label="Your name", value="Me", placeholder="e.g. Arun")
                    with gr.Tabs():
                        with gr.Tab("ntfy (easiest)"):
                            gr.Markdown("Install the free **ntfy** app, tap **+** and subscribe to a topic. Enter the same topic here.")
                            ntfy_topic_input = gr.Textbox(label="ntfy topic", placeholder="e.g. arun_tickets_982")
                        with gr.Tab("Telegram"):
                            gr.Markdown("Create a bot with **@BotFather** and send it any message. Get your chat ID from **@userinfobot**.")
                            with gr.Row():
                                tg_token_input = gr.Textbox(label="Bot token", type="password", placeholder="123456:ABC-DEF…")
                                tg_chat_input = gr.Textbox(label="Chat ID", placeholder="123456789")
                        with gr.Tab("Discord"):
                            gr.Markdown("In your server: **Edit channel → Integrations → Webhooks → New webhook**, then copy its URL.")
                            disc_hook_input = gr.Textbox(label="Webhook URL", placeholder="https://discord.com/api/webhooks/…")

                    gr.Markdown("### 🚨 When tickets open")
                    max_alerts_slider = gr.Slider(
                        label="Number of alerts", minimum=1, maximum=20, value=5, step=1,
                        info="One alert per check until this many are sent, then the watcher stops.",
                    )
                    with gr.Row():
                        test_notify_btn = gr.Button("🔔 Send test alert", variant="secondary")
                        add_task_btn = gr.Button("🚀 Start watching", variant="primary", scale=2)
                    add_result_lbl = gr.Markdown()

                with gr.Column(scale=2):
                    gr.Markdown(HELP_MD, elem_classes="help-card")

        # --- Watchers --------------------------------------------------------
        with gr.Tab("📋 Watchers") as watchers_tab:
            tasks_table = gr.Dataframe(value=get_tasks_table(), headers=TABLE_HEADERS, interactive=False, wrap=True)
            watcher_dd = gr.Dropdown(label="Choose a watcher to manage", choices=watcher_choices(), value=None)
            with gr.Row():
                pause_btn = gr.Button("⏯ Pause / resume")
                rearm_btn = gr.Button("🔄 Re-arm alerts")
                delete_btn = gr.Button("🗑️ Delete", variant="stop")
            manage_status_lbl = gr.Markdown()

        # --- Activity --------------------------------------------------------
        with gr.Tab("📜 Activity"):
            live_logs_box = gr.Textbox(
                label="Activity log (newest first)", value=logs_text(), lines=22, max_lines=22,
                interactive=False, show_copy_button=True,
            )
            clear_logs_btn = gr.Button("Clear log", size="sm")

        # --- Settings --------------------------------------------------------
        with gr.Tab("⚙️ Settings"):
            gr.Markdown("### Engine\nThese settings apply to every watcher on this Space.")
            interval_slider = gr.Slider(
                label="Check every (seconds)", minimum=5, maximum=60, step=5, value=app_state["interval_sec"],
                info="Very frequent checks can get the Space blocked by District.",
            )
            toggle_engine_btn = gr.Button("⏯ Pause / resume all checking")
            settings_status = gr.Markdown()

    # --- Events ---------------------------------------------------------------
    preview_btn.click(
        preview_shows,
        inputs=[cin_url_input, movie_filter_input, format_filter_input, target_date_input],
        outputs=[preview_summary, preview_table],
    )
    test_notify_btn.click(
        test_user_notification,
        inputs=[user_name_input, cin_url_input, ntfy_topic_input, tg_token_input, tg_chat_input, disc_hook_input],
        outputs=[add_result_lbl],
    )
    add_task_btn.click(
        add_new_watcher,
        inputs=[user_name_input, cin_url_input, movie_filter_input, format_filter_input, target_date_input,
                max_alerts_slider, ntfy_topic_input, tg_token_input, tg_chat_input, disc_hook_input],
        outputs=[add_result_lbl, tasks_table, watcher_dd],
    )

    manage_outputs = [manage_status_lbl, tasks_table, watcher_dd]
    pause_btn.click(toggle_watcher, inputs=[watcher_dd], outputs=manage_outputs)
    rearm_btn.click(rearm_watcher, inputs=[watcher_dd], outputs=manage_outputs)
    delete_btn.click(delete_watcher, inputs=[watcher_dd], outputs=manage_outputs)
    watchers_tab.select(refresh_watchers, inputs=[watcher_dd], outputs=[tasks_table, watcher_dd], show_progress="hidden")

    clear_logs_btn.click(clear_logs, outputs=[live_logs_box], show_progress="hidden")
    toggle_engine_btn.click(toggle_engine, outputs=[settings_status, status_box])
    interval_slider.release(change_interval, inputs=[interval_slider], outputs=[settings_status, status_box])

    timer = gr.Timer(3)
    timer.tick(poll_status, outputs=[status_box, tasks_table, live_logs_box], show_progress="hidden")
    demo.load(
        on_page_load,
        outputs=[target_date_input, interval_slider, tasks_table, watcher_dd, status_box],
        show_progress="hidden",
    )

# --- Launch -------------------------------------------------------------------
start_engine()
demo.queue(default_concurrency_limit=8).launch(server_name="0.0.0.0", server_port=7860)
