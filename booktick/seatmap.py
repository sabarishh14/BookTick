"""Seat map snapshots: what a show's seats look like the moment bookings open.

District's seat page draws its map from a request its own JavaScript makes, so the seats
aren't in any page BookTick can fetch directly. A browser on the server opens the seat page
as a guest (no login), takes the seat data from District's own request as it arrives, and
draws BookTick's own picture of it. It runs when bookings open or reopen, and for seat
trackers when a show's seat count moves, never on every check. One browser is kept running
so each snapshot costs only the page load, and alert snapshots always go first.
"""
import html
import itertools
import os
import queue
import re
import secrets
import threading
import time

from .config import PUBLIC_URL
from .core import add_log, shorten

BROWSER_PATHS = [
    os.environ.get("CHROMIUM_PATH", ""),
    "/usr/bin/chromium",  # installed on the Space from packages.txt
    "/usr/bin/chromium-browser",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",  # for running locally on Windows
]
SKIP = re.compile(r"google|doubleclick|analytics|facebook|clarity|hotjar|branch\.io|moengage|sentry", re.I)
IMAGE_TTL = 2 * 24 * 3600  # how long a seat map image stays available
MAX_IMAGES = 200
REUSE_FOR = 120            # watchers on the same show share one snapshot taken this recently

_images = {}               # image id -> {"png", "expires", "show", "free", "total", "taken_at"}
_images_lock = threading.Lock()
_jobs = queue.PriorityQueue(maxsize=80)  # (priority, order, job): alerts 0, seat trackers 5
_order = itertools.count()
_worker = None
_worker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Reading District's seat layout
# ---------------------------------------------------------------------------
def seat_rows(layout):
    """[(section, [(row, [(column, is_free, seat number)]), ...]), ...] from District's seat data.

    A seat's SeatStatus is "0" when it's free and "1" when it's sold or blocked.
    """
    names = {a.get("code"): a.get("label") for a in layout.get("areas") or []}
    sections = []
    for area in (layout.get("seatLayout") or {}).get("colAreas", {}).get("objArea") or []:
        rows = []
        for row in area.get("objRow") or []:
            seats = [(s.get("GridSeatNum") or 0, str(s.get("SeatStatus")) == "0", s.get("displaySeatNumber") or "")
                     for s in row.get("objSeat") or []]
            if seats:
                rows.append((row.get("PhyRowId") or "", seats))
        if rows:
            sections.append((names.get(area.get("AreaCode")) or area.get("AreaDesc") or "", rows))
    return sections


def counts(sections):
    seats = [seat for _, rows in sections for _, row in rows for seat in row]
    return sum(1 for seat in seats if seat[1]), len(seats)


def render_html(sections, title, subtitle):
    """BookTick's drawing of the seats: outlined = free, grey = sold or blocked; screen at the bottom."""
    free, total = counts(sections)
    width = max((col for _, rows in sections for _, row in rows for col, _, _ in row), default=1)
    cell, gap = 14, 3
    parts = []
    for name, rows in sections:
        if len(sections) > 1 and name:
            parts.append(f'<div class="area">{html.escape(name)}</div>')
        for row_id, seats in rows:
            cells = ['<i class="none"></i>'] * width
            for col, is_free, number in seats:
                if 1 <= col <= width:
                    cells[col - 1] = f'<i class="{"free" if is_free else "taken"}"></i>'
            label = html.escape(str(row_id))
            parts.append(f'<div class="row"><b>{label}</b>{"".join(cells)}<b>{label}</b></div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  body {{ margin: 0; padding: 20px; background: #fff; font: 13px system-ui, "DejaVu Sans", sans-serif; color: #161729; display: inline-block; }}
  h1 {{ font-size: 15px; margin: 0 0 2px; }} p {{ margin: 0 0 14px; color: #686a82; }}
  .area {{ margin: 10px 0 6px 26px; font-size: 11px; font-weight: 700; letter-spacing: .08em; color: #686a82; }}
  .row {{ display: flex; align-items: center; gap: {gap}px; margin-bottom: {gap}px; }}
  .row b {{ width: 20px; text-align: center; font-size: 11px; color: #686a82; }}
  i {{ width: {cell}px; height: {cell}px; border-radius: 3px; display: inline-block; box-sizing: border-box; }}
  i.free {{ border: 1.5px solid #15803d; }} i.taken {{ background: #c9cad6; }} i.none {{ visibility: hidden; }}
  .screen {{ margin: 12px 0 0 26px; width: {width * (cell + gap)}px; text-align: center; color: #686a82; font-size: 11px;
            letter-spacing: .3em; border-bottom: 3px solid #c9cad6; border-radius: 0 0 50% 50% / 0 0 12px 12px; padding-bottom: 4px; }}
  .key {{ margin-top: 12px; color: #686a82; }} .key i {{ vertical-align: -2px; margin: 0 4px 0 12px; }}
</style></head><body><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p>{"".join(parts)}
<div class="screen">SCREEN THIS WAY</div>
<div class="key"><i class="free"></i>{free} free<i class="taken"></i>{total - free} sold or blocked</div></body></html>"""


# ---------------------------------------------------------------------------
# The browser (one, kept running, used only from the worker thread)
# ---------------------------------------------------------------------------
def browser_path():
    return next((p for p in BROWSER_PATHS if p and os.path.exists(p)), None)


class Browser:
    def __init__(self):
        self.playwright = self.browser = None

    def ensure(self):
        if self.browser and self.browser.is_connected():
            return self.browser
        self.close()
        from playwright.sync_api import sync_playwright  # only needed when snapshots are taken

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            executable_path=browser_path(), args=["--no-sandbox", "--disable-dev-shm-usage"])
        return self.browser

    def layout(self, url):
        """Open the seat page as a new guest and return the seat data District's page asks for."""
        context = self.ensure().new_context(viewport={"width": 1280, "height": 900})
        try:
            def route(r):
                req = r.request
                if req.resource_type in ("image", "font", "media") or SKIP.search(req.url):
                    return r.abort()
                return r.continue_()
            context.route("**/*", route)
            page = context.new_page()
            with page.expect_response(lambda r: "/select-seat" in r.url and r.request.method == "POST",
                                      timeout=30000) as info:
                page.goto(url, wait_until="commit", timeout=30000)
            return info.value.json()
        finally:
            context.close()

    def png(self, page_html):
        page = self.ensure().new_page()
        try:
            page.set_content(page_html)
            return page.locator("body").screenshot()
        finally:
            page.close()

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = self.browser = None


# ---------------------------------------------------------------------------
# Snapshots, images and the worker
# ---------------------------------------------------------------------------
def enabled():
    return browser_path() is not None


def store_image(png, show, free, total):
    image_id = secrets.token_urlsafe(16)
    now = time.time()
    with _images_lock:
        for key in [k for k, v in _images.items() if v["expires"] < now]:
            del _images[key]
        while len(_images) >= MAX_IMAGES:
            del _images[min(_images, key=lambda k: _images[k]["expires"])]
        _images[image_id] = {"png": png, "expires": now + IMAGE_TTL, "show": show.get("url"),
                             "free": free, "total": total, "taken_at": now}
    return image_id


def image(image_id):
    with _images_lock:
        hit = _images.get(str(image_id or ""))
        return hit if hit and hit["expires"] > time.time() else None


def image_url(image_id):
    return f"{PUBLIC_URL}/seatmap/{image_id}.png" if PUBLIC_URL and image_id else None


def recent_image(show_url):
    """A snapshot of this show taken in the last REUSE_FOR seconds, if any."""
    now = time.time()
    with _images_lock:
        hits = [(k, v) for k, v in _images.items() if v["show"] == show_url and now - v["taken_at"] < REUSE_FOR]
    return max(hits, key=lambda kv: kv[1]["taken_at"], default=(None, None))[0]


def snapshot(browser, show, place):
    """Photograph one show's seats. Returns the stored image id."""
    reuse = recent_image(show["url"])
    if reuse:
        return reuse
    sections = seat_rows(browser.layout(show["url"]))
    if not sections:
        raise ValueError("District's seat page had no seat layout")
    free, total = counts(sections)
    when = show["start"].strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if show.get("start") else ""
    title = " · ".join(x for x in (show.get("title"), when) if x)
    subtitle = " · ".join(x for x in (shorten(place, 70), str(show.get("format") or "")) if x)
    return store_image(browser.png(render_html(sections, title, subtitle)), show, free, total)


def work():
    browser = Browser()
    while True:
        _, _, job = _jobs.get()
        try:
            job(browser)
        except Exception as e:  # a job reports its own errors; this only keeps the worker alive
            print(f"Seat map job failed: {e}", flush=True)
            browser.close()


def submit(priority, job):
    """Queue `job(browser)` for the worker. Never blocks; False if it couldn't be queued."""
    global _worker
    if not enabled():
        return False
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=work, daemon=True, name="booktick-seatmap")
            _worker.start()
    try:
        _jobs.put_nowait((priority, next(_order), job))
        return True
    except queue.Full:
        return False


def request(task, show, place, done):
    """Queue a snapshot; `done(task, show, place, image_id)` runs when it's ready. Never blocks."""
    def job(browser):
        try:
            image_id = snapshot(browser, show, place)
        except Exception as e:
            add_log(f"⚠️ [{task.get('user_label', 'Watcher')}] couldn't take the seat map: {shorten(e, 120)}",
                    (task.get("owner"),))
            browser.close()  # start a fresh browser next time
            return
        done(task, show, place, image_id)
    return submit(0, job)


def request_layout(url, done):
    """For the seat tracker: queue a read of one show's seats, after any alert snapshots.

    `done(layout, error)` runs on the worker thread with District's seat data, or None and why not.
    """
    def job(browser):
        try:
            layout = browser.layout(url)
        except Exception as e:
            browser.close()
            done(None, e)
            return
        done(layout, None)
    return submit(5, job)


def waiting():
    return _jobs.qsize()


def register(app):
    """Serve the seat map images (ntfy shows them from these links)."""
    from fastapi.responses import Response

    @app.get("/seatmap/{image_id}.png")
    def seatmap_image(image_id: str):
        hit = image(image_id)
        if not hit:
            return Response(status_code=404)
        return Response(hit["png"], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
