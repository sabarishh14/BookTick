"""Time and text helpers, the shared in-memory state, and the activity log."""
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import DEFAULT_INTERVAL, MAX_LOG_LINES, MAX_RANGE_DAYS

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
# Shared state. Watchers live in memory; storage.py copies changes to disk or Postgres.
# ---------------------------------------------------------------------------
tasks_lock = threading.RLock()  # RLock so helpers that take the lock can call each other
log_lock = threading.Lock()
wake_engine = threading.Event()  # set it to make the engine look for work right away
save_needed = threading.Event()  # set it to make the database writer save right away

state = {
    "tasks": {},  # watcher id -> watcher dict
    "paused": False,
    "interval_sec": DEFAULT_INTERVAL,
    "check_count": 0,
    "last_check_time": None,
    "logs": [],
}


def add_log(msg, owners=None):
    """Record a line in the activity log.

    `owners` says who sees it: None for everyone, or the owner ids of the watchers it's
    about (admins see every line). The full log also goes to the Space's container logs.
    """
    stamp = now_ist().strftime("%d %b %H:%M:%S")
    entry = {"t": stamp, "msg": msg, "owners": None if owners is None else frozenset(o for o in owners if o)}
    with log_lock:
        state["logs"].append(entry)
        del state["logs"][:-MAX_LOG_LINES]
    try:
        print(f"[{stamp}] {msg}", flush=True)
    except (UnicodeError, OSError):  # a console that can't show emoji must never stop the engine
        pass


def logs_for(owner, admin):
    """The log lines one person may see, newest first."""
    with log_lock:
        entries = list(state["logs"])
    return [e for e in reversed(entries) if admin or e["owners"] is None or owner in e["owners"]]


def redact(text, *secrets):
    """Hide tokens before text reaches the activity log."""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "•••")
    return text


# ---------------------------------------------------------------------------
# Formatting
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
# Dates. A watcher covers one day ("target_date") or a few ("target_date" to
# "target_date_to"); the calendar sends "YYYY-MM-DD" or "YYYY-MM-DD..YYYY-MM-DD".
# ---------------------------------------------------------------------------
def to_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


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


def parse_dates(value):
    """The calendar's value as (start, end) YYYY-MM-DD strings, or None if it's not usable."""
    parts = str(value or "").split("..")
    start = normalize_date(parts[0])
    end = normalize_date(parts[1]) if len(parts) > 1 else start
    if not start or not end or len(parts) > 2:
        return None
    if end < start:
        start, end = end, start
    if (to_date(end) - to_date(start)).days >= MAX_RANGE_DAYS:
        return None
    return start, end


def task_dates(task):
    """Every date a watcher covers, oldest first."""
    start = to_date(task.get("target_date")) or now_ist().date()
    end = to_date(task.get("target_date_to")) or start
    days = max(0, min((end - start).days, MAX_RANGE_DAYS - 1))
    return [str(start + timedelta(days=i)) for i in range(days + 1)]


def date_label(date_str):
    if not date_str:
        return "Today"
    day = to_date(date_str)
    if not day:
        return date_str
    today = now_ist().date()
    pretty = day.strftime("%a %d %b")
    if day == today:
        return f"Today · {pretty}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {pretty}"
    return pretty


def dates_label(start, end=None):
    """'Today · Sun 08 Nov', or 'Fri 13 – Sun 15 Nov · 3 days' for a range (as the calendar shows it)."""
    a, b = to_date(start), to_date(end) if end else None
    if not a or not b or a == b:
        return date_label(start)
    left = a.strftime("%a %d") if (a.year, a.month) == (b.year, b.month) else a.strftime("%a %d %b")
    return f"{left} – {b.strftime('%a %d %b')} · {(b - a).days + 1} days"


def task_dates_label(task):
    dates = task_dates(task)
    return dates_label(dates[0], dates[-1])


def date_is_past(date_str):
    day = to_date(date_str)
    return bool(day and day < now_ist().date())
