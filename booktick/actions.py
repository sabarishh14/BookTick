"""The buttons in alerts: "Booked it, stop watching" and "Got it, keep watching".

Each watcher has a random action token. ntfy's buttons POST to /watch/<token>/<choice>
straight from the notification. Telegram and Discord link to /watch/<token>, a small page
with the same two buttons; those apps preview links, so opening the page never changes
anything by itself.
"""
import secrets
from html import escape

from .config import APP_NAME, PUBLIC_URL
from .core import add_log, state, tasks_lock
from .storage import save_tasks

CHOICES = ("stop", "keep")


def new_token():
    return secrets.token_urlsafe(18)


def action_url(task, choice=None):
    """The link for one of a watcher's buttons (or its page), or None when there's no public URL."""
    token = task.get("action_token")
    if not PUBLIC_URL or not token:
        return None
    return f"{PUBLIC_URL}/watch/{token}" + (f"/{choice}" if choice else "")


def find(token):
    """The watcher with this action token, or None. Call with tasks_lock held."""
    token = str(token or "")
    if len(token) < 16:
        return None
    for task in state["tasks"].values():
        own = task.get("action_token")
        if own and secrets.compare_digest(own, token):
            return task
    return None


def apply(token, choice):
    """Carry out a button press. Returns (ok, message for the person who pressed it)."""
    if choice not in CHOICES:
        return False, "That button isn't recognised."
    with tasks_lock:
        task = find(token)
        if not task:
            return False, "This watcher no longer exists."
        name = task.get("user_label") or "Watcher"
        title = task.get("movie_filter") or "your movie"
        owners = (task.get("owner"),)
        if choice == "stop":
            if task.get("status") == "Done":
                return True, f"BookTick had already stopped watching for {title}."
            task.update(status="Done", booked=True, reminders_left=0)
            save_tasks()
            add_log(f"🎉 [{name}] booked, says the alert. Watcher stopped.", owners)
            return True, f"Enjoy the movie! BookTick has stopped watching for {title}."
        task["reminders_left"] = 0
        save_tasks()
    add_log(f"👍 [{name}] keep watching, says the alert.", owners)
    return True, (f"OK. No more reminders for now. BookTick keeps watching for {title} and will tell you "
                  "if more seats free up or bookings close and reopen.")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{app}</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f4f4f9; --card: #fff; --text: #161729; --muted: #686a82; --line: #e6e6ef; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg: #0b0c16; --card: #151624; --text: #ececf4; --muted: #9a9cb5; --line: #2c2d44; }} }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px; box-sizing: border-box;
         background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ width: 100%; max-width: 420px; padding: 24px; border-radius: 18px; background: var(--card); border: 1px solid var(--line); }}
  h1 {{ margin: 0 0 4px; font-size: 1.2rem; }} p {{ margin: 0 0 18px; color: var(--muted); }}
  form {{ margin: 0 0 10px; }}
  button {{ width: 100%; height: 48px; border-radius: 12px; border: 1px solid var(--line); background: var(--card); color: var(--text);
           font: inherit; font-weight: 650; cursor: pointer; }}
  button.primary {{ border: none; color: #fff; background: linear-gradient(135deg, #ff6b4a, #f43f7a); }}
</style></head>
<body><main>
  <h1>{heading}</h1>
  <p>{body}</p>
  {buttons}
</main></body></html>"""


def page_html(token, message=""):
    with tasks_lock:
        task = find(token)
        snapshot = dict(task) if task else None
    if not snapshot:
        return PAGE.format(app=APP_NAME, heading="Watcher not found", body="It may have been deleted.", buttons="")
    title = escape(snapshot.get("movie_filter") or "your movie")
    if message:
        return PAGE.format(app=APP_NAME, heading=title, body=escape(message), buttons="")
    if snapshot.get("status") == "Done":
        return PAGE.format(app=APP_NAME, heading=title, body="BookTick has already stopped watching for this.", buttons="")
    form = '<form method="post" action="/watch/{t}/{c}"><button class="{cls}" type="submit">{label}</button></form>'
    buttons = (form.format(t=escape(token), c="stop", cls="primary", label="Booked it, stop watching")
               + form.format(t=escape(token), c="keep", cls="", label="Not yet, keep watching"))
    return PAGE.format(app=APP_NAME, heading=title, body="Did you get tickets?", buttons=buttons)


def register(app):
    """Add the button endpoints to the FastAPI app that serves the page."""
    from fastapi.responses import HTMLResponse

    @app.get("/watch/{token}", response_class=HTMLResponse)
    def watch_page(token: str):
        return HTMLResponse(page_html(token))

    @app.post("/watch/{token}/{choice}", response_class=HTMLResponse)
    def watch_choice(token: str, choice: str):
        ok, message = apply(token, choice)
        return HTMLResponse(page_html(token, message), status_code=200 if ok else 404)
