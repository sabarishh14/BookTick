---
title: BookTick
emoji: 🎟️
colorFrom: red
colorTo: pink
sdk: gradio
sdk_version: 4.44.0
app_file: app.py
pinned: false
short_description: Phone alerts the moment movie tickets open on District
---

# BookTick - Never Miss First Day, First Show 🎟️

A Python and Gradio app that watches [District](https://www.district.in) around the clock and pings your phone the moment tickets open for the movie, format, dates and show times you want, at one cinema or anywhere in your city. A scheduler thread hands due pages to a small pool of fetch workers that read each page's embedded Next.js session data (seats, prices, formats) over compressed, short-lived connections. Watchers on the same page share one fetch, failing pages back off exponentially, and the whole engine pauses if District starts turning requests away. Alerts go out through ntfy, Telegram and Discord with a link to the theatre's page for that date. Watchers stay in memory, so the page and the checks never wait on storage, and are saved to Postgres (Neon) by a background writer. Each browser only sees and manages its own watchers, and an admin key unlocks the engine controls.

**Live:** [huggingface.co/spaces/sabarishhh14/BookTick](https://huggingface.co/spaces/sabarishhh14/BookTick)

## Features
- **Any cinema, or the whole city**: 790+ cities from District's sitemap, cinemas searchable by name or area, a pasted district.in link, or **Any cinema** to hear the moment your movie opens anywhere in the city
- **Movie, format, dates and show times**: several movies at once (including ones whose bookings haven't opened), IMAX / 4DX / 3D / 2D or your own format, one day or up to 7 in a row from a calendar, and morning / afternoon / evening / night
- **Live preview**: everything listed right now, with seats left, prices and booking links, grouped by cinema for a city-wide watch
- **Alerts**: ntfy, Telegram and Discord, and a test alert before you start. You hear when bookings open (plus reminders), when they close again, when they reopen and when seats free up
- **Answer from the alert**: ntfy alerts have **Booked it, stop watching** and **Got it, keep watching** buttons; Telegram and Discord alerts link to a page with the same two
- **Private watchers**: each browser sees and manages only its own; **Copy my link** opens them on another device
- **Watcher cards**: pause, resume, re-arm, send a test, or delete, and filter by status. Watchers stop by themselves once their dates pass
- **Admin controls**: pause checking for everyone, change how often it runs, see every watcher and the engine's health
- **Fair use**: up to 5 open watchers per browser (10 per network), and a 1-minute cooldown between test alerts to the same place
- **Saved watchers**: stored in Postgres when `DATABASE_URL` is set, so restarts and redeploys don't lose them
- Light and dark mode, remembered per browser

## Setup

### 1. Run locally

Use Python 3.10, the same version Hugging Face Spaces runs.

```bash
pip install gradio==4.44.0 -r requirements.txt
python app.py
```

App runs on `http://localhost:7860`

Without `DATABASE_URL`, watchers are saved to `booktick_watchers.json` in the folder you run it from.

### 2. Save watchers to Postgres (optional, recommended on the Space)

```bash
export DATABASE_URL="postgresql://user:password@host/db?sslmode=require"
python app.py
```

BookTick creates its `booktick_watchers` table on first run.

**Getting a free Postgres database from Neon:**
1. Go to [neon.tech](https://neon.tech) and create a project
2. Dashboard → **Connect** → copy the pooled connection string
3. Use it as `DATABASE_URL`. On the Space, add it under **Settings → Variables and secrets** as a secret, never in the code

### 3. Turn on the admin controls (optional)

```bash
export ADMIN_KEY="a-long-random-phrase"
python app.py
```

Then open **Settings → Admin**, enter the key and press **Unlock**. On the Space, add `ADMIN_KEY` as a secret. Without it, nobody can pause checking or change the interval.

**How it works:**
- The key is checked on the server for every admin action, compared in constant time; a wrong key waits a second before the reply
- After unlocking, this browser remembers the key; **Sign out of admin** forgets it
- Change the secret on the Space to sign every admin out

### 4. Set up alerts

Fill in one or more on the **New watcher** tab:

1. **ntfy** (easiest): install the ntfy app, tap **+** and subscribe to a topic nobody would guess, like `arun_tickets_982`. Enter the same topic in BookTick
2. **Telegram**: create a bot with [@BotFather](https://t.me/BotFather) and send it any message, then get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. **Discord**: in your server, **Edit channel → Integrations → Webhooks → New webhook**, then copy its URL

### 5. Deploy to Hugging Face Spaces

Every push to `main` that changes `app.py`, `booktick/`, `requirements.txt` or `README.md` syncs them to the Space through [.github/workflows/hf-sync.yml](.github/workflows/hf-sync.yml).

1. Add a repository secret named `HF_TOKEN`: a Hugging Face access token with write access to the Space
2. Add `DATABASE_URL` and `ADMIN_KEY` as secrets on the Space (see steps 2 and 3)

The buttons in alerts call the Space at its public address, which Hugging Face provides as `SPACE_HOST`. Anywhere else, set `PUBLIC_URL` to the address people reach the app at; without it, alerts are sent without buttons.

### 6. Keep the Space awake (optional)

A free Space goes to sleep after a while without visitors, and checking stops until someone opens it. To keep it awake for free:

1. Sign up at [uptimerobot.com](https://uptimerobot.com) and add an **HTTP(s)** monitor
2. URL: `https://sabarishhh14-booktick.hf.space`, interval: 5 minutes

Paid Space hardware can be set never to sleep instead.

### 7. Run the tests

```bash
python -m unittest discover -s tests
BOOKTICK_LIVE=1 python -m unittest tests.test_live   # also checks District's real pages
```

[.github/workflows/tests.yml](.github/workflows/tests.yml) runs the unit tests on every push and the live check every morning. If the live check fails, District has probably changed its pages: run `python tests/make_fixtures.py` to save fresh copies, then fix the parser.

## Architecture

```
BookTick/
├── app.py                    # Page layout and events
├── booktick/
│   ├── config.py             # Settings, limits and secrets
│   ├── core.py               # Time and date helpers, shared state, activity log
│   ├── district.py           # Fetching and parsing District; cities, cinemas, movies
│   ├── engine.py             # Scheduler + fetch workers, matching, back-off, expiry
│   ├── alerts.py             # ntfy, Telegram, Discord
│   ├── storage.py            # In memory; Postgres in the background, or a local JSON file
│   ├── access.py             # Owners, admin key, limits, test-alert cooldowns
│   ├── views.py              # HTML for the page
│   ├── handlers.py           # What the buttons and fields do
│   ├── theme.py              # Gradio theme, CSS and JS loading
│   └── static/               # app.css, app.js (calendar, live cards, theme, identity)
├── tests/                    # Unit tests, trimmed District pages, live check
└── .github/workflows/        # Deploy to the Space; run the tests
```

## How a Check Works

| Step | What happens |
|------|--------------|
| Plan | Every second, the scheduler lists the pages active watchers need: one per cinema and date, or one per movie, city and date for city-wide watchers. Watchers on the same page share it |
| Fetch | Due pages go to 4 workers. Pages arrive compressed (about 50 KB instead of 500 KB), on a connection that's never reused after sitting idle |
| Parse | Shows from the page's `__NEXT_DATA__` (seats, prices, formats); on cinema pages, Schema.org `ScreeningEvent`s fill gaps and add booking links |
| Match | The title contains one of your movie names, the format contains yours, the show is on one of your dates and in your show times, and seats are open |
| Alert | On changes: bookings open (then reminders, one per round, until you tap a button), close again (sold out or taken off sale), reopen, or a show gains 10+ seats. A watcher keeps going until its dates pass or you tap **Booked it** |
| Back off | A failing page waits 2×, 4×, 8× longer (up to 10 minutes). If District turns requests away (HTTP 403/429), every check pauses, longer each time (up to 30 minutes) |
| Clean up | Watchers whose dates have passed stop by themselves and are removed a week later |
| Save | Status and alert count are saved in the background; per-check details stay in memory |

City-wide watchers first read the city's cinema list (once a minute) until the movie appears in it, then watch that movie's city page, which lists every cinema's shows.

## Notes

- All times are Indian Standard Time.
- District has no public API. BookTick reads the same pages your browser does, so a redesign on District's side may need a parser update (the daily live test catches this).
- Watchers belong to the browser that made them. Clearing the browser's data loses access to them (they keep running and alerting). Keep **Copy my link** somewhere safe if that matters, and don't share it: anyone with it can manage your watchers.
- Checks run every 15 seconds by default (5 to 60 for the admin). Very frequent checks can get the Space blocked by District.
- BookTick is an independent project and isn't affiliated with District or Zomato.

---

Built by [Sabarish SB](https://github.com/sabarishh14).
