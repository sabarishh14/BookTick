"""Reading BookMyShow: one cinema's shows for one day.

BookMyShow's web pages sit behind Cloudflare's bot checks, and BookTick never tries to get
past those. Its showtimes service does answer requests that say plainly they come from
BookTick, and BookMyShow allowed BookTick to read public showtimes, at a reasonable pace,
that way. So that's all this reads. If BookMyShow turns a request away, the engine stops
asking it for a while (see engine.page_failed); it never retries in disguise.

BookMyShow says how full a show is (available, filling fast, almost full, sold out), not how
many seats are left, so its shows have no seat counts: "seats released" alerts, seat maps
and seat trackers are District only.
"""
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

from .core import IST, shorten, to_float

HOST = "in.bookmyshow.com"
SHOWTIMES_URL = f"https://{HOST}/api/v3/mobile/showtimes/byvenue"
VENUES_URL = f"https://{HOST}/api/v2/mobile/venues"            # a city's cinemas
REGIONS_URL = f"https://{HOST}/api/explore/v1/discover/regions"  # its cities
HEADERS = {
    # Says what's asking, as BookMyShow's permission asks. Never a browser's name.
    "User-Agent": "BookTick/1.0 (ticket availability alerts)",
    "Accept": "application/json",
}
TIMEOUT = (3, 12)
MAX_IDLE = 8  # like District: a connection is reused only for quick bursts
# A show's AvailStatus, as BookMyShow colours it.
LEVELS = {"0": "sold out", "1": "almost full", "2": "filling fast", "3": "seats available"}
# A cinema page: /cinemas/<region>/<cinema>/buytickets/<venue code>[/<yyyymmdd>]
CINEMA_PATH = re.compile(r"^/cinemas/([^/]+)/([^/]+)/buytickets/([A-Za-z0-9]+)(?:/|$)")
_local = threading.local()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
def is_bms(url):
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == HOST or host == "bookmyshow.com" or host.endswith(".bookmyshow.com")


def cinema_parts(url):
    """(region, cinema slug, venue code) from a BookMyShow cinema link, or None."""
    if not is_bms(url):
        return None
    match = CINEMA_PATH.match(urlparse(str(url)).path)
    return (match.group(1).lower(), match.group(2).lower(), match.group(3).upper()) if match else None


def clean_url(value):
    """A BookMyShow cinema link, as BookTick keeps it (no date), or None."""
    url = str(value or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = cinema_parts(url)
    return f"https://{HOST}/cinemas/{parts[0]}/{parts[1]}/buytickets/{parts[2]}" if parts else None


def venue_code(url):
    parts = cinema_parts(url)
    return parts[2] if parts else None


def name_from_url(url):
    """'Pvr Sathyam Royapettah (BookMyShow)', until a check brings BookMyShow's own name."""
    parts = cinema_parts(url)
    return f"{parts[1].replace('-', ' ').title()} (BookMyShow)" if parts else "Cinema (BookMyShow)"


def page_link(url, date=None):
    """The cinema's page on BookMyShow, on a day ("2026-11-20") if one's given."""
    base = clean_url(str(url or "").split("?")[0]) or str(url or "").split("?")[0]
    return f"{base}/{date.replace('-', '')}" if date else base


def seat_link(region, event_code, venue, session, date_code):
    """Straight to a show's seats (BookMyShow's own route for it)."""
    return f"https://{HOST}/movies/{region}/seat-layout/{event_code}/{venue}/{session}/{date_code}"


def page_date(url):
    """The day a page is for: BookTick's pages end ?fromdate=YYYY-MM-DD, like District's."""
    found = parse_qs(urlparse(str(url or "")).query).get("fromdate")
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Fetching and reading
# ---------------------------------------------------------------------------
def http(fresh=False):
    session = getattr(_local, "session", None)
    if session is not None and (fresh or time.time() - _local.last_used > MAX_IDLE):
        session.close()
        session = None
    if session is None:
        session = _local.session = requests.Session()
        session.headers.update(HEADERS)
    _local.last_used = time.time()
    return session


def why(err):
    """What went wrong asking BookMyShow, in a few words: 'HTTP 403', or the error."""
    status = getattr(getattr(err, "response", None), "status_code", None)
    return f"HTTP {status}" if status else shorten(str(err) or type(err).__name__, 100)


def fetch_json(params, url=SHOWTIMES_URL):
    """BookMyShow's answer. Raises requests.HTTPError (with .response.status_code) on 4xx/5xx,
    and ValueError when the answer isn't its JSON (a block page, say)."""
    try:
        res = http().get(url, params=params, timeout=TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        res = http(fresh=True).get(url, params=params, timeout=TIMEOUT)
    _local.last_used = time.time()
    res.raise_for_status()
    if "json" not in (res.headers.get("content-type") or "").lower():
        raise ValueError(f"BookMyShow didn't send showtimes (HTTP {res.status_code}, {res.headers.get('content-type')})")
    return res.json()


def fetch_shows(url):
    """(cinema name, shows) for a BookTick page: a cinema link ending ?fromdate=YYYY-MM-DD."""
    parts = cinema_parts(url)
    if not parts:
        raise ValueError("Not a BookMyShow cinema link")
    date = page_date(url) or datetime.now(IST).strftime("%Y-%m-%d")
    data = fetch_json({"venueCode": parts[2], "dateCode": date.replace("-", "")})
    return parse_showtimes(data, url, date)


def regions():
    """BookMyShow's cities: [{"code": "CHEN", "slug": "chennai", "names": {"chennai", "madras"}}], big cities first."""
    data = (fetch_json({}, REGIONS_URL) or {}).get("BookMyShow") or {}
    out = []
    for r in (data.get("TopCities") or []) + (data.get("OtherCities") or []):
        if isinstance(r, dict) and r.get("RegionCode") and r.get("RegionSlug"):
            names = {str(r["RegionSlug"]).lower(), str(r.get("RegionName") or "").lower()}
            names |= {str(a).lower() for a in r.get("Alias") or []}
            out.append({"code": r["RegionCode"], "slug": str(r["RegionSlug"]).lower(), "names": names - {""}})
    if not out:
        raise ValueError("BookMyShow didn't list any cities")
    return out


def region_for(city, known):
    """BookMyShow's city for one of District's ("chennai", "new-delhi", "gurgaon"), or None."""
    city = str(city or "").strip().lower()
    for want in dict.fromkeys([city, city.replace("-", " "), city.split("-")[-1]]):
        hit = next((r for r in known if want in r["names"]), None)
        hit = hit or next((r for r in known if want in set(re.findall(r"[a-z]+", " ".join(r["names"])))), None)
        if hit:
            return hit
    return None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "cinema"


def city_venues(region):
    """A city's cinemas on BookMyShow: [{"code", "name", "lat", "lon", "down", "url"}]."""
    data = fetch_json({"eventType": "MT", "regionCode": region["code"]}, VENUES_URL) or {}
    out = []
    for v in data.get("venues") or []:
        code, name = str(v.get("VenueCode") or "").upper(), str(v.get("VenueName") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]+", code) or not name:
            continue
        out.append({"code": code, "name": name, "lat": to_float(v.get("VenueLatitude")) or None,
                    "lon": to_float(v.get("VenueLongitude")) or None, "down": bool(v.get("isDown")),
                    "url": f"https://{HOST}/cinemas/{region['slug']}/{slugify(name)}/buytickets/{code}"})
    return out


def venue(url):
    """BookMyShow's name and map position for a cinema ({"name", "lat", "lon"}), from its showtimes
    today (or its next day with shows). None when it has no shows listed at all."""
    code = venue_code(url)
    if not code:
        return None
    data = fetch_json({"venueCode": code, "dateCode": datetime.now(IST).strftime("%Y%m%d")})
    details = (data or {}).get("ShowDetails") or []
    found = (details[0] if details and isinstance(details[0], dict) else {}).get("Venues") or {}
    if isinstance(found, list):
        found = found[0] if found else {}
    if not found.get("VenueName"):
        return None
    return {"name": found["VenueName"], "lat": to_float(found.get("VenueLat")) or None,
            "lon": to_float(found.get("VenueLong")) or None}


def cinema_name(venue):
    """'PVR: Sathyam, Royapettah' -> 'PVR Sathyam, Royapettah (BookMyShow)', and 'Rohini Silver
    Screens: Koyambedu' -> 'Rohini Silver Screens, Koyambedu (BookMyShow)'. The source is in the
    name, so a BookMyShow cinema can be told apart everywhere."""
    name = str(venue.get("VenueName") or "").strip()
    brand, colon, rest = name.partition(":")
    if colon and brand.strip() and rest.strip():
        name = f"{brand.strip()}{' ' if len(brand.split()) == 1 else ', '}{rest.strip()}"
    return f"{name} (BookMyShow)" if name else None


def show_time(code):
    try:
        return datetime.strptime(str(code), "%Y%m%d%H%M").replace(tzinfo=IST)
    except ValueError:
        return None


def show_format(child, show):
    """'2D', '3D', 'IMAX 3D', ...: the dimension, plus a screen kind BookMyShow puts in its attributes."""
    fmt = str(child.get("EventDimension") or "2D").strip()
    extra = [w for w in ("IMAX", "4DX", "ICE", "SCREENX", "DOLBY") if w in str(show.get("Attributes") or "").upper()
             and w not in fmt.upper()]
    return " ".join(extra + [fmt])


def parse_showtimes(data, url, date):
    """BookMyShow's showtimes for one cinema -> (cinema name, shows on `date`).

    A day that isn't open yet gets the nearest open day's shows back, so only `date`'s are kept.
    A cinema BookMyShow marks as down (bookings there aren't going through) keeps its shows,
    each one closed, like a show District greys out.
    """
    details = (data or {}).get("ShowDetails") or []
    block = details[0] if details and isinstance(details[0], dict) else {}
    venue = block.get("Venues") or {}
    if isinstance(venue, list):
        venue = venue[0] if venue else {}
    parts = cinema_parts(url) or ("", "", venue.get("VenueCode") or "")
    name = cinema_name(venue) or name_from_url(url)
    down = bool(venue.get("isVenueDown") or venue.get("IsVenueDown"))
    base = clean_url(url.split("?")[0]) or url.split("?")[0]
    day = date.replace("-", "")
    shows = []
    for event in block.get("Event") or []:
        for child in event.get("ChildEvents") or []:
            for s in child.get("ShowTimes") or []:
                if str(s.get("ShowDateCode") or str(s.get("ShowDateTime") or "")[:8]) != day:
                    continue
                status = str(s.get("AvailStatus") or "")
                shows.append({
                    "title": event.get("EventTitle") or child.get("EventName") or "Movie",
                    "cinema": name,
                    "cinema_url": base,
                    "format": show_format(child, s),
                    "language": child.get("EventLanguage") or "",
                    "start": show_time(s.get("ShowDateTime")),
                    "audi": s.get("ScreenName") or "",
                    "avail": 0 if status == "0" else None,  # how full, not how many: see LEVELS
                    "total": None,
                    "level": LEVELS.get(status),
                    "closed": down,
                    "price": to_float(s.get("MinPrice")),
                    "url": seat_link(parts[0] or "chennai", child.get("EventCode") or "", parts[2],
                                     s.get("SessionId") or "", day),
                })
    shows.sort(key=lambda s: s["start"].timestamp() if s["start"] else float("inf"))
    return name, shows
