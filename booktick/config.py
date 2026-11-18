"""Brand, settings and limits. Secrets come from the environment (Space secrets)."""
import os

APP_NAME = "BookTick"
APP_TAGLINE = "Get pinged the second tickets open for your movie on District."
AUTHOR_NAME = "Sabarish SB"
GITHUB_USER = "sabarishh14"
GITHUB_URL = f"https://github.com/{GITHUB_USER}"
GITHUB_AVATAR = f"https://github.com/{GITHUB_USER}.png?size=96"
BRAND_DISCORD_COLOR = 0xF9573A

# --- Secrets --------------------------------------------------------------------
# Postgres connection string. Without it, watchers go to a local JSON file.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Unlocks the admin controls (engine pause, check interval, every watcher). Without it,
# nobody can use them.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
# Where this app can be reached from the internet, for the buttons in alerts. Hugging Face
# sets SPACE_HOST by itself; anywhere else, set PUBLIC_URL. Without either, alerts have no buttons.
PUBLIC_URL = (os.environ.get("PUBLIC_URL", "").strip()
              or (f"https://{os.environ['SPACE_HOST'].strip()}" if os.environ.get("SPACE_HOST") else "")).rstrip("/")

# --- Checking -------------------------------------------------------------------
DEFAULT_INTERVAL = 15        # seconds between checks of the same District page
MIN_INTERVAL, MAX_INTERVAL = 5, 60
CITY_LIST_MAX_AGE = 60       # city-wide watchers re-read a city's cinema list at most this often
FETCH_WORKERS = 4            # District pages fetched at the same time
MAX_BACKOFF = 600            # longest wait before retrying a page that keeps failing (seconds)
MAX_BLOCK_BACKOFF = 1800     # longest pause for everyone when District turns requests away
MAX_RANGE_DAYS = 7           # longest date range one watcher can cover
KEEP_FINISHED_DAYS = 7       # finished watchers are removed this many days after their last date
MORE_SEATS_MIN = 10          # seats a show must gain before a "seats released" alert (or any, if it was sold out)
MORE_SEATS_GAP = 900         # a cinema that opens, closes and opens again is announced at most this often
SEATS_BACK_GAP = 600         # "seats released" alerts at most this often per watcher (what comes between waits)
NEW_SHOWS_GAP = 180          # "new shows" alerts at most this often per watcher (shows added between wait too)
DAY_END_MARGIN = 1800        # shows leaving a page within this long of its last show's start: the day ending, not a close
CINEMA_FLAP_GAP = 600        # "closed at <cinema>" at most this often per cinema (seats can blink 0 -> 1 -> 0)
CITY_SPREAD_GAP = 180        # a city-wide watcher names the cinemas that opened since, at most this often
ALERT_LOG_KEEP = 12          # alerts a watcher's card lists (the newest)
MAX_SEATMAPS = 8             # seat maps sent when bookings open: one per matching show, earliest first

# --- Seat tracker ---------------------------------------------------------------
TRACK_DAYS_AHEAD = 7         # how far ahead a tracker's day can be
MAX_TRACKED_SHOWS = 60       # shows one tracker follows (seat counts for all; seat snapshots soonest first)
TRACK_SAVE_EVERY = 600       # seconds between saving a tracker's history to storage
TRACK_MAP_GAP = 300          # least time between seat snapshots of one show in its last 3 hours…
TRACK_MAP_GAP_FAR = 1200     # …and before that
TRACK_MAP_SPACING = 30       # least time between any two tracker snapshots, across everyone
RELEASE_MIN = 6              # seats that must come back in one go to count as released by the cinema
BLOCK_MIN = 10               # seats gone in one go to count as held back by the cinema (or a big group)
HOLD_WINDOW = 1200           # seats free again this soon after being taken were an unfinished booking
HOLD_MAX = 10                # …but only up to one booking's worth of seats at once
REPORT_AFTER = 600           # the report goes out this long after the last tracked show starts

# --- Limits (admins are exempt) -------------------------------------------------
MAX_WATCHERS_PER_PERSON = 5   # open watchers (watching or paused) per browser
MAX_WATCHERS_PER_NETWORK = 10  # per IP address, so new browsers don't get around the limit above
MAX_WATCHERS_TOTAL = 500
MAX_TRACKERS_PER_PERSON = 2   # seat trackers going at once per browser (each takes seat snapshots)
MAX_TRACKERS_TOTAL = 30
TEST_COOLDOWN = 60            # seconds between test alerts to the same ntfy topic / chat / webhook
TEST_ALERTS_PER_MINUTE = 20   # across everyone
PREVIEW_CACHE = 15            # seconds a preview reuses a page it just fetched

# --- Defaults and choices -------------------------------------------------------
DEFAULT_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"
DEFAULT_CITY = "chennai"
DEFAULT_CINEMA_LABEL = "INOX Phoenix Market City (formerly Jazz Cinemas), Velachery"
FORMAT_CHOICES = ["ANY", "IMAX", "4DX", "3D", "2D"]       # the seat tracker's format list
# A watcher's formats: (chip, words to look for, where). District's format field only says things
# like "2D", "3D" or "IMAX 3D"; premium screens show up in the screen's or the cinema's name
# ("4K RGB LASER DOLBY ATMOS"). "format" looks at the format and the screen's name; "any" also
# at the cinema's name. Words match whole, so "ICE" doesn't match "SERVICE".
FORMATS = [
    ("IMAX", ["IMAX"], "format"),
    ("4DX", ["4DX"], "format"),
    ("3D", ["3D"], "format"),
    ("2D", ["2D"], "format"),
    ("Dolby Atmos", ["ATMOS"], "any"),
    ("Laser", ["LASER"], "any"),
    ("4K", ["4K"], "any"),
    ("PLF", ["PLF"], "any"),
    ("ICE", ["ICE"], "format"),
    ("ScreenX", ["SCREENX", "SCREEN X"], "format"),
    ("MX4D", ["MX4D"], "format"),
    ("EPIQ", ["EPIQ"], "format"),
    ("P[XL]", ["P[XL]", "PXL"], "format"),
    ("Insignia", ["INSIGNIA"], "format"),
    ("Onyx", ["ONYX"], "format"),
    ("Dolby Cinema", ["DOLBY CINEMA"], "format"),
]
BASE_FORMATS = ["IMAX", "4DX", "3D", "2D"]  # always offered; the rest when a city's cinemas have them
MAX_CINEMAS_PER_WATCHER = 5   # cinemas one watcher can follow…
MAX_CINEMA_DAYS = 15          # …and cinemas × days, since each is a page checked every few seconds
# (key, label, from hour, to hour). Hours past 24 run into the next morning, so a
# 1 AM show counts as Night.
TIME_SLOTS = [
    ("morning", "Morning", 5, 12),
    ("afternoon", "Afternoon", 12, 16),
    ("evening", "Evening", 16, 20),
    ("night", "Night", 20, 29),
]
MAX_LOG_LINES = 2000          # kept in memory across everyone; each person sees their own

# --- Storage --------------------------------------------------------------------
DB_TABLE = "booktick_watchers"
TASKS_FILE = "booktick_watchers.json"
LEGACY_TASKS_FILE = "district_tasks.json"  # file name used before the BookTick rename
