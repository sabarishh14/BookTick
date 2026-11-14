"""Reading District: fetching pages, parsing shows, and the city/cinema/movie directory.

District has no public API. BookTick reads the JSON its pages embed for the browser
(Next.js's __NEXT_DATA__), with Schema.org events as a backup on cinema pages.
"""
import json
import re
import threading
import time
from urllib.parse import urlparse

import requests

from .core import parse_time, to_float, to_int

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # requests adds "Accept-Encoding: gzip, deflate": pages arrive about 10x smaller.
}
TIMEOUT = (3, 12)  # seconds to connect (normally ~50 ms), seconds to wait for the page
# District's edge drops idle connections without telling us, and a request on a dropped
# connection hangs until the timeout. So a connection is reused only for quick bursts
# (several dates checked back to back), never after sitting idle.
MAX_IDLE = 8
_local = threading.local()


def http(fresh=False):
    """This thread's session, reused for bursts of requests and replaced after MAX_IDLE seconds."""
    session = getattr(_local, "session", None)
    if session is not None and (fresh or time.time() - _local.last_used > MAX_IDLE):
        session.close()
        session = None
    if session is None:
        session = _local.session = requests.Session()
        session.headers.update(HEADERS)
    _local.last_used = time.time()
    return session


def fetch_html(url, timeout=TIMEOUT):
    """The page's text. Raises requests.HTTPError (with .response.status_code) on 4xx/5xx."""
    try:
        res = http().get(url, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout):
        res = http(fresh=True).get(url, timeout=timeout)  # one more try on a new connection
    _local.last_used = time.time()
    res.raise_for_status()
    return res.content.decode("utf-8", errors="replace")


def next_data(page):
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.DOTALL)
    return json.loads(match.group(1)) if match else {}


def dig(node, *keys):
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
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
    slug = str(url or "").split("?")[0].rstrip("/").split("/")[-1]
    slug = re.sub(r"-CD\d+$", "", slug, flags=re.I)
    return slug.replace("-", " ").title() or "Cinema"


def cinema_id_from_url(url):
    match = re.search(r"-CD(\d+)", str(url or ""), re.I)
    return match.group(1) if match else None


def query_url(page_url, date):
    base = page_url.split("?")[0].strip()
    return f"{base}?fromdate={date}" if date else base


def city_movie_url(movie_url, city, date=None):
    """A movie's page for one city, which lists every cinema's shows there, or None."""
    base, count = re.subn(r"-movie-tickets-MV(\d+)$", rf"-movie-tickets-in-{city}-MV\1",
                          str(movie_url or "").split("?")[0].rstrip("/"))
    return query_url(base, date) if count else None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "cinema"


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------
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
    """Deep link to the seat picker for one show.

    District shows "Booking is now closed" unless the movie's format code is in the path and
    ends the session id, and the show's date is included. City pages call that code "mcd", and
    their own encSessionId ends in the cinema's id instead, so it can't be used as it is.
    """
    s = session
    fid = str(s.get("fid") or s.get("mcd") or "").lower()
    if not fid:
        return fallback
    if all(s.get(k) for k in ("cid", "sid", "mid")):
        enc = f"{s['cid']}-{s['sid']}-{str(s['mid']).lower()}-{fid}"
    elif s.get("encSessionId"):
        enc = s["encSessionId"]
    else:
        return fallback
    start = parse_time(s.get("showTime"), naive_is_utc=True)
    date = f"&fromdate={start.strftime('%Y-%m-%d')}" if start else ""
    url = f"https://www.district.in/movies/seat-layout/{fid}?encsessionid={enc}{date}&freeseating=false&fromsessions=true&type=CINEMAS"
    if content_id:
        url += f"&contentid={content_id}"
    return url


def cinema_page_url(entity_url):
    """City pages link a cinema as /<slug>-c/<id>; its own page is /movies/<slug>-CD<id>."""
    match = re.fullmatch(r"/(.+)-c/(\d+)", str(entity_url or ""))
    return f"https://www.district.in/movies/{match.group(1)}-CD{match.group(2)}" if match else None


def make_show(session, title, cinema, content_id, fallback_url, fmt=None, cinema_url=None):
    s = session
    return {
        "title": title,
        "cinema": cinema,
        "cinema_url": cinema_url,
        "format": s.get("scrnFmt") or fmt or s.get("premiumLabel") or "2D",
        # District's session times are UTC with no timezone marker.
        "start": parse_time(s.get("showTime"), naive_is_utc=True),
        "audi": s.get("audi", ""),
        "avail": to_int(s.get("avail")),
        "total": to_int(s.get("total")),
        "price": lowest_price(s.get("areas")),
        "url": session_booking_url(s, content_id, fallback_url),
    }


def sort_shows(shows):
    shows.sort(key=lambda s: s["start"].timestamp() if s["start"] else float("inf"))
    return shows


def find_sessions(data):
    """District files a cinema's shows under a key that changes with the URL
    (the cinema ID, or cinema ID + date), so take whichever block has them."""
    sessions = (data.get("pageData") or {}).get("sessions") or []
    if sessions:
        return sessions, list(data.get("groupedMovies") or [])
    for block in (data.get("serverState") or {}).values():
        if not isinstance(block, dict):
            continue
        found = (block.get("pageData") or {}).get("sessions") or []
        if found:
            meta = block.get("meta") or {}
            return found, list(meta.get("movies") or []) + list(meta.get("groupedMovies") or [])
    return [], []


def parse_cinema_page(page, url):
    """One cinema's page -> (cinema_name, shows). Each show has an IST `start` datetime."""
    data = dig(next_data(page), "props", "pageProps", "data") or {}
    # "cinemaName" is built from the link; the session block's meta has District's real name.
    real_names = [dig(block, "meta", "cinema", "name") for block in (data.get("serverState") or {}).values()
                  if isinstance(block, dict)]
    cinema_name = next((n for n in real_names if n), None) or data.get("cinemaName") or cinema_name_from_url(url)
    cinema_url = url.split("?")[0]
    shows = []

    # 1. Live session data (has seat counts)
    sessions, movies = find_sessions(data)
    lookup = build_movie_lookup(movies)
    for s in sessions:
        movie = lookup.get(str(s.get("mid"))) or lookup.get(str(s.get("contentId"))) or {}
        title = movie.get("name") or f"{s.get('lang', '')} movie".strip()
        shows.append(make_show(s, title, cinema_name, movie.get("content_id") or s.get("contentId"), url,
                               movie.get("format"), cinema_url))

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

            # The same show from the session data: by its session id, or by movie and time when
            # that's unambiguous. Two screens can show the same movie at the same time, and each
            # must keep its own link.
            sid = re.search(r"encsessionid=[^-&]+-([^-&]+)-", book_url)
            same = next((s for s in shows if f"-{sid.group(1)}-" in s["url"]), None) if sid else None
            if same is None:
                alike = [s for s in shows if s["title"].lower() == title.lower() and s["start"] == start]
                if len(alike) > 1:
                    continue
                same = alike[0] if alike else None
            if same:
                same["url"] = book_url
                continue
            sold_out = str(offers.get("availability", "")).endswith("SoldOut")
            shows.append({
                "title": title,
                "cinema": cinema_name,
                "cinema_url": cinema_url,
                "format": ev.get("videoFormat") or ("IMAX" if "IMAX" in name else "3D" if "3D" in name else "2D"),
                "start": start,
                "audi": "",
                "avail": 0 if sold_out else None,
                "total": None,
                "price": to_float(offers.get("price")),
                "url": book_url,
            })
    return cinema_name, sort_shows(shows)


def parse_city_movie_page(page, url):
    """A movie's city page -> (movie_name, shows across every cinema in the city)."""
    state = dig(next_data(page), "props", "pageProps", "data", "serverState") or {}
    shows, movie_name = [], None
    for block in (state.get("movieSessions") or {}).values():
        if not isinstance(block, dict):
            continue
        meta = block.get("meta") or {}
        movie = meta.get("movie") or {}
        movie_name = movie_name or movie.get("name")
        content_id = movie.get("content_id")
        for cinema in block.get("arrangedSessions") or []:
            name = cinema.get("entityName") or "Cinema"
            cinema_url = cinema_page_url(cinema.get("entityUrl"))
            for s in cinema.get("sessions") or []:
                shows.append(make_show(s, movie_name or "Movie", name, content_id, url, cinema_url=cinema_url))
    return movie_name, sort_shows(shows)


def fetch_shows(kind, url):
    """Fetch and parse one page: kind is "cinema" or "city"."""
    page = fetch_html(url)
    return parse_city_movie_page(page, url) if kind == "city" else parse_cinema_page(page, url)


# ---------------------------------------------------------------------------
# Directory: cities, their cinemas, and what's showing
# ---------------------------------------------------------------------------
CITY_SITEMAP_URL = "https://cdn.district.in/sitemap/sitemap-cinema-city.xml"
POPULAR_CITIES = [
    "chennai", "bengaluru", "mumbai", "new-delhi", "hyderabad", "kolkata", "pune", "ahmedabad", "kochi",
    "coimbatore", "gurgaon", "noida", "jaipur", "lucknow", "chandigarh", "vizag", "madurai", "trivandrum",
    "mysuru", "indore",
]
_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, loader, fallback=None, retry_after=300, max_age=None):
    """Return a cached value, reloading it after `ttl` seconds (or sooner, if the caller's
    `max_age` is shorter).

    If a reload fails, keep serving the last good value (or `fallback`) and try again
    after `retry_after` seconds. With nothing to fall back on, the error is raised.
    """
    with _cache_lock:
        hit = _cache.get(key)
    now = time.time()
    if hit and hit["expires"] > now and (max_age is None or now - hit["loaded"] < max_age or hit.get("failed")):
        return hit["value"]
    try:
        value, expires, failed = loader(), now + ttl, False
    except Exception:
        value = hit["value"] if hit else fallback
        if value is None:
            raise
        expires, failed = now + min(retry_after, max_age or retry_after), True
    with _cache_lock:
        _cache[key] = {"value": value, "expires": expires, "loaded": now, "failed": failed}
    return value


def peek(key):
    """A cached value and its age in seconds, without loading anything. None if not cached."""
    with _cache_lock:
        hit = _cache.get(key)
    return None if hit is None else {"value": hit["value"], "age": time.time() - hit["loaded"]}


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


def parse_city_page(page):
    """District's 'cinemas in <city>' page -> {"cinemas": [...], "movies": [...]}."""
    rails = dig(next_data(page), "props", "pageProps", "data", "serverState", "EDSResponse", "rails") or []
    links = {cid: path for path, cid in re.findall(r"(/movies/[a-z0-9-]+-CD(\d+))", page)}
    cinemas, movies = {}, {}
    for rail in rails:
        for item in rail.get("items") or []:
            info = dig(item, "ItemDetails", "CinemaData") or {}
            cid, name = str(info.get("cinema_id") or ""), info.get("cinema_name")
            if not cid or not name or cid in cinemas:
                continue
            here = []
            for child in item.get("children") or []:
                movie = dig(child, "ItemDetails", "MovieData") or {}
                if not movie.get("name"):
                    continue
                here.append(movie["name"])
                if movie.get("movie_id") and movie.get("web_url"):
                    movies.setdefault(str(movie["movie_id"]), {
                        "id": str(movie["movie_id"]), "name": movie["name"], "url": movie["web_url"]})
            cinemas[cid] = {
                "id": cid,
                "name": name,
                # District takes the cinema's display name from the link, so prefer its own links.
                "url": "https://www.district.in" + links.get(cid, f"/movies/{slugify(name)}-CD{cid}"),
                "movies": list(dict.fromkeys(here)),
            }
    if not cinemas:
        raise ValueError("District's page didn't list any cinemas")
    return {"cinemas": sorted(cinemas.values(), key=lambda c: c["name"].lower()),
            "movies": sorted(movies.values(), key=lambda m: m["name"].lower())}


def valid_city(city):
    city = str(city or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{1,60}", city):
        raise ValueError("Pick a city from the list")
    return city


def city_directory(city, max_age=None):
    """A city's cinemas and movies, cached for 30 minutes (or `max_age` seconds, if shorter)."""
    city = valid_city(city)
    return cached(f"city:{city}", 30 * 60,
                  lambda: parse_city_page(fetch_html(f"https://www.district.in/movies/cinemas-in-{city}")),
                  max_age=max_age)


def city_cinemas(city):
    return city_directory(city)["cinemas"]


def known_cinema_name(url):
    """District's name for a cinema, from any city list already loaded (no network)."""
    cid = cinema_id_from_url(url)
    with _cache_lock:
        lists = [hit["value"] for key, hit in _cache.items() if key.startswith("city:")]
    return next((c["name"] for d in lists for c in d["cinemas"] if cid and c["id"] == cid), None)


def cinema_label(cinema, city):
    """'PVR Grand Mall, Velachery, Chennai' -> 'PVR Grand Mall, Velachery' (the city is already picked)."""
    return re.sub(rf",\s*{re.escape(city_label(city))}\s*$", "", cinema["name"], flags=re.I) or cinema["name"]


def movie_choices(cinemas, cinema=None):
    """This cinema's movies first, then everything else showing in the city."""
    here = list(cinema["movies"]) if cinema else []
    elsewhere = sorted({m for c in cinemas for m in c["movies"]} - set(here), key=str.lower)
    return here + elsewhere


def matching_movies(movies, keywords):
    """Movies in a city's list whose name contains any keyword."""
    return [m for m in movies if any(k in m["name"].lower() for k in keywords)]
