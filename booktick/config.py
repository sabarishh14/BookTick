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

# --- Checking -------------------------------------------------------------------
DEFAULT_INTERVAL = 15        # seconds between checks of the same District page
MIN_INTERVAL, MAX_INTERVAL = 5, 60
CITY_LIST_MAX_AGE = 60       # city-wide watchers re-read a city's cinema list at most this often
FETCH_WORKERS = 4            # District pages fetched at the same time
MAX_BACKOFF = 600            # longest wait before retrying a page that keeps failing (seconds)
MAX_BLOCK_BACKOFF = 1800     # longest pause for everyone when District turns requests away
MAX_RANGE_DAYS = 7           # longest date range one watcher can cover
KEEP_FINISHED_DAYS = 7       # finished watchers are removed this many days after their last date

# --- Limits (admins are exempt) -------------------------------------------------
MAX_WATCHERS_PER_PERSON = 5   # open watchers (watching or paused) per browser
MAX_WATCHERS_PER_NETWORK = 10  # per IP address, so new browsers don't get around the limit above
MAX_WATCHERS_TOTAL = 500
TEST_COOLDOWN = 60            # seconds between test alerts to the same ntfy topic / chat / webhook
TEST_ALERTS_PER_MINUTE = 20   # across everyone
PREVIEW_CACHE = 15            # seconds a preview reuses a page it just fetched

# --- Defaults and choices -------------------------------------------------------
DEFAULT_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"
DEFAULT_CITY = "chennai"
DEFAULT_CINEMA_LABEL = "INOX Phoenix Market City (formerly Jazz Cinemas), Velachery"
FORMAT_CHOICES = ["ANY", "IMAX", "4DX", "3D", "2D"]
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
