"""Who can see and change what, and the limits that keep the Space fair.

Each browser keeps a random key (see BookTick.key in static/app.js). The server never
stores it: a watcher records a hash of it as its owner, and only a session holding the
same key, or an admin, can see or change that watcher. Every check happens here on the
server, so hiding a button is never the only protection.
"""
import collections
import hashlib
import hmac
import json
import re
import threading
import time

from .config import (ADMIN_KEY, MAX_WATCHERS_PER_NETWORK, MAX_WATCHERS_PER_PERSON, MAX_WATCHERS_TOTAL,
                     TEST_ALERTS_PER_MINUTE, TEST_COOLDOWN)
from .core import plural, state, tasks_lock

OPEN_STATUSES = ("Active", "Paused")


def digest(text, purpose):
    return hashlib.sha256(f"booktick:{purpose}:{text}".encode("utf-8")).hexdigest()[:24]


def check_admin(key):
    """True only when ADMIN_KEY is set and `key` matches it (compared in constant time)."""
    key = str(key or "").strip()
    return bool(ADMIN_KEY and key and hmac.compare_digest(key.encode("utf-8"), ADMIN_KEY.encode("utf-8")))


def session_from_identity(raw):
    """The page sends {"key": ..., "admin": ...} from its storage on load."""
    try:
        ident = json.loads(raw) if raw else {}
    except ValueError:
        ident = {}
    key = str(ident.get("key") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        return {"owner": None, "admin": False, "admin_rejected": False}
    admin_key = str(ident.get("admin") or "")
    admin = check_admin(admin_key)
    return {"owner": digest(key, "owner"), "admin": admin, "admin_rejected": bool(admin_key) and not admin}


def network_id(request):
    """A hash of the visitor's IP address (Hugging Face puts it in X-Forwarded-For)."""
    if request is None:
        return None
    headers = getattr(request, "headers", {}) or {}
    ip = str(headers.get("x-forwarded-for", "")).split(",")[0].strip()
    if not ip and getattr(request, "client", None):
        ip = request.client.host
    return digest(ip, "network") if ip else None


def can_manage(session, task):
    if not session or not task:
        return False
    return bool(session.get("admin") or (session.get("owner") and task.get("owner") == session["owner"]))


def visible_tasks(session):
    """(id, watcher copy) pairs this session may see: its own, or every watcher for an admin."""
    with tasks_lock:
        items = [(tid, dict(t)) for tid, t in state["tasks"].items()]
    if session and session.get("admin"):
        return items
    owner = (session or {}).get("owner")
    return [(tid, t) for tid, t in items if owner and t.get("owner") == owner]


def watcher_limit_error(session, network):
    """Why this person can't add another watcher right now, or None."""
    if session.get("admin"):
        return None
    with tasks_lock:
        open_tasks = [t for t in state["tasks"].values() if t.get("status") in OPEN_STATUSES]
    if len(open_tasks) >= MAX_WATCHERS_TOTAL:
        return "BookTick is watching as much as it can right now. Try again once some watchers finish."
    mine = sum(1 for t in open_tasks if t.get("owner") == session.get("owner"))
    if mine >= MAX_WATCHERS_PER_PERSON:
        return (f"You can have {plural(MAX_WATCHERS_PER_PERSON, 'watcher')} going at once. "
                "Delete one you don't need, or wait for one to finish.")
    if network and sum(1 for t in open_tasks if t.get("network") == network) >= MAX_WATCHERS_PER_NETWORK:
        return "Too many watchers from your network right now. Delete one you don't need first."
    return None


# --- Test alerts ----------------------------------------------------------------
_test_lock = threading.Lock()
_last_test = {}  # destination hash -> time of the last test sent there
_recent_tests = collections.deque()  # times of recent tests, for the overall limit


def destinations(channels):
    """One hash per place an alert would go, so the cooldown follows the phone, not the visitor."""
    out = []
    if channels.get("ntfy_topic"):
        out.append(digest("ntfy:" + channels["ntfy_topic"].lower(), "dest"))
    if channels.get("telegram_chat_id"):
        out.append(digest(f"tg:{channels.get('telegram_token')}:{channels['telegram_chat_id']}", "dest"))
    if channels.get("discord_webhook"):
        out.append(digest("discord:" + channels["discord_webhook"], "dest"))
    return out


def claim_test(channels, admin=False):
    """Reserve a test alert. Returns None if it may be sent, or a reason it can't yet."""
    now = time.time()
    with _test_lock:
        while _recent_tests and now - _recent_tests[0] > 60:
            _recent_tests.popleft()
        if not admin and len(_recent_tests) >= TEST_ALERTS_PER_MINUTE:
            return "Lots of test alerts are going out right now. Try again in a minute."
        keys = destinations(channels)
        wait = max((TEST_COOLDOWN - (now - _last_test.get(k, 0)) for k in keys), default=0)
        if wait > 0 and not admin:
            return f"A test just went to this channel. Try again in {int(wait) + 1}s."
        for k in keys:
            _last_test[k] = now
        _recent_tests.append(now)
        if len(_last_test) > 5000:  # forget old entries
            for k in [k for k, t in _last_test.items() if now - t > TEST_COOLDOWN]:
                del _last_test[k]
    return None
