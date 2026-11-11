"""Saving watchers.

Watchers live in memory, which is all the engine and the page ever read, so storage never
slows either down. With DATABASE_URL set (a Space secret), a background thread copies each
change to Postgres, so watchers survive restarts and redeploys. Without it they go to a
local JSON file, which a Space loses whenever it restarts.
"""
import json
import os
import re
import threading
from urllib.parse import urlparse

from .config import DATABASE_URL, DB_TABLE, LEGACY_TASKS_FILE, TASKS_FILE
from .core import add_log, plural, redact, save_needed, shorten, state, tasks_lock, wake_engine

# Rewritten on every check and meaningless after a restart, so they aren't stored.
UNSTORED_KEYS = ("last_checked", "last_checked_ts", "last_result", "last_alert_ts", "last_more_ts",
                 "page_seats", "expected_pages")
# What the database holds right now (in stored_form), so only changes get written.
_db = {"ready": False, "stored": {}}


def save_tasks():
    """Save all watchers. Call while holding tasks_lock.

    With a database this only wakes the background writer, so it never waits on the network.
    """
    if DATABASE_URL:
        save_needed.set()
        return
    tmp = TASKS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stored_form(state["tasks"]), f, indent=2)
        os.replace(tmp, TASKS_FILE)
    except Exception as e:
        print(f"Could not save watchers: {e}", flush=True)


def db_reason(e):
    """A short reason for the activity log, without host names or the password.

    The full error (still without the password) goes only to the Space's container logs.
    """
    full = redact(e, DATABASE_URL, urlparse(DATABASE_URL).password)
    print(f"Database error: {full}", flush=True)
    match = re.search(r"(?:ERROR|FATAL):\s*([^\n]+)", full)
    reason = match.group(1) if match else re.split(r"failed:\s*", (full.splitlines() or [""])[0])[-1]
    return shorten(reason.strip(), 160) or type(e).__name__


def stored_form(tasks):
    """The watchers as they're saved: plain JSON copies, without the per-check fields."""
    kept = {tid: {k: v for k, v in t.items() if k not in UNSTORED_KEYS} for tid, t in tasks.items()}
    return json.loads(json.dumps(kept))


def db_connect():
    import psycopg  # only needed when DATABASE_URL is set

    # A fresh connection per save: Neon closes idle ones when it suspends. prepare_threshold=None
    # because a pooled endpoint may not keep prepared statements between transactions.
    return psycopg.connect(DATABASE_URL, connect_timeout=10, prepare_threshold=None)


def db_load():
    """Every saved watcher (creating the table the first time)."""
    with db_connect() as conn:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {DB_TABLE} ("
                     "id text PRIMARY KEY, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
        rows = conn.execute(f"SELECT id, data FROM {DB_TABLE}").fetchall()
    return {tid: dict(data, id=tid) for tid, data in rows}


def db_flush():
    """Write the watchers that changed since the last save, and remove deleted ones."""
    if not _db["ready"]:
        # The database was down at launch: load it now, keeping watchers added meanwhile.
        saved = db_load()
        with tasks_lock:
            for tid, task in saved.items():
                state["tasks"].setdefault(tid, task)
        _db.update(ready=True, stored=stored_form(saved))
        add_log(f"🗄️ Database connected. Loaded {plural(len(saved), 'saved watcher')}.", owners=())
        wake_engine.set()
    with tasks_lock:
        current = stored_form(state["tasks"])
    stored = _db["stored"]
    changed = [(tid, data) for tid, data in current.items() if stored.get(tid) != data]
    removed = [tid for tid in stored if tid not in current]
    if not changed and not removed:
        return
    from psycopg.types.json import Jsonb

    with db_connect() as conn, conn.cursor() as cur:
        if changed:
            cur.executemany(f"INSERT INTO {DB_TABLE} (id, data, updated_at) VALUES (%s, %s, now()) "
                            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                            [(tid, Jsonb(data)) for tid, data in changed])
        if removed:
            cur.execute(f"DELETE FROM {DB_TABLE} WHERE id = ANY(%s)", (removed,))
    _db["stored"] = current


def storage_loop():
    """The database writer: saves soon after each change, and retries every 30s if a save fails."""
    failing = False
    while True:
        save_needed.wait(timeout=30)
        save_needed.clear()
        try:
            db_flush()
            if failing:
                add_log("🗄️ Database is back. Watchers are being saved again.", owners=())
            failing = False
        except Exception as e:
            if not failing:
                add_log(f"⚠️ Couldn't save watchers to the database: {db_reason(e)}. Retrying every 30s.", owners=())
            failing = True


def load_tasks():
    """The watchers to start with: from Postgres when it's set up, else the local file."""
    if DATABASE_URL:
        try:
            tasks = db_load()
            _db.update(ready=True, stored=stored_form(tasks))
            add_log(f"🗄️ Loaded {plural(len(tasks), 'saved watcher')} from the database.", owners=())
            return tasks
        except Exception as e:
            # One quick try so the Space starts promptly; the writer keeps trying after that.
            add_log(f"⚠️ Couldn't reach the database: {db_reason(e)}. Retrying in the background.", owners=())
            return {}
    for path in (TASKS_FILE, LEGACY_TASKS_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
    return {}


def start_storage():
    state["tasks"] = load_tasks()
    if DATABASE_URL:
        threading.Thread(target=storage_loop, daemon=True, name="booktick-storage").start()
