import os
import re
import json
import time
import uuid
import threading
import urllib.request
import html as html_lib
from urllib.parse import urlparse, quote
from datetime import datetime, timezone, timedelta

import requests
import gradio as gr

# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------
APP_NAME = "BookTick"
APP_TAGLINE = "Get pinged the second tickets open for your movie on District."
AUTHOR_NAME = "Sabarish SB"
GITHUB_USER = "sabarishh14"
GITHUB_URL = f"https://github.com/{GITHUB_USER}"
GITHUB_AVATAR = f"https://github.com/{GITHUB_USER}.png?size=96"
BRAND_DISCORD_COLOR = 0xF9573A

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
TASKS_FILE = "booktick_watchers.json"
LEGACY_TASKS_FILE = "district_tasks.json"  # file name used before the BookTick rename
DEFAULT_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"
FORMAT_CHOICES = ["ANY", "IMAX", "4DX", "3D", "2D"]
MAX_LOG_LINES = 300

# RLock so helpers that take the lock can safely call each other.
tasks_lock = threading.RLock()
log_lock = threading.Lock()
wake_engine = threading.Event()  # set it to make the engine check right away


def load_tasks():
    for path in (TASKS_FILE, LEGACY_TASKS_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
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


def fmt_clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_when(dt):
    if not dt:
        return "Time unknown"
    return dt.strftime("%a %d %b · ") + fmt_clock(dt)


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


def date_is_past(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date() < now_ist().date()
    except (TypeError, ValueError):
        return False


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


def clean_cinema_url(value):
    """Return a usable district.in link (adding https:// if it's missing), or None."""
    url = (value or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    if host == "district.in" or host.endswith(".district.in"):
        return url
    return None


def cinema_name_from_url(url):
    slug = url.split("?")[0].rstrip("/").split("/")[-1]
    slug = re.sub(r"-CD\d+$", "", slug, flags=re.I)
    return slug.replace("-", " ").title() or "Cinema"


def shorten(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ago(timestamp):
    if not timestamp:
        return ""
    secs = max(0, int(time.time() - float(timestamp)))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


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
# District directory: cities, their cinemas, and what each cinema is showing
# ---------------------------------------------------------------------------
CITY_SITEMAP_URL = "https://cdn.district.in/sitemap/sitemap-cinema-city.xml"
DEFAULT_CITY = "chennai"
DEFAULT_CINEMA_LABEL = "INOX Phoenix Market City (formerly Jazz Cinemas), Velachery"
POPULAR_CITIES = [
    "chennai", "bengaluru", "mumbai", "new-delhi", "hyderabad", "kolkata", "pune", "ahmedabad", "kochi",
    "coimbatore", "gurgaon", "noida", "jaipur", "lucknow", "chandigarh", "vizag", "madurai", "trivandrum",
    "mysuru", "indore",
]
_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, loader, fallback=None, retry_after=300):
    """Return a cached value, reloading it after `ttl` seconds.

    If a reload fails, keep serving the last good value (or `fallback`) and try again
    after `retry_after` seconds. With nothing to fall back on, the error is raised.
    """
    with _cache_lock:
        hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        value, expires = loader(), time.time() + ttl
    except Exception:
        value = hit[1] if hit else fallback
        if value is None:
            raise
        expires = time.time() + retry_after
    with _cache_lock:
        _cache[key] = (expires, value)
    return value


def dig(node, *keys):
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def next_data(page):
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.DOTALL)
    return json.loads(match.group(1)) if match else {}


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "cinema"


def city_label(key):
    return str(key or "").replace("-", " ").title()


def load_cities():
    """Every city District lists cinemas for, from its public sitemap."""
    keys = set(re.findall(r"/movies/cinemas-in-([a-z0-9-]+)\s*</loc>", fetch_html(CITY_SITEMAP_URL)))
    if not keys:
        raise ValueError("no cities found in District's sitemap")
    return sorted(keys)


def city_choices():
    keys = set(cached("cities", 24 * 3600, load_cities, fallback=POPULAR_CITIES))
    top = [k for k in POPULAR_CITIES if k in keys]
    rest = sorted((k for k in keys if k not in top), key=city_label)
    return [(city_label(k), k) for k in top + rest]


def load_city_cinemas(city):
    """Read District's 'cinemas in <city>' page: each cinema's link and movies."""
    page = fetch_html(f"https://www.district.in/movies/cinemas-in-{city}")
    rails = dig(next_data(page), "props", "pageProps", "data", "serverState", "EDSResponse", "rails") or []
    links = {cid: path for path, cid in re.findall(r"(/movies/[a-z0-9-]+-CD(\d+))", page)}
    cinemas = {}
    for rail in rails:
        for item in rail.get("items") or []:
            info = dig(item, "ItemDetails", "CinemaData") or {}
            cid, name = str(info.get("cinema_id") or ""), info.get("cinema_name")
            if not cid or not name or cid in cinemas:
                continue
            movies = [dig(child, "ItemDetails", "MovieData", "name") for child in item.get("children") or []]
            cinemas[cid] = {
                "id": cid,
                "name": name,
                # District takes the cinema's display name from the link, so prefer its own links.
                "url": "https://www.district.in" + links.get(cid, f"/movies/{slugify(name)}-CD{cid}"),
                "movies": list(dict.fromkeys(m for m in movies if m)),
            }
    if not cinemas:
        raise ValueError("District's page didn't list any cinemas")
    return sorted(cinemas.values(), key=lambda c: c["name"].lower())


def city_cinemas(city):
    city = str(city or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,60}", city):
        raise ValueError("Pick a city from the list")
    return cached(f"cinemas:{city}", 30 * 60, lambda: load_city_cinemas(city))


def cinema_id_from_url(url):
    match = re.search(r"-CD(\d+)", str(url or ""), re.I)
    return match.group(1) if match else None


def cinema_label(cinema, city):
    """'PVR Grand Mall, Velachery, Chennai' -> 'PVR Grand Mall, Velachery' (the city is already picked)."""
    return re.sub(rf",\s*{re.escape(city_label(city))}\s*$", "", cinema["name"], flags=re.I) or cinema["name"]


def movie_choices(cinemas, cinema=None):
    """This cinema's movies first, then everything else showing in the city."""
    here = list(cinema["movies"]) if cinema else []
    elsewhere = sorted({m for c in cinemas for m in c["movies"]} - set(here), key=str.lower)
    return here + elsewhere


def movie_text(value):
    """The movie picker returns a list; watchers store the names comma-separated."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


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
    headline = f"🧪 Test alert from {APP_NAME}" if test else f"🚨 {counter}Tickets open: {' / '.join(titles)}"
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
            "color": 0x6366F1 if test else BRAND_DISCORD_COLOR,
            "description": "\n".join([intro, f"**{movie}**", cinema_name, ""] + lines),
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


def sample_shows(url):
    """A made-up show used by test alerts."""
    start = (now_ist() + timedelta(hours=3)).replace(minute=30, second=0, microsecond=0)
    return [{
        "title": "Test Movie", "format": "IMAX 3D", "start": start, "audi": "",
        "avail": 120, "total": 200, "price": 450.0,
        "url": url or "https://www.district.in",
    }]


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
        task["last_checked_ts"] = time.time()

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
                    t["last_checked_ts"] = time.time()
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
# HTML building blocks
# ---------------------------------------------------------------------------
# Watchers are shared by everyone who opens the Space, so anything a visitor typed
# (names, movies, links) is escaped before it goes into HTML.
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
    """The BookTick mark: a ticket with a tick. `uid` keeps gradient ids unique on the page."""
    g = f"btg-{uid}"
    return (
        f'<svg class="{cls}" viewBox="0 0 48 48" aria-hidden="true">'
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


def toast(ok, text):
    """Pop-up message. Gradio renders toasts as HTML, so the text is escaped here."""
    (gr.Info if ok else gr.Warning)(esc(shorten(text, 240)))


def step_head(badge, title, sub):
    return (f'<div class="bt-step-head"><span class="bt-step-num">{badge}</span>'
            f'<div><h3>{title}</h3><p>{sub}</p></div></div>')


def watcher_title(task):
    return task.get("movie_filter") or "Any movie"


def date_picker_html(value):
    """The show-date field: a button that opens a month calendar.

    The page script (BookTick.date in HEAD) draws the days and writes the picked date
    into a hidden textbox (#bt-date), which is what the Python handlers read.
    """
    weekdays = "".join(f"<span>{d}</span>" for d in ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"))
    return f"""
<div class="bt-date">
  <span class="bt-field-label" id="bt-date-label">Show date</span>
  <span class="bt-field-info">Show times are in IST.</span>
  <button type="button" class="bt-date-trigger" aria-haspopup="dialog" aria-expanded="false"
          aria-labelledby="bt-date-label bt-date-value">
    {icon("calendar")}<span class="bt-date-value" id="bt-date-value">{esc(date_label(value))}</span>{icon("chevron-down")}
  </button>
  <div class="bt-cal" role="dialog" aria-label="Choose a show date" hidden>
    <div class="bt-cal-head">
      <button type="button" class="bt-cal-nav" data-cal-nav="-1" aria-label="Previous month">{icon("chevron-left")}</button>
      <b class="bt-cal-title" aria-live="polite"></b>
      <button type="button" class="bt-cal-nav" data-cal-nav="1" aria-label="Next month">{icon("chevron-right")}</button>
    </div>
    <div class="bt-cal-week" aria-hidden="true">{weekdays}</div>
    <div class="bt-cal-days"></div>
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


HELP_HTML = f"""
<aside class="bt-help">
  <h3>How it works</h3>
  <ol class="bt-steps">
    <li><b>Pick your city and cinema</b><span>Every cinema on <a href="https://www.district.in" target="_blank" rel="noopener noreferrer">District</a> is listed. You can also paste a cinema's link.</span></li>
    <li><b>Pick the movie, format and date</b><span>Bookings not open yet? Type the movie's name. Tap <em>Preview shows</em> to see what's listed right now.</span></li>
    <li><b>Choose where to get alerts</b><span>ntfy is quickest: install the app, subscribe to a topic, and enter the same topic here.</span></li>
    <li><b>Send a test, then start watching</b><span>When a matching show has seats, you get the alerts you chose, each with a link straight to seat selection. Then the watcher stops; re-arm it from <em>Watchers</em>.</span></li>
  </ol>
  <h4>Good to know</h4>
  <ul class="bt-tips">
    <li>{icon("lock")}<span>Use a topic name nobody would guess, like <code>arun_tickets_982</code>. Anyone who knows it can read your alerts.</span></li>
    <li>{icon("alert")}<span>Watchers are wiped when this Space restarts or goes to sleep after a long time without visitors. If you're waiting days, check back now and then.</span></li>
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


def status_html():
    with tasks_lock:
        tasks = list(app_state["tasks"].values())
    watching = sum(1 for t in tasks if t.get("status") == "Active")
    alerts = sum(to_int(t.get("alert_count")) or 0 for t in tasks)
    running = not app_state["paused"]
    last = app_state["last_check_time"]
    if last:
        last_value, last_sub = fmt_clock(last), f"IST · {ago(last.timestamp())}"
    else:
        last_value, last_sub = "—", "No checks yet"

    engine_value = (f'<span class="bt-live {"on" if running else "off"}"></span>'
                    f'{"Running" if running else "Paused"}')
    toggle = (f'<button type="button" class="bt-mini" data-bt-action="engine" '
              f'title="{"Pause" if running else "Resume"} checking for every watcher">'
              f'{icon("pause" if running else "play")}{"Pause" if running else "Resume"}</button>')
    tiles = [
        ("activity", "brand", "Engine", engine_value,
         f"Every {app_state['interval_sec']}s · {plural(app_state['check_count'], 'check')}", toggle),
        ("eye", "ok", "Watching", str(watching), f"{plural(len(tasks), 'watcher')} in total", ""),
        ("bell", "hot", "Alerts sent", str(alerts), "Across all watchers", ""),
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
STATUS_META = {"Active": ("Watching", "active"), "Paused": ("Paused", "paused"), "Done": ("Alerted", "done")}
FILTERS = [("all", "All"), ("Active", "Watching"), ("Paused", "Paused"), ("Done", "Alerted")]
EMPTY_FILTER_TEXT = {
    "Active": "No watcher is checking right now.",
    "Paused": "No paused watchers.",
    "Done": "No watcher has finished its alerts yet.",
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


def watcher_card(task_id, t):
    status = t.get("status", "")
    label, tone = STATUS_META.get(status, (status or "Unknown", "paused"))
    movie = esc(watcher_title(t))
    owner = esc(t.get("user_label") or "Watcher")
    cinema = esc(shorten(t.get("cinema_name") or cinema_name_from_url(t.get("cinema_url", "")), 52))
    href = safe_href(t.get("cinema_url"))
    cinema_html = (f'<a href="{href}" target="_blank" rel="noopener noreferrer">{cinema}{icon("external")}</a>'
                   if href else cinema)
    fmt = str(t.get("format_filter") or "ANY")
    target = t.get("target_date", "")
    tags = [f'<span class="bt-tag">{icon("film")}{"Any format" if fmt == "ANY" else esc(fmt)}</span>',
            f'<span class="bt-tag">{icon("calendar")}{esc(date_label(target))}</span>']
    if date_is_past(target):
        tags.append('<span class="bt-tag warn">Date has passed</span>')

    sent = to_int(t.get("alert_count")) or 0
    max_alerts = to_int(t.get("max_alerts")) or 5
    pct = min(100, round(100 * sent / max_alerts)) if max_alerts else 0

    if t.get("last_checked"):
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
    else:
        actions.append(action_button("rearm", task_id, "Re-arm", "rotate", "primary", "Reset the alert count and watch again"))
    if status != "Done" and sent > 0:
        actions.append(action_button("rearm", task_id, "Re-arm", "rotate", "", "Reset the alert count and watch again"))
    actions.append(action_button("test", task_id, "Test", "send", "", "Send a test alert to this watcher's channels"))
    actions.append(action_button(
        "delete", task_id, "Delete", "trash", "danger", "Delete this watcher",
        confirm=f"BookTick stops watching for “{watcher_title(t)}” ({t.get('user_label') or 'Watcher'}). This can't be undone.",
    ))

    pulse = '<span class="bt-pulse"></span>' if status == "Active" else ""
    return f"""
<article class="bt-card" data-status="{esc(status)}">
  <div class="bt-card-head">
    <span class="bt-badge s-{tone}">{pulse}{label}</span>
    <span class="bt-owner">{icon("user")}{owner}</span>
  </div>
  <div>
    <h3 class="bt-card-title">{movie}</h3>
    <div class="bt-card-cinema">{icon("pin")}{cinema_html}</div>
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


def watchers_html():
    with tasks_lock:
        items = [(tid, dict(t)) for tid, t in app_state["tasks"].items()]
    if not items:
        return f"""
<div class="bt-empty">
  {logo_svg("empty", "bt-logo-lg")}
  <h3>No watchers yet</h3>
  <p>Set one up and {APP_NAME} will keep checking District for you, day and night.</p>
  <button type="button" class="bt-btn primary" data-bt-goto="new">{icon("plus")}<span>Create a watcher</span></button>
</div>"""

    counts = {k: sum(1 for _, t in items if t.get("status") == k) for k in ("Active", "Paused", "Done")}
    counts["all"] = len(items)
    chips = "".join(
        f'<button type="button" class="bt-chip" data-bt-filter="{key}">{label}<b>{counts[key]}</b></button>'
        for key, label in FILTERS
    )
    banner = ""
    if app_state["paused"]:
        banner = callout(
            "warn", "Checking is paused for everyone",
            'No watcher is checked until the engine resumes. '
            f'<button type="button" class="bt-link" data-bt-action="engine">{icon("play")}Resume now</button>',
        )
    cards = "".join(watcher_card(tid, t) for tid, t in reversed(items))  # newest first
    empties = "".join(f'<div class="bt-filter-empty" data-for="{k}">{text}</div>'
                      for k, text in EMPTY_FILTER_TEXT.items() if counts[k] == 0)
    return f"""
<div class="bt-watchers">
  <div class="bt-toolbar">
    <div class="bt-chips" role="group" aria-label="Filter watchers">{chips}</div>
    <button type="button" class="bt-btn primary" data-bt-goto="new">{icon("plus")}<span>New watcher</span></button>
  </div>
  {banner}
  <div class="bt-grid">{cards}</div>
  {empties}
</div>"""


# --- Activity log -------------------------------------------------------------
LOG_TONES = (("🔥", "hot"), ("⚠", "warn"), ("❌", "warn"), ("Engine error", "warn"), ("✅", "ok"),
             ("➕", "info"), ("🔄", "info"), ("🧪", "info"), ("▶", "info"), ("⏸", "muted"), ("🗑", "muted"))


def logs_html():
    with log_lock:
        entries = list(reversed(app_state["logs"]))
    if not entries:
        return f'<div class="bt-log-empty">{icon("activity")}<span>Nothing yet. Checks, alerts and changes show up here.</span></div>'
    rows = []
    for entry in entries:
        m = re.match(r"^\[([^\]]+)\] (.*)$", entry, re.S)
        stamp, msg = (m.group(1), m.group(2)) if m else ("", entry)
        tone = "sub" if msg.startswith("   ") else next((t for key, t in LOG_TONES if key in msg), "")
        rows.append(f'<li class="bt-log-row {tone}" data-line="{esc(entry)}">'
                    f'<time>{esc(stamp)}</time><span>{esc(msg.strip())}</span></li>')
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


def movie_groups(shows, matched_ids, tag=""):
    groups = {}
    for s in shows:
        groups.setdefault(s["title"], []).append(s)
    out = []
    for title, group in groups.items():
        chips = "".join(show_chip(s, id(s) in matched_ids) for s in group)
        out.append(f'<section class="bt-movie"><header><h4>{esc(title)}</h4>{tag}'
                   f'<span class="bt-count">{plural(len(group), "show")}</span></header>'
                   f'<div class="bt-shows">{chips}</div></section>')
    return "".join(out)


# ---------------------------------------------------------------------------
# UI actions
# ---------------------------------------------------------------------------
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


NO_CINEMA_HELP = "Pick a cinema from the list, or paste its link from district.in."


def hint(text):
    return f'<div class="bt-hint-line">{icon("info")}<span>{text}</span></div>'


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


def movie_update(cinemas, cinema, picked):
    """New movie suggestions that keep the movies already picked.

    Gradio 4.44 remembers multiselect picks by their position in the list, so the picks
    are sent again with the new list. Otherwise they would shift onto other movies.
    """
    picked = [m for m in (picked or []) if str(m).strip()]
    return gr.update(choices=movie_choices(cinemas, cinema), value=picked)


def on_city_change(city, picked):
    """New city: list its cinemas. The cinema has to be picked again."""
    try:
        cinemas = city_cinemas(city)
    except Exception as e:
        info = callout("err", f"Couldn't load cinemas in {esc(city_label(city))}",
                       f"{esc(str(e))}. You can paste a cinema link below instead.")
        return gr.update(choices=[], value=None), gr.update(), info, gr.update(open=True), ""
    choices = [(cinema_label(c, city), c["url"]) for c in cinemas]
    info = hint(f"{plural(len(cinemas), 'cinema')} in {esc(city_label(city))}. Pick one to see what it's showing.")
    return gr.update(choices=choices, value=None), movie_update(cinemas, None, picked), info, gr.update(), ""


def on_cinema_change(city, url, picked):
    """Cinema picked: use its link and put its movies at the top of the movie list."""
    if not url:
        return "", gr.update(), ""
    try:
        cinemas = city_cinemas(city)
    except Exception:
        cinemas = []
    cinema = next((c for c in cinemas if c["url"] == url), None)
    return url, movie_update(cinemas, cinema, picked), cinema_card(cinema)


def on_link_input(url, city, picked):
    """A pasted link: select that cinema in the list when it's in the chosen city."""
    cid = cinema_id_from_url(url)
    try:
        cinemas = city_cinemas(city)
    except Exception:
        cinemas = []
    cinema = next((c for c in cinemas if cid and c["id"] == cid), None)
    if cinema:
        return gr.update(value=cinema["url"]), movie_update(cinemas, cinema, picked), cinema_card(cinema)
    clean = clean_cinema_url(url)
    info = hint(f"Using the pasted link: <b>{esc(cinema_name_from_url(clean))}</b>") if clean else ""
    return gr.update(value=None), gr.update(), info


def load_directory(city, url, picked):
    """On page load: fill the city list, that city's cinemas, and the movie suggestions."""
    city = city or DEFAULT_CITY
    cities = gr.update(choices=city_choices(), value=city)
    try:
        cinemas = city_cinemas(city)
    except Exception as e:
        info = callout("warn", "Couldn't load the cinema list right now",
                       f"{esc(str(e))}. The link below still works.")
        return cities, gr.update(), gr.update(), info
    cid = cinema_id_from_url(url)
    cinema = next((c for c in cinemas if cid and c["id"] == cid), None)
    choices = [(cinema_label(c, city), c["url"]) for c in cinemas]
    return (cities, gr.update(choices=choices, value=cinema["url"] if cinema else None),
            movie_update(cinemas, cinema, picked), cinema_card(cinema))


def preview_shows(cin_url, movie_names, fmt_val, date_val):
    movie_names = movie_text(movie_names)
    url = clean_cinema_url(cin_url)
    if not url:
        return callout("err", "Which cinema?", NO_CINEMA_HELP)
    date = normalize_date(date_val)
    if not date:
        return callout("err", "Pick a show date", "Choose a day from the calendar.")
    try:
        cinema_name, shows = parse_district_shows(query_url(url, date))
    except Exception as e:
        return callout("err", "Couldn't load that page", esc(str(e)))

    listed = [s for s in shows if on_date(s, date)]
    title = (f'<div><div class="bt-eyebrow">{icon("pin")}Showtimes</div><h3>{esc(cinema_name)}</h3>'
             f'<p>{esc(date_label(date))} · {plural(len(listed), "show")} listed</p></div>')
    if not listed:
        note = callout("info", f"No shows are listed for {esc(date_label(date))} yet",
                       "That's normal before bookings open. A watcher will keep checking.")
        return f'<div class="bt-preview"><div class="bt-preview-head">{title}</div>{note}</div>'

    keywords = split_keywords(movie_names)
    matched = [s for s in listed if show_matches(s, keywords, fmt_val)]
    matched_ids = {id(s) for s in matched}
    bookable = [s for s in matched if seats_open(s)]
    others = [s for s in listed if id(s) not in matched_ids]

    head = (f'<div class="bt-preview-head">{title}<div class="bt-preview-stats">'
            f'<span><b>{len(matched)}</b>match</span><span><b>{len(bookable)}</b>with seats</span></div></div>')
    if bookable:
        note = callout("hot", "Tickets are already open for this",
                       "A watcher would alert you on its very first check.")
    elif matched:
        note = callout("warn", "Every matching show is sold out right now",
                       "A watcher will alert you if seats free up.")
    else:
        movies = "".join(f'<span class="bt-tag">{esc(t)}</span>' for t in sorted({s["title"] for s in listed}))
        formats = "".join(f'<span class="bt-tag">{esc(str(f))}</span>' for f in sorted({str(s["format"]) for s in listed}))
        note = callout("info", "Nothing matches your filters yet",
                       f'<div class="bt-kv"><b>Movies here</b><div class="bt-tags">{movies}</div></div>'
                       f'<div class="bt-kv"><b>Formats</b><div class="bt-tags">{formats}</div></div>')

    body = ""
    if matched:
        body += movie_groups(matched, matched_ids, '<span class="bt-pill">Matches your filters</span>')
    if others:
        body += (f'<details class="bt-others"{"" if matched else " open"}><summary>'
                 f'Other shows at this cinema <span class="bt-count">{len(others)}</span></summary>'
                 f'{movie_groups(others, matched_ids)}</details>')
    return f'<div class="bt-preview">{head}{note}{body}</div>'


def test_user_notification(user_name, cin_url, ntfy_top, tg_tok, tg_chat, disc_url):
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return callout("err", "Can't send a test yet", error)
    task = dict(channels, user_label=(user_name or "").strip() or "Test")
    url = clean_cinema_url(cin_url)
    cinema = cinema_name_from_url(url) if url else "Sample Cinema"
    results = send_task_notification(task, sample_shows(url), cinema, test=True)
    if all(ok for _, ok, _ in results):
        return callout("ok", "Test alert sent. Check your phone.", esc(describe_results(results)))
    return callout("warn", "Some alerts didn't go through", esc(describe_results(results)))


def add_new_watcher(user_name, cin_url, movie_names, fmt_val, date_val, max_alerts_val, ntfy_top, tg_tok, tg_chat, disc_url):
    keep = gr.update()
    movie_names = movie_text(movie_names)
    url = clean_cinema_url(cin_url)
    if not url:
        return callout("err", "Which cinema?", NO_CINEMA_HELP), keep, keep
    date = normalize_date(date_val)
    if not date:
        return callout("err", "Pick a show date", "Choose a day from the calendar."), keep, keep
    channels, error = clean_channels(ntfy_top, tg_tok, tg_chat, disc_url)
    if error:
        return callout("err", "Where should we alert you?", error), keep, keep

    task_id = "w_" + uuid.uuid4().hex[:6]
    name = (user_name or "").strip() or f"User {len(app_state['tasks']) + 1}"
    task = dict(
        channels,
        id=task_id,
        user_label=name,
        cinema_url=url,
        cinema_name=cinema_name_from_url(url),
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
    add_log(f"➕ [{name}] watching for {movie} ({task['format_filter']}) on {date_label(date)} via {' · '.join(channel_names(task))}.")
    msg = callout(
        "ok", f"{esc(name)}'s watcher is live",
        f"Watching for <b>{esc(movie)}</b> ({esc(task['format_filter'])}) on <b>{esc(date_label(date))}</b>. "
        f'The first check runs now. <button type="button" class="bt-link" data-bt-goto="watchers">'
        f'See it in Watchers{icon("arrow")}</button>',
    )
    return msg, watchers_html(), status_html()


def toggle_watcher(task_id):
    with tasks_lock:
        task = app_state["tasks"].get(task_id)
        if not task:
            return False, "That watcher no longer exists."
        status = task.get("status")
        if status == "Done":
            return False, "This watcher already sent all its alerts. Re-arm it to start again."
        task["status"] = "Paused" if status == "Active" else "Active"
        save_tasks()
        name, new_status, title = task.get("user_label"), task["status"], watcher_title(task)
    wake_engine.set()
    paused = new_status == "Paused"
    add_log(f"{'⏸' if paused else '▶'} [{name}] {'paused' if paused else 'resumed'}.")
    return True, f"{'Paused' if paused else 'Resumed'} the {title} watcher."


def rearm_watcher(task_id):
    with tasks_lock:
        task = app_state["tasks"].get(task_id)
        if not task:
            return False, "That watcher no longer exists."
        task.update(alert_count=0, status="Active", last_result="", last_checked="", last_checked_ts=None)
        save_tasks()
        name, title = task.get("user_label"), watcher_title(task)
    wake_engine.set()
    add_log(f"🔄 [{name}] re-armed: alert count reset and watching again.")
    return True, f"Re-armed the {title} watcher. It's watching again."


def delete_watcher(task_id):
    with tasks_lock:
        task = app_state["tasks"].pop(task_id, None)
        if task:
            save_tasks()
    if not task:
        return False, "That watcher no longer exists."
    add_log(f"🗑️ [{task.get('user_label')}] watcher deleted.")
    return True, f"Deleted the {watcher_title(task)} watcher."


def test_watcher(task_id):
    with tasks_lock:
        task = app_state["tasks"].get(task_id)
        snapshot = dict(task) if task else None
    if not snapshot:
        return False, "That watcher no longer exists."
    cinema = snapshot.get("cinema_name") or cinema_name_from_url(snapshot.get("cinema_url", ""))
    results = send_task_notification(snapshot, sample_shows(snapshot.get("cinema_url")), cinema, test=True)
    summary = describe_results(results)
    add_log(f"🧪 [{snapshot.get('user_label')}] test alert: {summary}")
    if results and all(ok for _, ok, _ in results):
        return True, f"Test alert sent: {summary}"
    return False, f"Some test alerts failed: {summary}"


def engine_note_html():
    if app_state["paused"]:
        return callout("warn", "Paused for everyone", "No watcher is being checked until you resume.")
    return callout("ok", "Running", f"Every active watcher is checked every {app_state['interval_sec']} seconds.")


def engine_button_update():
    if app_state["paused"]:
        return gr.update(value="Resume checking", variant="primary")
    return gr.update(value="Pause all checking", variant="secondary")


def flip_engine():
    app_state["paused"] = not app_state["paused"]
    wake_engine.set()
    if app_state["paused"]:
        add_log("⏸ Engine paused. No watcher is being checked.")
        return "Paused. No watcher is being checked until you resume."
    add_log("▶ Engine resumed.")
    return "Running. Checks resumed."


def run_watcher_action(payload):
    """Handles the buttons inside the HTML cards (they send 'action:watcher_id')."""
    action, _, task_id = str(payload or "").partition(":")
    keep = gr.update()
    engine_outputs = (keep, keep)
    if action == "engine":
        toast(True, flip_engine())
        engine_outputs = (engine_note_html(), engine_button_update())
    elif action in ("pause", "rearm", "delete", "test"):
        handler = {"pause": toggle_watcher, "rearm": rearm_watcher, "delete": delete_watcher, "test": test_watcher}[action]
        ok, msg = handler(task_id)
        toast(ok, msg)
    else:
        return keep, keep, keep, keep
    return (watchers_html(), status_html()) + engine_outputs


def toggle_engine():
    toast(True, flip_engine())
    return engine_note_html(), engine_button_update(), status_html(), watchers_html()


def change_interval(val):
    app_state["interval_sec"] = max(5, int(val))
    add_log(f"⏱️ Check interval set to {app_state['interval_sec']}s.")
    toast(True, f"Checking every {app_state['interval_sec']} seconds.")
    return engine_note_html(), status_html()


def clear_logs():
    with log_lock:
        app_state["logs"].clear()
    return logs_html()


def poll_status():
    return status_html(), watchers_html(), logs_html()


def on_page_load():
    return (
        today_ist(),
        gr.update(value=app_state["interval_sec"]),
        watchers_html(),
        status_html(),
        engine_note_html(),
        engine_button_update(),
        logs_html(),
    )


# ---------------------------------------------------------------------------
# Look and feel
# ---------------------------------------------------------------------------
BRAND_HUE = gr.themes.Color(
    c50="#fff4f1", c100="#ffe6df", c200="#ffcdbf", c300="#ffa892", c400="#ff7b5e", c500="#f9573a",
    c600="#e33d22", c700="#bf2f18", c800="#9d2a19", c900="#82281b", c950="#470f08", name="booktick",
)
GRADIENT = "linear-gradient(135deg, #ff6b4a 0%, #f43f7a 100%)"
GRADIENT_HOVER = "linear-gradient(135deg, #ff5a36 0%, #ec2d6c 100%)"

theme = gr.themes.Soft(
    primary_hue=BRAND_HUE,
    secondary_hue="slate",
    neutral_hue="slate",
    radius_size=gr.themes.sizes.radius_lg,
    font=[gr.themes.GoogleFont("Plus Jakarta Sans"), "ui-sans-serif", "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "Consolas", "monospace"],
).set(
    body_background_fill="#f4f4f9",
    body_background_fill_dark="#0b0c16",
    body_text_color="#161729",
    body_text_color_dark="#ececf4",
    body_text_color_subdued="#686a82",
    body_text_color_subdued_dark="#9a9cb5",
    background_fill_primary="#ffffff",
    background_fill_primary_dark="#151624",
    background_fill_secondary="#f7f7fb",
    background_fill_secondary_dark="#1c1d2e",
    border_color_primary="#e8e8f0",
    border_color_primary_dark="#2a2b40",
    block_background_fill="#ffffff",
    block_background_fill_dark="#151624",
    block_border_color="#e8e8f0",
    block_border_color_dark="#2a2b40",
    block_shadow="none",
    block_shadow_dark="none",
    block_label_text_weight="600",
    block_title_text_weight="650",
    block_title_background_fill="transparent",
    block_title_background_fill_dark="transparent",
    block_title_text_color="#161729",
    block_title_text_color_dark="#ececf4",
    block_title_padding="0",
    block_title_radius="0",
    block_info_text_color="#686a82",
    block_info_text_color_dark="#9a9cb5",
    input_background_fill="#f7f7fb",
    input_background_fill_dark="#1c1d2e",
    # A crisp 1px edge on every input, so focus reads as a sharp brand outline with a faint
    # halo instead of a soft blur (the Soft theme has no input border at all).
    input_border_width="1px",
    input_border_width_dark="1px",
    input_border_color="#e3e3ec",
    input_border_color_dark="#2c2d44",
    input_border_color_hover="#cdcedc",
    input_border_color_hover_dark="#3d3e5a",
    input_border_color_focus="#f9573a",
    input_border_color_focus_dark="#ff6b4a",
    input_shadow="none",
    input_shadow_dark="none",
    input_shadow_focus="0 0 0 3px rgba(249, 87, 58, .12)",
    input_shadow_focus_dark="0 0 0 3px rgba(255, 107, 74, .16)",
    button_large_radius="12px",
    button_small_radius="10px",
    button_primary_background_fill=GRADIENT,
    button_primary_background_fill_dark=GRADIENT,
    button_primary_background_fill_hover=GRADIENT_HOVER,
    button_primary_background_fill_hover_dark=GRADIENT_HOVER,
    button_primary_text_color="#ffffff",
    button_primary_text_color_dark="#ffffff",
    button_primary_border_color="transparent",
    button_primary_border_color_dark="transparent",
    button_secondary_background_fill="#ffffff",
    button_secondary_background_fill_dark="#1c1d2e",
    button_secondary_background_fill_hover="#f4f4f9",
    button_secondary_background_fill_hover_dark="#24253a",
    button_secondary_border_color="#e3e3ec",
    button_secondary_border_color_dark="#30314a",
    button_secondary_text_color="#161729",
    button_secondary_text_color_dark="#ececf4",
    slider_color="#f9573a",
    slider_color_dark="#f9573a",
    color_accent_soft="#ffe6df",
    color_accent_soft_dark="rgba(249, 87, 58, .16)",
)

CSS = """
:root {
  --bt-brand: #f9573a; --bt-brand-strong: #d93a20; --bt-grad: linear-gradient(135deg, #ff6b4a 0%, #f43f7a 100%);
  --bt-ink: #12132a; --bt-surface: #ffffff; --bt-surface-2: #f6f6fa; --bt-line: #e8e8f0;
  --bt-text: #161729; --bt-muted: #686a82; --bt-on-bg: #12132a; --bt-on-fg: #ffffff;
  --bt-ok: #15803d; --bt-ok-bg: #e5f7ec; --bt-warn: #b45309; --bt-warn-bg: #fff3d9;
  --bt-done: #4f46e5; --bt-done-bg: #ecebfe; --bt-hot: #d4361c; --bt-hot-bg: #ffebe5;
  --bt-err: #c81e3a; --bt-err-bg: #ffe8ec; --bt-info: #2563eb; --bt-info-bg: #e8f0fe;
  --bt-shadow: 0 1px 2px rgba(18, 19, 42, .05), 0 10px 28px -16px rgba(18, 19, 42, .18);
  --bt-pop-bg: #ffffff; --bt-pop-line: #e6e6ef; --bt-hover: #f3f3f8; --bt-chip-bg: #ffffff; --bt-scroll: #cfd0dc;
  --bt-pop-shadow: 0 1px 2px rgba(18, 19, 42, .05), 0 12px 32px -8px rgba(18, 19, 42, .18);
  --bt-radius: 18px;
  --bt-chevron: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  --bt-check: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20 6 9 17l-5-5'/%3E%3C/svg%3E");
}
.dark {
  --bt-surface: #151624; --bt-surface-2: #1c1d2e; --bt-line: #2a2b40; --bt-text: #ececf4; --bt-muted: #9a9cb5;
  --bt-on-bg: #f1f1f8; --bt-on-fg: #12132a;
  --bt-ok: #4ade80; --bt-ok-bg: rgba(74, 222, 128, .12); --bt-warn: #fbbf24; --bt-warn-bg: rgba(251, 191, 36, .12);
  --bt-done: #a5b4fc; --bt-done-bg: rgba(129, 140, 248, .16); --bt-hot: #ff8a6e; --bt-hot-bg: rgba(249, 87, 58, .15);
  --bt-err: #fb7185; --bt-err-bg: rgba(244, 63, 94, .14); --bt-info: #93c5fd; --bt-info-bg: rgba(96, 165, 250, .14);
  --bt-shadow: 0 1px 2px rgba(0, 0, 0, .4), 0 12px 30px -18px rgba(0, 0, 0, .7);
  --bt-pop-bg: #1b1c2d; --bt-pop-line: #2c2d44; --bt-hover: rgba(255, 255, 255, .06); --bt-chip-bg: #24253a; --bt-scroll: #3d3e5a;
  --bt-pop-shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 16px 40px -10px rgba(0, 0, 0, .75);
}

.gradio-container { max-width: 1200px !important; margin: 0 auto !important; }
.bt-hidden { display: none !important; }
.bt-i { width: 1em; height: 1em; flex: none; fill: none; stroke: currentColor; stroke-width: 2;
        stroke-linecap: round; stroke-linejoin: round; }
.bt-root-html, .bt-root-html > .prose { padding: 0 !important; }

/* ---------- header ---------- */
.bt-hero { position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between;
  gap: 18px; flex-wrap: wrap; padding: 22px 26px; border-radius: 22px; color: #fff;
  background: radial-gradient(110% 160% at 0% 0%, rgba(255, 107, 74, .38) 0%, rgba(255, 107, 74, 0) 52%),
              radial-gradient(90% 140% at 100% 100%, rgba(244, 63, 122, .32) 0%, rgba(244, 63, 122, 0) 58%), #12132a;
  box-shadow: 0 18px 40px -24px rgba(18, 19, 42, .7); }
.bt-hero::after { content: ""; position: absolute; inset: 0; pointer-events: none; opacity: .5;
  background-image: radial-gradient(rgba(255, 255, 255, .09) 1px, transparent 1px); background-size: 18px 18px;
  mask-image: linear-gradient(90deg, transparent, #000 60%); -webkit-mask-image: linear-gradient(90deg, transparent, #000 60%); }
.bt-brand { position: relative; z-index: 1; display: flex; align-items: center; gap: 14px; min-width: 0; }
.bt-logo { width: 54px; height: 54px; flex: none; filter: drop-shadow(0 8px 18px rgba(249, 87, 58, .45)); }
.bt-wordmark { font-size: 1.85rem; font-weight: 800; letter-spacing: -.03em; line-height: 1.05; color: #fff; }
.bt-wordmark span { background: linear-gradient(135deg, #ff8a6b 0%, #ff5c93 100%); -webkit-background-clip: text;
  background-clip: text; color: transparent; }
.bt-hero .bt-tagline { margin: 6px 0 0; color: rgba(255, 255, 255, .72); font-size: .98rem; line-height: 1.4; }
.bt-gh { position: relative; z-index: 1; display: inline-flex; align-items: center; gap: 10px; padding: 6px 14px 6px 6px;
  border-radius: 999px; background: rgba(255, 255, 255, .08); border: 1px solid rgba(255, 255, 255, .16);
  color: #fff !important; text-decoration: none !important; transition: background .2s, border-color .2s; }
.bt-gh:hover { background: rgba(255, 255, 255, .15); border-color: rgba(255, 255, 255, .3); }
.bt-gh img { width: 36px; height: 36px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, .25); margin: 0; }
.bt-gh-text { display: flex; flex-direction: column; line-height: 1.15; }
.bt-gh-text small { font-size: .66rem; letter-spacing: .09em; text-transform: uppercase; color: rgba(255, 255, 255, .6); }
.bt-gh-text b { font-size: .92rem; font-weight: 700; color: #fff; }
.bt-gh-mark { width: 20px; height: 20px; flex: none; opacity: .9; }
.bt-hero-side { position: relative; z-index: 1; display: flex; align-items: center; gap: 10px; }
.bt-theme { width: 48px; height: 48px; flex: none; display: grid; place-items: center; margin: 0; padding: 0; border-radius: 50%;
  background: rgba(255, 255, 255, .08); border: 1px solid rgba(255, 255, 255, .16); color: #fff; cursor: pointer;
  transition: background .2s, border-color .2s; }
.bt-theme:hover { background: rgba(255, 255, 255, .15); border-color: rgba(255, 255, 255, .3); }
.bt-theme:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
.bt-theme .bt-i { font-size: 19px; }
.bt-theme .bt-i-sun, .dark .bt-theme .bt-i-moon { display: none; }
.dark .bt-theme .bt-i-sun { display: block; }

/* ---------- stats ---------- */
.bt-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.bt-stat { position: relative; display: flex; flex-wrap: wrap; align-items: flex-start; gap: 12px; padding: 14px 16px; min-width: 0;
  background: var(--bt-surface); border: 1px solid var(--bt-line); border-radius: var(--bt-radius); box-shadow: var(--bt-shadow); }
.bt-stat-icon { width: 40px; height: 40px; border-radius: 12px; display: grid; place-items: center; font-size: 19px; flex: none; }
.bt-stat-body { flex: 1; min-width: 0; }
.tone-brand { background: var(--bt-hot-bg); color: var(--bt-brand); }
.tone-ok { background: var(--bt-ok-bg); color: var(--bt-ok); }
.tone-hot { background: var(--bt-done-bg); color: var(--bt-done); }
.tone-info { background: var(--bt-info-bg); color: var(--bt-info); }
.bt-stat-label { font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--bt-muted); }
.bt-stat-value { display: flex; align-items: center; gap: 8px; margin: 2px 0 1px; font-size: 1.4rem; font-weight: 800;
  letter-spacing: -.02em; line-height: 1.25; color: var(--bt-text); white-space: nowrap; }
.bt-stat-sub { font-size: .78rem; color: var(--bt-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bt-live { width: 10px; height: 10px; border-radius: 50%; flex: none; }
.bt-live.on { background: #22c55e; animation: bt-ring 1.8s ease-out infinite; }
.bt-live.off { background: #f59e0b; box-shadow: 0 0 0 4px rgba(245, 158, 11, .2); }
.bt-mini { position: absolute; top: 12px; right: 12px; display: inline-flex; align-items: center; gap: 5px; height: 28px; padding: 0 10px;
  border-radius: 8px; border: 1px solid var(--bt-line); background: var(--bt-surface-2); color: var(--bt-text);
  font: inherit; font-size: .76rem; font-weight: 700; cursor: pointer; transition: background .15s, border-color .15s; }
.bt-mini:hover { border-color: var(--bt-brand); color: var(--bt-brand); }
@keyframes bt-ring { 0% { box-shadow: 0 0 0 0 rgba(34, 197, 94, .55); } 75%, 100% { box-shadow: 0 0 0 8px rgba(34, 197, 94, 0); } }

/* ---------- tabs ---------- */
#bt-tabs { margin-top: 4px; }
#bt-tabs > .tab-nav { display: flex; gap: 4px; width: fit-content; max-width: 100%; overflow-x: auto; padding: 5px;
  border: 1px solid var(--bt-line) !important; border-radius: 14px; background: var(--bt-surface); box-shadow: var(--bt-shadow); }
#bt-tabs > .tab-nav button { flex: none; margin: 0; padding: 8px 18px; border: none !important; border-radius: 10px !important;
  background: transparent; color: var(--bt-muted); font-weight: 650; font-size: .92rem; transition: background .15s, color .15s; }
#bt-tabs > .tab-nav button:hover { color: var(--bt-text); background: var(--bt-surface-2); }
#bt-tabs > .tab-nav button.selected { background: var(--bt-on-bg); color: var(--bt-on-fg); }
#bt-tabs > .tabitem { padding: 16px 0 0 !important; border: none !important; background: transparent !important; }

/* ---------- form steps ---------- */
.bt-step { padding: 18px !important; gap: 14px !important; background: var(--bt-surface) !important;
  border: 1px solid var(--bt-line) !important; border-radius: var(--bt-radius) !important; box-shadow: var(--bt-shadow); }
.bt-step .form { gap: 14px !important; border: none !important; background: transparent !important; box-shadow: none !important; overflow: visible !important; }
.bt-step .form > .block, .bt-step > .block { padding: 0 !important; border: none !important; background: transparent !important; box-shadow: none !important; }
.bt-step .row { gap: 14px !important; }
.bt-step-head { display: flex; align-items: flex-start; gap: 12px; }
.bt-step-num { width: 32px; height: 32px; flex: none; display: grid; place-items: center; border-radius: 10px;
  background: var(--bt-grad); color: #fff; font-weight: 800; font-size: .9rem; box-shadow: 0 6px 14px -8px rgba(244, 63, 122, .9); }
.bt-step-num .bt-i { font-size: 17px; }
.bt-step-head h3 { margin: 0 !important; font-size: 1.08rem; font-weight: 750; color: var(--bt-text); letter-spacing: -.01em; }
.bt-step-head p { margin: 2px 0 0 !important; font-size: .86rem; color: var(--bt-muted); }
.bt-stack { gap: 16px !important; }
#bt-channel-tabs > .tab-nav { display: flex; gap: 4px; width: fit-content; max-width: 100%; padding: 4px; border: none !important;
  border-radius: 12px; background: var(--bt-surface-2); }
#bt-channel-tabs > .tab-nav button { margin: 0; padding: 6px 14px; border: none !important; border-radius: 9px !important;
  background: transparent; color: var(--bt-muted); font-weight: 650; font-size: .86rem; }
#bt-channel-tabs > .tab-nav button.selected { background: var(--bt-surface); color: var(--bt-text); box-shadow: 0 1px 3px rgba(18, 19, 42, .14); }
#bt-channel-tabs > .tabitem { padding: 12px 0 0 !important; border: none !important; background: transparent !important; }
.bt-cinema { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: 12px;
  background: var(--bt-surface-2); border: 1px solid var(--bt-line); font-size: .85rem; color: var(--bt-muted); }
.bt-cinema > .bt-i { font-size: 16px; color: var(--bt-brand); }
.bt-cinema b { font-weight: 700; color: var(--bt-text); }
.bt-cinema-link { display: inline-flex; align-items: center; gap: 5px; margin-left: auto; flex: none; white-space: nowrap;
  color: var(--bt-brand) !important; font-weight: 700; font-size: .8rem; text-decoration: none !important; }
.bt-cinema-link:hover { text-decoration: underline !important; }

/* ---------- dropdowns (Gradio's, restyled) ---------- */
/* .wrap:has(> .wrap-inner) is the input box; a plain .wrap would also catch Gradio's loading overlay. */
.bt-dd .wrap:has(> .wrap-inner) { border-color: var(--input-border-color) !important; transition: border-color .15s, box-shadow .15s; }
.bt-dd .wrap:has(> .wrap-inner):hover { border-color: var(--input-border-color-hover) !important; }
.bt-dd .wrap:has(> .wrap-inner):focus-within { border-color: var(--input-border-color-focus) !important; box-shadow: var(--input-shadow-focus) !important; }
.bt-dd .wrap-inner { min-height: 42px; padding: 3px 8px 3px 12px !important; gap: 6px !important; }
.bt-dd .secondary-wrap input { margin: 0 !important; padding: 6px 0 !important; text-overflow: ellipsis; }
.bt-dd .secondary-wrap input::placeholder { color: var(--bt-muted); }
.bt-dd:not(#bt-movie) input { padding-right: 40px !important; }
/* Gradio's filled triangle, swapped for the same chevron as the date picker. It's drawn on
   ::before so turning it doesn't fight the transform Gradio uses to centre the icon. */
.bt-dd .icon-wrap { width: 20px !important; height: 20px; margin-right: 2px !important; color: var(--bt-muted) !important; transition: color .15s; }
.bt-dd .icon-wrap svg { display: none; }
.bt-dd .icon-wrap::before { content: ""; display: block; width: 100%; height: 100%; background: currentColor; transition: transform .2s ease;
  -webkit-mask: var(--bt-chevron) center / 16px no-repeat; mask: var(--bt-chevron) center / 16px no-repeat; }
.bt-dd .wrap:hover .icon-wrap { color: var(--bt-text) !important; }
/* Gradio only renders the option list while it's open. */
.bt-dd:has(ul.options) .icon-wrap { color: var(--bt-text) !important; }
.bt-dd:has(ul.options) .icon-wrap::before { transform: rotate(180deg); }

/* The open list: 12px panel, 4px inset, 8px rows, so the corners nest. Rows stay neutral;
   the picked one is marked only by weight and a brand check. */
.bt-dd ul.options { margin: 6px 0 !important; padding: 4px !important; border: 1px solid var(--bt-pop-line) !important;
  border-radius: 12px !important; background: var(--bt-pop-bg) !important; box-shadow: var(--bt-pop-shadow) !important; }
.bt-dd ul.options li.item { align-items: center; gap: 10px; margin: 0 !important; padding: 8px 10px !important; border-radius: 8px;
  color: var(--bt-text); font-size: .875rem; line-height: 1.4; }
.bt-dd ul.options li.item:hover, .bt-dd ul.options li.item.active { background: var(--bt-hover) !important; }
.bt-dd ul.options li.item.selected { font-weight: 600; }
/* Gradio's "✓" sits before the text; this turns it into a check mark at the end of the row. */
.bt-dd ul.options .inner-item { order: 2; flex: none; margin-left: auto; padding: 0 !important; width: 14px; height: 14px;
  font-size: 0; color: var(--bt-brand); background: currentColor;
  -webkit-mask: var(--bt-check) center / 14px no-repeat; mask: var(--bt-check) center / 14px no-repeat; }
.bt-dd ul.options .inner-item.hide { display: none; }
.bt-dd ul.options::-webkit-scrollbar, .bt-cal::-webkit-scrollbar { width: 12px; }
.bt-dd ul.options::-webkit-scrollbar-track { margin: 6px 0; background: transparent; }
.bt-dd ul.options::-webkit-scrollbar-thumb { border: 4px solid transparent; border-radius: 999px; background: var(--bt-scroll) padding-box; }
.bt-dd ul.options::-webkit-scrollbar-thumb:hover { background-color: var(--bt-muted); }
@supports not selector(::-webkit-scrollbar) {
  .bt-dd ul.options { scrollbar-width: thin; scrollbar-color: var(--bt-scroll) transparent; }
}
.bt-dd .token { gap: 2px; padding: 2px 3px 2px 9px !important; border: 1px solid var(--bt-pop-line) !important; border-radius: 7px !important;
  background: var(--bt-chip-bg) !important; color: var(--bt-text) !important; box-shadow: 0 1px 1px rgba(18, 19, 42, .04) !important;
  font-size: .82rem !important; font-weight: 600 !important; line-height: 1.5 !important; }
.bt-dd .token > * + * { margin-left: 0 !important; }
.bt-dd .token-remove { width: 18px !important; height: 18px !important; padding: 4px !important; border: none !important; border-radius: 5px !important;
  background: transparent !important; fill: var(--bt-muted) !important; transition: background .15s, fill .15s; }
.bt-dd .token-remove:hover { fill: var(--bt-text) !important; background: var(--bt-hover) !important; }
.bt-dd .token-remove.remove-all { width: 22px !important; height: 22px !important; padding: 6px !important; border-radius: 6px !important; }

/* ---------- show-date calendar ---------- */
.bt-date { position: relative; display: flex; flex-direction: column; }
.bt-field-label { display: block; margin-bottom: var(--spacing-xs); color: var(--block-title-text-color);
  font-weight: var(--block-title-text-weight); font-size: var(--block-title-text-size); line-height: var(--line-sm); }
.bt-field-info { display: block; margin-bottom: var(--spacing-lg); color: var(--block-info-text-color);
  font-weight: var(--block-info-text-weight); font-size: var(--block-info-text-size); line-height: var(--line-sm); }
.bt-date-trigger { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; margin: 0; padding: 0 10px 0 12px;
  border: var(--input-border-width) solid var(--input-border-color); border-radius: var(--input-radius);
  background: var(--input-background-fill); color: var(--body-text-color); font: inherit; font-size: var(--input-text-size);
  text-align: left; cursor: pointer; transition: border-color .15s, box-shadow .15s; }
.bt-date-trigger:hover { border-color: var(--input-border-color-hover); }
.bt-date-trigger:focus-visible, .bt-date.is-open .bt-date-trigger { outline: none; border-color: var(--input-border-color-focus);
  box-shadow: var(--input-shadow-focus); }
.bt-date-trigger > .bt-i { font-size: 17px; color: var(--bt-muted); }
.bt-date-trigger > .bt-i:last-child { font-size: 16px; transition: rotate .2s ease, color .15s; }
.bt-date-trigger:hover > .bt-i:last-child, .bt-date.is-open .bt-date-trigger > .bt-i:last-child { color: var(--bt-text); }
.bt-date.is-open .bt-date-trigger > .bt-i:last-child { rotate: 180deg; }
.bt-date-value { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bt-cal { position: absolute; top: calc(100% + 6px); left: 0; z-index: 60; width: 100%; min-width: 280px;
  max-width: min(340px, calc(100vw - 32px)); padding: 10px; border-radius: 12px; border: 1px solid var(--bt-pop-line);
  background: var(--bt-pop-bg); box-shadow: var(--bt-pop-shadow); animation: bt-pop .14s ease; }
.bt-cal.is-right { left: auto; right: 0; }
.bt-cal[hidden] { display: none !important; }
.bt-cal-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; }
.bt-cal-title { font-size: .9rem; font-weight: 600; color: var(--bt-text); }
.bt-cal-nav { width: 30px; height: 30px; display: grid; place-items: center; margin: 0; padding: 0; border: none; border-radius: 8px;
  background: transparent; color: var(--bt-muted); cursor: pointer; transition: background .15s, color .15s; }
.bt-cal-nav .bt-i { font-size: 16px; }
.bt-cal-nav:hover:not(:disabled) { background: var(--bt-hover); color: var(--bt-text); }
.bt-cal-nav:disabled { opacity: .3; cursor: default; }
.bt-cal-week, .bt-cal-days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 2px; }
.bt-cal-week span { padding: 6px 0; text-align: center; font-size: .68rem; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; color: var(--bt-muted); }
.bt-day { position: relative; height: 36px; margin: 0; padding: 0; border: none; border-radius: 8px; background: transparent;
  color: var(--bt-text); font: inherit; font-size: .85rem; font-variant-numeric: tabular-nums; cursor: pointer;
  transition: background .12s, color .12s; }
.bt-day:hover:not(:disabled):not(.is-selected) { background: var(--bt-hover); }
.bt-day:disabled { color: var(--bt-muted); opacity: .45; cursor: default; }
.bt-day.is-today { color: var(--bt-brand); font-weight: 600; }
.bt-day.is-today::after { content: ""; position: absolute; left: 50%; bottom: 5px; width: 3px; height: 3px; margin-left: -1.5px;
  border-radius: 50%; background: currentColor; }
.bt-day.is-selected { background: var(--bt-brand); color: #fff; font-weight: 600; }
.bt-day:focus-visible, .bt-cal-nav:focus-visible, .bt-cal-chip:focus-visible { outline: 2px solid var(--bt-brand); outline-offset: 1px; }
.bt-cal-quick { display: flex; gap: 6px; margin-top: 8px; padding-top: 10px; border-top: 1px solid var(--bt-pop-line); }
.bt-cal-chip { flex: 1 1 0; min-width: 0; height: 30px; margin: 0; padding: 0 8px; border-radius: 8px; border: 1px solid var(--bt-pop-line);
  background: transparent; color: var(--bt-text); font: inherit; font-size: .78rem; font-weight: 600; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; cursor: pointer; transition: background .15s, border-color .15s, color .15s; }
.bt-cal-chip:hover { background: var(--bt-hover); }
.bt-cal-chip.is-selected { border-color: color-mix(in srgb, var(--bt-brand) 50%, transparent); color: var(--bt-brand);
  background: color-mix(in srgb, var(--bt-brand) 7%, transparent); }
.bt-hint-line { display: flex; align-items: center; gap: 8px; font-size: .84rem; color: var(--bt-muted); }
.bt-hint-line .bt-i { font-size: 15px; color: var(--bt-info); }
.bt-hint-line b { color: var(--bt-text); }
.bt-accordion { padding: 0 !important; border: 1px dashed var(--bt-line) !important; border-radius: 12px !important; background: transparent !important; }
.bt-accordion > .label-wrap { padding: 10px 14px !important; font-size: .86rem; font-weight: 650; color: var(--bt-muted); }
.bt-accordion > .label-wrap:hover { color: var(--bt-text); }
.bt-accordion > div:not(.label-wrap) { padding: 0 14px 14px !important; }
.bt-hint { margin: 0 0 10px !important; font-size: .86rem; color: var(--bt-muted); }
.bt-hint b { color: var(--bt-text); }
.bt-cta-row { gap: 10px !important; }
.bt-cta-row button { min-height: 46px; font-weight: 700 !important; }

/* ---------- callouts ---------- */
.bt-callout { display: flex; align-items: flex-start; gap: 12px; padding: 12px 14px; border-radius: 14px;
  font-size: .9rem; line-height: 1.5; background: var(--bt-surface-2); color: var(--bt-text); }
.bt-callout > .bt-i { font-size: 19px; margin-top: 1px; }
.bt-callout-title { font-weight: 750; }
.bt-callout-body { color: var(--bt-text); opacity: .88; }
.bt-callout code { padding: 1px 6px; border-radius: 6px; background: rgba(127, 127, 160, .16); font-size: .85em; }
.bt-callout.ok { background: var(--bt-ok-bg); } .bt-callout.ok > .bt-i, .bt-callout.ok .bt-callout-title { color: var(--bt-ok); }
.bt-callout.err { background: var(--bt-err-bg); } .bt-callout.err > .bt-i, .bt-callout.err .bt-callout-title { color: var(--bt-err); }
.bt-callout.warn { background: var(--bt-warn-bg); } .bt-callout.warn > .bt-i, .bt-callout.warn .bt-callout-title { color: var(--bt-warn); }
.bt-callout.info { background: var(--bt-info-bg); } .bt-callout.info > .bt-i, .bt-callout.info .bt-callout-title { color: var(--bt-info); }
.bt-callout.hot { background: var(--bt-hot-bg); } .bt-callout.hot > .bt-i, .bt-callout.hot .bt-callout-title { color: var(--bt-hot); }
.bt-link { display: inline-flex; align-items: center; gap: 5px; padding: 0; margin: 0 0 0 4px; border: none; background: none;
  color: var(--bt-brand); font: inherit; font-weight: 700; cursor: pointer; }
.bt-link:hover { text-decoration: underline; }
.bt-kv { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.bt-kv > b { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: var(--bt-muted); }

/* ---------- help + about ---------- */
.bt-help, .bt-about { padding: 20px; border-radius: var(--bt-radius); background: var(--bt-surface);
  border: 1px solid var(--bt-line); box-shadow: var(--bt-shadow); color: var(--bt-text); }
.bt-help h3, .bt-about h3 { margin: 0 !important; font-size: 1.05rem; font-weight: 750; }
.bt-help h4 { margin: 4px 0 10px !important; font-size: .78rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--bt-muted); }
.bt-steps { counter-reset: bt-step; list-style: none !important; padding: 0 !important; margin: 14px 0 18px !important;
  display: flex; flex-direction: column; gap: 14px; }
.bt-steps li { counter-increment: bt-step; display: grid; grid-template-columns: 30px 1fr; column-gap: 12px; row-gap: 2px; margin: 0 !important; padding: 0 !important; }
.bt-steps li::before { content: counter(bt-step); grid-row: span 2; width: 30px; height: 30px; display: grid; place-items: center;
  border-radius: 10px; background: var(--bt-hot-bg); color: var(--bt-brand); font-weight: 800; font-size: .85rem; }
.bt-steps b { font-size: .92rem; font-weight: 700; }
.bt-steps span { font-size: .85rem; line-height: 1.5; color: var(--bt-muted); }
.bt-help a { color: var(--bt-brand); font-weight: 600; }
.bt-tips { list-style: none !important; padding: 0 !important; margin: 0 !important; display: flex; flex-direction: column; gap: 10px; }
.bt-tips li { display: flex; gap: 10px; margin: 0 !important; padding: 10px 12px !important; border-radius: 12px; background: var(--bt-surface-2);
  font-size: .84rem; line-height: 1.5; color: var(--bt-muted); }
.bt-tips li::marker { content: none; }
.bt-tips .bt-i { font-size: 16px; margin-top: 2px; color: var(--bt-brand); }
.bt-tips code { padding: 1px 6px; border-radius: 6px; background: var(--bt-surface); border: 1px solid var(--bt-line); font-size: .85em; color: var(--bt-text); }
.bt-about { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }
.bt-aside { position: sticky; top: 16px; align-self: flex-start; }
.bt-about p { margin: 0 !important; font-size: .9rem; line-height: 1.55; color: var(--bt-muted); }
.bt-logo-md { width: 44px; height: 44px; }
.bt-gh-card { display: flex; align-items: center; gap: 12px; width: 100%; margin-top: 6px; padding: 10px 14px 10px 10px;
  border-radius: 14px; border: 1px solid var(--bt-line); background: var(--bt-surface-2); color: var(--bt-text) !important;
  text-decoration: none !important; transition: border-color .15s, transform .15s; }
.bt-gh-card:hover { border-color: var(--bt-brand); transform: translateY(-1px); }
.bt-gh-card img { width: 44px; height: 44px; border-radius: 50%; margin: 0; }
.bt-gh-card span { display: flex; flex-direction: column; flex: 1; line-height: 1.3; }
.bt-gh-card small { color: var(--bt-muted); font-size: .8rem; }
.bt-gh-card .bt-gh-mark { width: 22px; height: 22px; }

/* ---------- buttons inside HTML ---------- */
.bt-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 36px; padding: 0 12px;
  border-radius: 10px; border: 1px solid var(--bt-line); background: var(--bt-surface); color: var(--bt-text);
  font: inherit; font-size: .84rem; font-weight: 650; white-space: nowrap; cursor: pointer;
  transition: background .15s, border-color .15s, color .15s, transform .05s, filter .15s; }
.bt-btn .bt-i { font-size: 15px; }
.bt-btn:hover { background: var(--bt-surface-2); border-color: color-mix(in srgb, var(--bt-text) 22%, var(--bt-line)); }
.bt-btn:active { transform: translateY(1px); }
.bt-btn:focus-visible, .bt-chip:focus-visible, .bt-mini:focus-visible, .bt-link:focus-visible { outline: 2px solid var(--bt-brand); outline-offset: 2px; }
.bt-btn.primary { background: var(--bt-grad); color: #fff; border-color: transparent; box-shadow: 0 8px 18px -10px rgba(244, 63, 122, .85); }
.bt-btn.primary:hover { filter: brightness(1.06) saturate(1.05); }
.bt-btn.danger { color: var(--bt-err); }
.bt-btn.danger:hover { background: var(--bt-err-bg); border-color: transparent; }
.bt-btn.danger-solid { background: #dc2645; border-color: transparent; color: #fff; }
.bt-btn.danger-solid:hover { background: #c01d3a; }

/* ---------- watchers ---------- */
.bt-watchers { display: flex; flex-direction: column; gap: 14px; }
.bt-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.bt-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.bt-chip { display: inline-flex; align-items: center; gap: 7px; height: 34px; padding: 0 8px 0 14px; border-radius: 999px;
  border: 1px solid var(--bt-line); background: var(--bt-surface); color: var(--bt-muted); font: inherit; font-size: .85rem;
  font-weight: 650; cursor: pointer; transition: background .15s, color .15s, border-color .15s; }
.bt-chip b { min-width: 22px; padding: 1px 7px; border-radius: 999px; background: var(--bt-surface-2); color: var(--bt-text); font-size: .74rem; text-align: center; }
.bt-chip:hover { color: var(--bt-text); }
html:not([data-bt-view]) .bt-chip[data-bt-filter="all"], html[data-bt-view="all"] .bt-chip[data-bt-filter="all"],
html[data-bt-view="Active"] .bt-chip[data-bt-filter="Active"], html[data-bt-view="Paused"] .bt-chip[data-bt-filter="Paused"],
html[data-bt-view="Done"] .bt-chip[data-bt-filter="Done"] {
  background: var(--bt-on-bg) !important; border-color: var(--bt-on-bg) !important; color: var(--bt-on-fg) !important; }
html:not([data-bt-view]) .bt-chip[data-bt-filter="all"] b, html[data-bt-view="all"] .bt-chip[data-bt-filter="all"] b,
html[data-bt-view="Active"] .bt-chip[data-bt-filter="Active"] b, html[data-bt-view="Paused"] .bt-chip[data-bt-filter="Paused"] b,
html[data-bt-view="Done"] .bt-chip[data-bt-filter="Done"] b { background: rgba(127, 127, 160, .28) !important; color: inherit !important; }
/* !important: Gradio adds a higher-specificity copy of every rule, which would otherwise win over these. */
html[data-bt-view="Active"] .bt-card:not([data-status="Active"]), html[data-bt-view="Paused"] .bt-card:not([data-status="Paused"]),
html[data-bt-view="Done"] .bt-card:not([data-status="Done"]) { display: none !important; }
.bt-filter-empty { display: none; padding: 28px; border: 1px dashed var(--bt-line); border-radius: var(--bt-radius);
  text-align: center; color: var(--bt-muted); font-size: .9rem; }
html[data-bt-view="Active"] .bt-filter-empty[data-for="Active"], html[data-bt-view="Paused"] .bt-filter-empty[data-for="Paused"],
html[data-bt-view="Done"] .bt-filter-empty[data-for="Done"] { display: block !important; }

.bt-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.bt-card { position: relative; display: flex; flex-direction: column; gap: 12px; padding: 16px; min-width: 0;
  background: var(--bt-surface); border: 1px solid var(--bt-line); border-radius: var(--bt-radius); box-shadow: var(--bt-shadow);
  transition: border-color .18s, box-shadow .18s, transform .18s, opacity .18s; }
.bt-card:hover { border-color: color-mix(in srgb, var(--bt-brand) 30%, var(--bt-line)); transform: translateY(-1px); }
.bt-card[data-status="Active"]::before { content: ""; position: absolute; left: 16px; right: 16px; top: -1px; height: 3px;
  border-radius: 0 0 4px 4px; background: var(--bt-grad); }
.bt-card.is-busy { opacity: .55; pointer-events: none; }
.bt-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.bt-badge { display: inline-flex; align-items: center; gap: 7px; padding: 4px 10px; border-radius: 999px; font-size: .74rem; font-weight: 750; letter-spacing: .01em; }
.bt-badge.s-active { background: var(--bt-ok-bg); color: var(--bt-ok); }
.bt-badge.s-paused { background: var(--bt-warn-bg); color: var(--bt-warn); }
.bt-badge.s-done { background: var(--bt-done-bg); color: var(--bt-done); }
.bt-pulse { width: 7px; height: 7px; border-radius: 50%; background: currentColor; animation: bt-ring 1.8s ease-out infinite; }
.bt-owner { display: inline-flex; align-items: center; gap: 5px; min-width: 0; font-size: .8rem; color: var(--bt-muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bt-card-title { margin: 0 !important; font-size: 1.18rem !important; font-weight: 800; letter-spacing: -.015em; line-height: 1.25;
  color: var(--bt-text); overflow-wrap: anywhere; }
.bt-card-cinema { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: .85rem; color: var(--bt-muted); min-width: 0; }
.bt-card-cinema a { display: inline-flex; align-items: center; gap: 5px; min-width: 0; color: inherit !important; text-decoration: none !important; }
.bt-card-cinema a:hover { color: var(--bt-brand) !important; }
.bt-card-cinema a .bt-i { font-size: 12px; opacity: .7; }
.bt-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.bt-tag { display: inline-flex; align-items: center; gap: 5px; padding: 4px 9px; border-radius: 8px; border: 1px solid var(--bt-line);
  background: var(--bt-surface-2); color: var(--bt-text); font-size: .78rem; font-weight: 650; }
.bt-tag .bt-i { color: var(--bt-muted); }
.bt-tag.warn { background: var(--bt-warn-bg); border-color: transparent; color: var(--bt-warn); }
.bt-progress-top { display: flex; justify-content: space-between; margin-bottom: 6px; font-size: .78rem; color: var(--bt-muted); }
.bt-progress-top b { color: var(--bt-text); }
.bt-bar { height: 6px; border-radius: 999px; background: var(--bt-surface-2); overflow: hidden; border: 1px solid var(--bt-line); }
.bt-bar i { display: block; height: 100%; border-radius: inherit; background: var(--bt-grad); transition: width .4s ease; }
.bt-last { display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px; border-radius: 12px; background: var(--bt-surface-2);
  font-size: .84rem; color: var(--bt-text); }
.bt-last > .bt-i { font-size: 16px; margin-top: 2px; color: var(--bt-muted); }
.bt-last b { display: block; font-weight: 700; }
.bt-last small { display: block; margin-top: 1px; font-size: .76rem; color: var(--bt-muted); }
.bt-last.hot { background: var(--bt-hot-bg); } .bt-last.hot > .bt-i, .bt-last.hot b { color: var(--bt-hot); }
.bt-last.warn { background: var(--bt-warn-bg); } .bt-last.warn > .bt-i, .bt-last.warn b { color: var(--bt-warn); }
.bt-chans { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: .78rem; color: var(--bt-muted); }
.bt-chans > .bt-i { font-size: 14px; }
.bt-chan { padding: 2px 8px; border-radius: 999px; background: var(--bt-surface-2); border: 1px solid var(--bt-line); font-weight: 650; color: var(--bt-text); }
.bt-chan.none { color: var(--bt-err); }
.bt-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: auto; padding-top: 12px; border-top: 1px solid var(--bt-line); }
.bt-actions .bt-btn.primary { flex: 1 1 auto; }
.bt-empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 20px; text-align: center;
  border: 1px dashed var(--bt-line); border-radius: 22px; background: var(--bt-surface); color: var(--bt-text); }
.bt-empty h3 { margin: 8px 0 0 !important; font-size: 1.2rem; font-weight: 800; }
.bt-empty p { margin: 0 0 10px !important; max-width: 380px; color: var(--bt-muted); }
.bt-logo-lg { width: 64px; height: 64px; filter: drop-shadow(0 12px 24px rgba(249, 87, 58, .35)); }

/* ---------- preview ---------- */
#bt-preview:empty, #bt-add-result:empty { display: none; }
.bt-preview { display: flex; flex-direction: column; gap: 12px; padding: 16px; border-radius: 16px; border: 1px solid var(--bt-line); background: var(--bt-surface-2); }
.bt-preview-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.bt-preview-head h3 { margin: 2px 0 0 !important; font-size: 1.05rem; font-weight: 800; color: var(--bt-text); }
.bt-preview-head p { margin: 2px 0 0 !important; font-size: .84rem; color: var(--bt-muted); }
.bt-eyebrow { display: flex; align-items: center; gap: 5px; font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--bt-brand); }
.bt-preview-stats { display: flex; gap: 8px; }
.bt-preview-stats span { display: flex; flex-direction: column; align-items: center; min-width: 74px; padding: 6px 10px; border-radius: 12px;
  background: var(--bt-surface); border: 1px solid var(--bt-line); font-size: .72rem; color: var(--bt-muted); }
.bt-preview-stats b { font-size: 1.1rem; color: var(--bt-text); }
.bt-movie { display: flex; flex-direction: column; gap: 8px; }
.bt-movie header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.bt-movie h4 { margin: 0 !important; font-size: .95rem; font-weight: 750; color: var(--bt-text); }
.bt-pill { padding: 2px 8px; border-radius: 999px; background: var(--bt-hot-bg); color: var(--bt-hot); font-size: .7rem; font-weight: 750; }
.bt-count { padding: 1px 7px; border-radius: 999px; background: var(--bt-surface); border: 1px solid var(--bt-line); color: var(--bt-muted); font-size: .72rem; font-weight: 650; }
.bt-shows { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.bt-show { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border-radius: 12px; border: 1px solid var(--bt-line);
  background: var(--bt-surface); color: var(--bt-text) !important; text-decoration: none !important; transition: border-color .15s, transform .15s, box-shadow .15s; }
a.bt-show:hover { border-color: var(--bt-brand); transform: translateY(-1px); box-shadow: var(--bt-shadow); }
.bt-show.match { border-color: color-mix(in srgb, var(--bt-brand) 45%, var(--bt-line)); }
.bt-show.soldout { opacity: .55; }
.bt-show-time { font-size: 1rem; font-weight: 800; letter-spacing: -.01em; }
.bt-show-meta { font-size: .74rem; color: var(--bt-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bt-show-seats { display: flex; align-items: center; gap: 5px; font-size: .78rem; font-weight: 650; }
.bt-show-seats .bt-i { color: var(--bt-muted); }
.bt-seatbar { display: block; height: 4px; border-radius: 999px; background: var(--bt-surface-2); overflow: hidden; }
.bt-seatbar i { display: block; height: 100%; border-radius: inherit; background: var(--bt-ok); }
.bt-seatbar.low i { background: var(--bt-warn); }
.bt-show-foot { display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-top: 2px; font-size: .74rem; color: var(--bt-muted); }
.bt-book { display: inline-flex; align-items: center; gap: 3px; color: var(--bt-brand); font-weight: 750; }
.bt-others { border-top: 1px solid var(--bt-line); padding-top: 10px; }
.bt-others summary { display: flex; align-items: center; gap: 8px; cursor: pointer; font-weight: 700; font-size: .88rem; color: var(--bt-muted); list-style: none; }
.bt-others summary::-webkit-details-marker { display: none; }
.bt-others summary::before { content: "›"; display: inline-block; font-size: 1.1rem; transition: transform .15s; }
.bt-others[open] summary::before { transform: rotate(90deg); }
.bt-others[open] > .bt-movie { margin-top: 12px; }

/* ---------- activity ---------- */
.bt-section-title h3 { margin: 0 !important; font-size: 1.05rem; font-weight: 750; color: var(--bt-text); }
.bt-section-title p { margin: 2px 0 0 !important; font-size: .85rem; color: var(--bt-muted); }
.bt-log-bar { align-items: center !important; }
#bt-log { max-height: 560px; overflow: auto; padding: 6px !important; border: 1px solid var(--bt-line) !important;
  border-radius: var(--bt-radius) !important; background: var(--bt-surface) !important; box-shadow: var(--bt-shadow); }
.bt-log-list { list-style: none !important; margin: 0 !important; padding: 0 !important; font-family: var(--font-mono); font-size: .8rem; }
.bt-log-row { display: grid; grid-template-columns: 128px 1fr; gap: 14px; margin: 0 !important; padding: 7px 10px !important; border-radius: 8px; line-height: 1.5; }
.bt-log-row:hover { background: var(--bt-surface-2); }
.bt-log-row time { color: var(--bt-muted); white-space: nowrap; }
.bt-log-row span { color: var(--bt-text); overflow-wrap: anywhere; }
.bt-log-row.hot { background: var(--bt-hot-bg); } .bt-log-row.hot span { color: var(--bt-hot); font-weight: 700; }
.bt-log-row.warn span { color: var(--bt-warn); }
.bt-log-row.ok span { color: var(--bt-ok); }
.bt-log-row.info span { color: var(--bt-info); }
.bt-log-row.muted span, .bt-log-row.sub span { color: var(--bt-muted); }
.bt-log-row.sub span { padding-left: 16px; }
.bt-log-empty { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 40px 16px; color: var(--bt-muted); font-family: var(--font-sans); }
#bt-copy-log.is-copied { color: var(--bt-ok) !important; border-color: var(--bt-ok) !important; }

/* ---------- confirm dialog (lives outside the app, so no media queries here) ---------- */
.bt-shade { position: fixed; inset: 0; z-index: 9998; background: rgba(10, 11, 24, .45); backdrop-filter: blur(2px); animation: bt-fade .15s ease; }
.bt-dialog { position: absolute; z-index: 9999; padding: 20px; border-radius: 18px; background: var(--bt-surface, #fff);
  border: 1px solid var(--bt-line, #e8e8f0); color: var(--bt-text, #161729); font-family: var(--font, system-ui, sans-serif);
  box-shadow: 0 24px 60px -20px rgba(10, 11, 24, .55); animation: bt-pop .16s ease; }
.bt-dialog-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 12px; background: var(--bt-err-bg, #ffe8ec); color: var(--bt-err, #c81e3a); font-size: 20px; }
.bt-dialog h3 { margin: 12px 0 4px; font-size: 1.08rem; font-weight: 800; }
.bt-dialog p { margin: 0; font-size: .9rem; line-height: 1.5; color: var(--bt-muted, #686a82); }
.bt-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
@keyframes bt-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes bt-pop { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }

/* ---------- footer ---------- */
.bt-footer { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 20px; margin-top: 8px;
  padding: 18px 4px 4px; border-top: 1px solid var(--bt-line); color: var(--bt-muted); font-size: .85rem; }
.bt-footer-brand { display: flex; align-items: center; gap: 8px; }
.bt-footer-brand b { color: var(--bt-text); font-weight: 800; }
.bt-logo-sm { width: 24px; height: 24px; }
.bt-footer-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.bt-footer-meta a { display: inline-flex; align-items: center; gap: 7px; color: var(--bt-text) !important; font-weight: 650; text-decoration: none !important; }
.bt-footer-meta a:hover { color: var(--bt-brand) !important; }
.bt-footer-meta img { width: 22px; height: 22px; border-radius: 50%; margin: 0; }
.bt-footer-meta .bt-gh-mark { width: 16px; height: 16px; }
.bt-footer-note { flex-basis: 100%; margin: 0 !important; font-size: .78rem; }

/* ---------- small screens ---------- */
@media (max-width: 900px) {
  .bt-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .bt-hero { padding: 18px; border-radius: 18px; }
  .bt-logo { width: 46px; height: 46px; }
  .bt-wordmark { font-size: 1.55rem; }
  .bt-hero .bt-tagline { font-size: .9rem; }
  .bt-stats { gap: 10px; }
  .bt-stat { padding: 12px; gap: 10px; }
  .bt-stat-icon { width: 34px; height: 34px; font-size: 16px; border-radius: 10px; }
  .bt-stat-value { font-size: 1.15rem; }
  .bt-mini { position: static; flex-basis: 100%; justify-content: center; }
  .bt-stat-sub { white-space: normal; }
  .bt-cinema { flex-wrap: wrap; }
  #bt-tabs > .tab-nav, #bt-channel-tabs > .tab-nav { flex-wrap: nowrap; }
  #bt-tabs > .tab-nav { width: 100%; }
  #bt-tabs > .tab-nav button { flex: 1 1 auto; padding: 8px 6px; font-size: .84rem; }
  #bt-channel-tabs > .tab-nav button { padding: 6px 10px; font-size: .82rem; }
  .bt-grid { grid-template-columns: 1fr; }
  .bt-toolbar > .bt-btn { width: 100%; }
  .bt-log-row { grid-template-columns: 1fr; gap: 0; }
  .bt-shows { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .bt-preview { padding: 12px; }
  .bt-dialog-actions .bt-btn { flex: 1; }
}
"""

FAVICON = "data:image/svg+xml," + quote(logo_svg("fav", "x"))

# The light/dark switch in the header. Gradio picks the theme itself when the page loads
# (and again when the system theme changes), so a saved choice is re-applied on top of it.
THEME_JS = r"""
<script>
(() => {
  if (window.__btTheme) return;
  window.__btTheme = true;
  const KEY = "booktick-theme";
  const saved = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const apply = () => {
    const mode = saved();
    if (!document.body || (mode !== "dark" && mode !== "light")) return;
    const dark = mode === "dark";
    if (document.body.classList.contains("dark") !== dark) document.body.classList.toggle("dark", dark);
  };
  const watch = () => {
    apply();
    new MutationObserver(apply).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  };
  if (document.body) watch(); else document.addEventListener("DOMContentLoaded", watch);
  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t || !t.closest("[data-bt-theme]")) return;
    const next = document.body.classList.contains("dark") ? "light" : "dark";
    try { localStorage.setItem(KEY, next); } catch (err) {}
    document.body.classList.toggle("dark", next === "dark");
  });
})();
</script>
"""

# The show-date calendar (markup from date_picker_html). A plain string, not part of the
# f-string below, so its braces don't need doubling.
DATE_PICKER_JS = r"""
<script>
(() => {
  // The picked date lives in the hidden #bt-date textbox, which the Python handlers read.
  // Dates are "YYYY-MM-DD" strings handled as UTC calendar days, so the browser's own
  // timezone never shifts them, and "today" always means today in India.
  const BT = window.BookTick;
  if (!BT || BT.date) return;
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"];
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTHS_AHEAD = 12;

  const pad = (n) => String(n).padStart(2, "0");
  const toIso = (d) => d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  const fromIso = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "").trim());
    return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
  };
  const today = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);  // IST is UTC+5:30
  const addDays = (s, n) => { const d = fromIso(s); d.setUTCDate(d.getUTCDate() + n); return toIso(d); };
  const monthOf = (s) => s.slice(0, 7);
  const addMonths = (ym, n) => toIso(new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7) - 1 + n, 1))).slice(0, 7);
  const daysIn = (ym) => new Date(Date.UTC(+ym.slice(0, 4), +ym.slice(5, 7), 0)).getUTCDate();
  const short = (s) => {
    const d = fromIso(s);
    return DAYS[d.getUTCDay()].slice(0, 3) + " " + pad(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()].slice(0, 3);
  };
  // Same wording as date_label() in Python.
  const label = (s) => {
    const t = today();
    if (s === t) return "Today · " + short(s);
    if (s === addDays(t, 1)) return "Tomorrow · " + short(s);
    return short(s);
  };

  const field = () => document.querySelector("#bt-date input, #bt-date textarea");
  const picked = () => {
    const el = field();
    return el && fromIso(el.value) ? el.value.trim() : today();
  };
  const showLabel = (root, s) => {
    const out = root.querySelector(".bt-date-value");
    if (out) out.textContent = label(s);
  };

  const render = (root, focus) => {
    const cal = root.querySelector(".bt-cal");
    const t = today(), sel = picked(), ym = cal.dataset.month;
    const lead = fromIso(ym + "-01").getUTCDay();
    const count = daysIn(ym);
    const name = MONTHS[+ym.slice(5, 7) - 1] + " " + ym.slice(0, 4);
    // The one day reachable with Tab; arrow keys move from there.
    const target = [focus, sel, t, ym + "-01"].find((s) => s && monthOf(s) === ym && s >= t);
    cal.querySelector(".bt-cal-title").textContent = name;
    cal.querySelector('[data-cal-nav="-1"]').disabled = ym <= monthOf(t);
    cal.querySelector('[data-cal-nav="1"]').disabled = ym >= addMonths(monthOf(t), MONTHS_AHEAD);

    let html = "";
    for (let i = 0; i < Math.ceil((lead + count) / 7) * 7; i++) {
      const n = i - lead + 1;
      if (n < 1 || n > count) { html += "<span></span>"; continue; }
      const s = ym + "-" + pad(n);
      const cls = "bt-day" + (s === t ? " is-today" : "") + (s === sel ? " is-selected" : "");
      html += '<button type="button" class="' + cls + '" data-date="' + s + '" tabindex="' + (s === target ? 0 : -1) + '"' +
        ' aria-pressed="' + (s === sel) + '"' + (s === t ? ' aria-current="date"' : "") + (s < t ? " disabled" : "") +
        ' aria-label="' + DAYS[i % 7] + " " + n + " " + name + '">' + n + "</button>";
    }
    cal.querySelector(".bt-cal-days").innerHTML = html;

    // Shortcuts: today, tomorrow, and the next Friday (when new movies usually release).
    let k = (5 - fromIso(t).getUTCDay() + 7) % 7;
    if (k < 2) k += 7;
    const quick = [["Today", t, ""], ["Tomorrow", addDays(t, 1), ""],
                   [short(addDays(t, k)), addDays(t, k), "Next Friday, when new movies usually release"]];
    cal.querySelector(".bt-cal-quick").innerHTML = quick.map(([text, s, title]) =>
      '<button type="button" class="bt-cal-chip' + (s === sel ? " is-selected" : "") + '" data-cal-pick="' + s + '"' +
      (title ? ' title="' + title + '"' : "") + ">" + text + "</button>").join("");
  };

  const focusDay = (root) => {
    const day = root.querySelector('.bt-day[tabindex="0"]');
    if (day) day.focus({ preventScroll: true });
  };
  const open = (root) => {
    const cal = root.querySelector(".bt-cal");
    const t = today(), sel = picked();
    cal.dataset.month = monthOf(sel < t ? t : sel);
    render(root);
    cal.classList.remove("is-right");
    cal.hidden = false;
    root.classList.add("is-open");
    root.querySelector(".bt-date-trigger").setAttribute("aria-expanded", "true");
    if (cal.getBoundingClientRect().right > document.documentElement.clientWidth - 8) cal.classList.add("is-right");
    focusDay(root);
  };
  const close = (root, refocus) => {
    const cal = root.querySelector(".bt-cal");
    if (!cal || cal.hidden) return;
    cal.hidden = true;
    root.classList.remove("is-open");
    const trigger = root.querySelector(".bt-date-trigger");
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();
  };
  const choose = (root, s) => {
    const el = field();
    if (el) {
      el.value = s;
      el.dispatchEvent(new Event("input", { bubbles: true }));  // tells Gradio the value changed
    }
    showLabel(root, s);
    close(root, true);
  };
  const moveTo = (root, s) => {
    const t = today();
    if (s < t) s = t;
    if (monthOf(s) > addMonths(monthOf(t), MONTHS_AHEAD)) return;
    root.querySelector(".bt-cal").dataset.month = monthOf(s);
    render(root, s);
    focusDay(root);
  };

  BT.date = {
    sync: (s) => {
      document.querySelectorAll(".bt-date").forEach((root) => {
        showLabel(root, fromIso(s) ? String(s).trim() : today());
        if (root.classList.contains("is-open")) render(root);
      });
      return [];
    },
  };

  document.addEventListener("click", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const root = t && t.closest(".bt-date");
    document.querySelectorAll(".bt-date.is-open").forEach((r) => { if (r !== root) close(r); });
    if (!root) return;
    if (t.closest(".bt-date-trigger")) {
      if (root.classList.contains("is-open")) close(root); else open(root);
      return;
    }
    const nav = t.closest("[data-cal-nav]");
    if (nav) {
      if (!nav.disabled) {
        const cal = root.querySelector(".bt-cal");
        cal.dataset.month = addMonths(cal.dataset.month, +nav.getAttribute("data-cal-nav"));
        render(root);
      }
      return;
    }
    const pick = t.closest("[data-cal-pick], .bt-day");
    if (pick && !pick.disabled) choose(root, pick.getAttribute("data-cal-pick") || pick.getAttribute("data-date"));
  });

  document.addEventListener("keydown", (e) => {
    const t = e.target instanceof Element ? e.target : null;
    const root = t && t.closest(".bt-date");
    if (!root) return;
    if (e.key === "Escape" && root.classList.contains("is-open")) {
      e.preventDefault();
      close(root, true);
      return;
    }
    if (e.key === "ArrowDown" && t.closest(".bt-date-trigger") && !root.classList.contains("is-open")) {
      e.preventDefault();
      open(root);
      return;
    }
    const day = t.closest(".bt-day");
    if (!day) return;
    const s = day.getAttribute("data-date");
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (step) {
      e.preventDefault();
      moveTo(root, addDays(s, step));
    } else if (e.key === "PageUp" || e.key === "PageDown") {
      e.preventDefault();
      const ym = addMonths(monthOf(s), e.key === "PageUp" ? -1 : 1);
      moveTo(root, ym + "-" + pad(Math.min(+s.slice(8), daysIn(ym))));
    }
  });

  // Tabbing out of the calendar closes it.
  document.addEventListener("focusin", (e) => {
    document.querySelectorAll(".bt-date.is-open").forEach((r) => { if (!r.contains(e.target)) close(r); });
  });
})();
</script>
"""

HEAD = f"""
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<meta name="theme-color" content="#12132a">
<script>
(() => {{
  if (window.BookTick) return;
  const BT = (window.BookTick = {{ queue: [] }});
  const TRASH = '<svg class="bt-i" viewBox="0 0 24 24" aria-hidden="true">{ICONS["trash"]}</svg>';

  // Buttons inside the HTML cards can't call Python directly. They queue
  // "action:watcher_id" and click a hidden Gradio button that reads the queue.
  BT.take = () => BT.queue.shift() || "";
  BT.send = (action, id) => {{
    BT.queue.push(action + ":" + (id || ""));
    const btn = document.getElementById("bt-action-btn");
    if (btn) btn.click();
  }};

  // Live regions (stats, watcher cards) are patched in place instead of being
  // re-rendered, so a button never disappears under the pointer mid-click.
  const morph = (a, b) => {{
    if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) {{ a.replaceWith(b.cloneNode(true)); return; }}
    if (a.nodeType !== 1) {{ if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }}
    for (const at of Array.from(a.attributes)) if (!b.hasAttribute(at.name)) a.removeAttribute(at.name);
    for (const at of Array.from(b.attributes)) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
    morphChildren(a, b);
  }};
  const morphChildren = (a, b) => {{
    const old = Array.from(a.childNodes), next = Array.from(b.childNodes);
    next.forEach((n, i) => {{ if (i < old.length) morph(old[i], n); else a.appendChild(n.cloneNode(true)); }});
    for (let i = next.length; i < old.length; i++) old[i].remove();
  }};
  BT.render = (region, html, tries) => {{
    const root = document.querySelector('.bt-live-root[data-region="' + region + '"]');
    if (!root) {{
      if ((tries || 0) < 50) setTimeout(() => BT.render(region, html, (tries || 0) + 1), 100);
      return [];
    }}
    const tpl = document.createElement("template");
    tpl.innerHTML = html || "";
    morphChildren(root, tpl.content);
    return [];
  }};

  BT.goto = (tab) => {{
    const btn = document.getElementById("tab-" + tab + "-button");
    if (btn) btn.click();
    const tabs = document.getElementById("bt-tabs");
    if (tabs && tabs.getBoundingClientRect().top < 0) tabs.scrollIntoView({{ behavior: "smooth", block: "start" }});
  }};

  // A small confirm dialog placed next to the button that opened it, so it stays
  // in view even when Hugging Face stretches the app's frame to its full height.
  BT.confirm = (anchor, title, message, okLabel) => new Promise((resolve) => {{
    const shade = document.createElement("div");
    shade.className = "bt-shade";
    const box = document.createElement("div");
    box.className = "bt-dialog";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML = '<div class="bt-dialog-icon">' + TRASH + '</div><h3></h3><p></p>' +
      '<div class="bt-dialog-actions"><button type="button" class="bt-btn" data-x="no">Cancel</button>' +
      '<button type="button" class="bt-btn danger-solid" data-x="yes"></button></div>';
    box.querySelector("h3").textContent = title;
    box.querySelector("p").textContent = message;
    box.querySelector('[data-x="yes"]').textContent = okLabel;
    const width = Math.min(380, window.innerWidth - 32);
    box.style.width = width + "px";
    box.style.left = (window.scrollX + Math.max(16, (window.innerWidth - width) / 2)) + "px";
    document.body.appendChild(shade);
    document.body.appendChild(box);
    const r = anchor.getBoundingClientRect();
    const top = window.scrollY + r.top + r.height / 2 - box.offsetHeight / 2;
    box.style.top = Math.max(window.scrollY + 16, top) + "px";
    const onKey = (e) => {{ if (e.key === "Escape") {{ e.preventDefault(); close(false); }} }};
    const close = (ok) => {{
      shade.remove(); box.remove();
      document.removeEventListener("keydown", onKey, true);
      if (document.body.contains(anchor)) anchor.focus();
      resolve(ok);
    }};
    shade.addEventListener("click", () => close(false));
    box.addEventListener("click", (e) => {{
      const x = e.target.closest("[data-x]");
      if (x) close(x.getAttribute("data-x") === "yes");
    }});
    document.addEventListener("keydown", onKey, true);
    box.querySelector('[data-x="no"]').focus();
  }});

  BT.copyLog = () => {{
    const rows = Array.from(document.querySelectorAll("#bt-log .bt-log-row"));
    const text = rows.map((r) => r.getAttribute("data-line") || r.textContent).join("\\n");
    const done = () => {{
      const b = document.getElementById("bt-copy-log");
      if (!b) return;
      b.classList.add("is-copied");
      setTimeout(() => b.classList.remove("is-copied"), 1600);
    }};
    const fallback = () => {{
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {{ document.execCommand("copy"); }} catch (e) {{}}
      ta.remove();
      done();
    }};
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
    return [];
  }};

  document.addEventListener("click", (e) => {{
    const t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    const filter = t.closest(".bt-chip[data-bt-filter]");
    if (filter) {{
      document.documentElement.setAttribute("data-bt-view", filter.getAttribute("data-bt-filter"));
      return;
    }}
    const go = t.closest("[data-bt-goto]");
    if (go) {{ e.preventDefault(); BT.goto(go.getAttribute("data-bt-goto")); return; }}
    const a = t.closest("[data-bt-action]");
    if (!a) return;
    e.preventDefault();
    const action = a.getAttribute("data-bt-action");
    const id = a.getAttribute("data-bt-id") || "";
    const card = a.closest(".bt-card");
    const run = () => {{ if (card) card.classList.add("is-busy"); BT.send(action, id); }};
    const ask = a.getAttribute("data-bt-confirm");
    if (!ask) return run();
    BT.confirm(a, a.getAttribute("data-bt-confirm-title") || "Are you sure?", ask,
               a.getAttribute("data-bt-confirm-ok") || "Confirm").then((ok) => {{ if (ok) run(); }});
  }});
}})();
</script>
{THEME_JS}
{DATE_PICKER_JS}
"""


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def live_region(region, html):
    """A block whose content the browser patches in place (see BookTick.render in HEAD).

    Python never re-renders it. Fresh HTML goes into a hidden textbox, and its change
    event hands the HTML to the page script.
    """
    gr.HTML(f'<div class="bt-live-root" data-region="{region}">{html}</div>', elem_classes="bt-root-html")
    source = gr.Textbox(value=html, elem_classes="bt-hidden", show_label=False, container=False, max_lines=1)
    source.change(None, inputs=[source], js=f"(h) => (window.BookTick ? window.BookTick.render('{region}', h) : [])")
    return source


with gr.Blocks(title=f"{APP_NAME} · Movie ticket alerts for District", theme=theme, css=CSS, head=HEAD) as demo:
    gr.HTML(header_html(), elem_classes="bt-root-html")
    status_box = live_region("status", status_html())

    with gr.Tabs(elem_id="bt-tabs"):
        # --- New watcher -----------------------------------------------------
        with gr.Tab("New watcher", id="new", elem_id="tab-new"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3, min_width=340, elem_classes="bt-stack"):
                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("1", "Pick your show", "Choose your city and cinema, then the movie you're waiting for."))
                        with gr.Row():
                            city_input = gr.Dropdown(
                                label="City", choices=[(city_label(DEFAULT_CITY), DEFAULT_CITY)], value=DEFAULT_CITY,
                                info="Type to search 790+ cities.", scale=2, min_width=180, elem_id="bt-city", elem_classes="bt-dd",
                            )
                            cinema_input = gr.Dropdown(
                                label="Cinema", choices=[(DEFAULT_CINEMA_LABEL, DEFAULT_URL)], value=DEFAULT_URL,
                                info="Type part of the name or the area.", scale=3, min_width=240, elem_id="bt-cinema-pick",
                                elem_classes="bt-dd",
                            )
                        cinema_info = gr.HTML(elem_classes="bt-root-html")
                        movie_filter_input = gr.Dropdown(
                            label="Movie", choices=["Ramayana"], value=["Ramayana"], multiselect=True, allow_custom_value=True,
                            elem_id="bt-movie", elem_classes="bt-dd",
                            info="Movies at this cinema come first, then the rest of the city. Not listed yet? "
                                 "Type the name and press Enter. Part of a name is enough.",
                        )
                        with gr.Row():
                            format_filter_input = gr.Dropdown(
                                label="Format", choices=FORMAT_CHOICES, value="IMAX", allow_custom_value=True,
                                info="Pick one or type your own.", min_width=160, elem_classes="bt-dd",
                            )
                            with gr.Column(min_width=200):
                                gr.HTML(date_picker_html(today_ist()), elem_classes="bt-root-html")
                        with gr.Accordion("Or paste a cinema link", open=False, elem_classes="bt-accordion") as link_accordion:
                            cin_url_input = gr.Textbox(
                                label="Cinema page on District",
                                value=DEFAULT_URL,
                                max_lines=1,
                                placeholder="https://www.district.in/movies/...",
                                info="For a cinema that isn't in the list: open it on district.in and copy the link from the address bar.",
                            )
                        preview_btn = gr.Button("Preview shows on this date", variant="secondary")
                        preview_out = gr.HTML(elem_id="bt-preview")

                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("2", "Where should we alert you?", "Fill in one or more. ntfy is the quickest to set up."))
                        user_name_input = gr.Textbox(
                            label="Your name", value="Me", placeholder="e.g. Arun", max_lines=1,
                            info="Shown on the watcher card so you can spot yours.",
                        )
                        with gr.Tabs(elem_id="bt-channel-tabs"):
                            with gr.Tab("ntfy (easiest)"):
                                gr.HTML('<p class="bt-hint">Install the free <b>ntfy</b> app, tap <b>+</b> and subscribe to a topic. Enter the same topic here.</p>')
                                ntfy_topic_input = gr.Textbox(label="ntfy topic", placeholder="e.g. arun_tickets_982", max_lines=1)
                            with gr.Tab("Telegram"):
                                gr.HTML('<p class="bt-hint">Create a bot with <b>@BotFather</b> and send it any message. Get your chat ID from <b>@userinfobot</b>.</p>')
                                with gr.Row():
                                    tg_token_input = gr.Textbox(label="Bot token", type="password", placeholder="123456:ABC-DEF…")
                                    tg_chat_input = gr.Textbox(label="Chat ID", placeholder="123456789", max_lines=1)
                            with gr.Tab("Discord"):
                                gr.HTML('<p class="bt-hint">In your server: <b>Edit channel → Integrations → Webhooks → New webhook</b>, then copy its URL.</p>')
                                disc_hook_input = gr.Textbox(label="Webhook URL", placeholder="https://discord.com/api/webhooks/…", max_lines=1)

                    with gr.Column(elem_classes="bt-step"):
                        gr.HTML(step_head("3", "When tickets open", "Choose how many alerts to get, send a test, then start watching."))
                        max_alerts_slider = gr.Slider(
                            label="Number of alerts", minimum=1, maximum=20, value=5, step=1,
                            info="One alert per check until this many are sent, then the watcher stops.",
                        )
                        with gr.Row(elem_classes="bt-cta-row"):
                            test_notify_btn = gr.Button("Send test alert", variant="secondary", scale=1, min_width=150)
                            add_task_btn = gr.Button("Start watching", variant="primary", scale=2, min_width=180)
                        add_result_lbl = gr.HTML(elem_id="bt-add-result")

                with gr.Column(scale=2, min_width=300, elem_classes="bt-aside"):
                    gr.HTML(HELP_HTML, elem_classes="bt-root-html")

        # --- Watchers --------------------------------------------------------
        with gr.Tab("Watchers", id="watchers", elem_id="tab-watchers") as watchers_tab:
            watchers_box = live_region("watchers", watchers_html())

        # --- Activity --------------------------------------------------------
        with gr.Tab("Activity", id="activity", elem_id="tab-activity") as activity_tab:
            with gr.Row(elem_classes="bt-log-bar"):
                gr.HTML('<div class="bt-section-title"><h3>Activity log</h3><p>Newest first: every check result, alert and change.</p></div>')
                copy_log_btn = gr.Button("Copy log", size="sm", elem_id="bt-copy-log", scale=0, min_width=110)
                clear_logs_btn = gr.Button("Clear log", size="sm", scale=0, min_width=110)
            log_box = gr.HTML(logs_html(), elem_id="bt-log")

        # --- Settings --------------------------------------------------------
        with gr.Tab("Settings", id="settings", elem_id="tab-settings"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3, min_width=320, elem_classes="bt-step"):
                    gr.HTML(step_head(icon("activity"), "Engine", "These settings apply to every watcher on this Space."))
                    engine_note = gr.HTML(engine_note_html())
                    interval_slider = gr.Slider(
                        label="Check every (seconds)", minimum=5, maximum=60, step=5, value=app_state["interval_sec"],
                        info="Very frequent checks can get the Space blocked by District.",
                    )
                    toggle_engine_btn = gr.Button("Pause all checking", variant="secondary")
                with gr.Column(scale=2, min_width=280):
                    gr.HTML(about_html(), elem_classes="bt-root-html")

    gr.HTML(footer_html(), elem_classes="bt-root-html")

    # Hidden bridges for the widgets rendered inside HTML (see BookTick in HEAD): the
    # date picked in the calendar, and the buttons on the watcher cards.
    target_date_input = gr.Textbox(
        value=today_ist(), elem_id="bt-date", elem_classes="bt-hidden", show_label=False, container=False, max_lines=1,
    )
    action_box = gr.Textbox(elem_id="bt-action", elem_classes="bt-hidden", show_label=False, container=False)
    action_btn = gr.Button("Run action", elem_id="bt-action-btn", elem_classes="bt-hidden")

    # --- Events ---------------------------------------------------------------
    city_input.input(
        on_city_change, inputs=[city_input, movie_filter_input],
        outputs=[cinema_input, movie_filter_input, cinema_info, link_accordion, cin_url_input],
    )
    cinema_input.input(
        on_cinema_change, inputs=[city_input, cinema_input, movie_filter_input],
        outputs=[cin_url_input, movie_filter_input, cinema_info], show_progress="hidden",
    )
    cin_url_input.input(
        on_link_input, inputs=[cin_url_input, city_input, movie_filter_input],
        outputs=[cinema_input, movie_filter_input, cinema_info], show_progress="hidden", trigger_mode="always_last",
    )
    preview_btn.click(
        preview_shows,
        inputs=[cin_url_input, movie_filter_input, format_filter_input, target_date_input],
        outputs=[preview_out],
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
        outputs=[add_result_lbl, watchers_box, status_box],
    )

    action_btn.click(
        run_watcher_action,
        inputs=[action_box],
        outputs=[watchers_box, status_box, engine_note, toggle_engine_btn],
        js="(v) => (window.BookTick ? window.BookTick.take() : '')",
        show_progress="hidden",
        trigger_mode="multiple",
    )
    # Python sets the date too (on page load), so keep the calendar's button text in step.
    target_date_input.change(None, inputs=[target_date_input],
                             js="(v) => (window.BookTick && window.BookTick.date ? window.BookTick.date.sync(v) : [])")
    watchers_tab.select(watchers_html, outputs=[watchers_box], show_progress="hidden")
    activity_tab.select(logs_html, outputs=[log_box], show_progress="hidden")

    copy_log_btn.click(None, js="() => (window.BookTick ? window.BookTick.copyLog() : [])")
    clear_logs_btn.click(clear_logs, outputs=[log_box], show_progress="hidden")
    toggle_engine_btn.click(toggle_engine, outputs=[engine_note, toggle_engine_btn, status_box, watchers_box])
    interval_slider.release(change_interval, inputs=[interval_slider], outputs=[engine_note, status_box])

    timer = gr.Timer(3)
    timer.tick(poll_status, outputs=[status_box, watchers_box, log_box], show_progress="hidden")
    demo.load(
        on_page_load,
        outputs=[target_date_input, interval_slider, watchers_box, status_box, engine_note, toggle_engine_btn, log_box],
        show_progress="hidden",
    )
    # Separate so a slow District page never holds up the live status above.
    demo.load(
        load_directory, inputs=[city_input, cin_url_input, movie_filter_input],
        outputs=[city_input, cinema_input, movie_filter_input, cinema_info],
        show_progress="hidden",
    )

# --- Launch -------------------------------------------------------------------
start_engine()
demo.queue(default_concurrency_limit=8).launch(server_name="0.0.0.0", server_port=7860)
