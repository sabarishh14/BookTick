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

A Python and Gradio app that watches [District](https://www.district.in) cinema pages around the clock and pings your phone the moment tickets open for the movie, format and date you want. A single background engine reads each cinema page's embedded Next.js session data (seats, prices, formats), with Schema.org events as a fallback. It shares one fetch between everyone watching the same cinema and date, and sends alerts through ntfy, Telegram and Discord with a deep link straight to seat selection. Watchers are kept in memory, so the page and the checks never wait on storage, and saved to Postgres (Neon) by a background writer so they survive restarts.

**Live:** [huggingface.co/spaces/sabarishhh14/BookTick](https://huggingface.co/spaces/sabarishhh14/BookTick)

## Features
- **Cinema directory**: 790+ cities from District's sitemap, cinemas searchable by name or area, or paste any district.in cinema link
- **Movie, format and date**: watch several movies at once, including ones whose bookings haven't opened yet; IMAX, 4DX, 3D, 2D or your own format; pick the day from a calendar
- **Live preview**: everything listed for that date right now, with seats left, prices and booking links
- **Alerts**: ntfy, Telegram and Discord, a test alert before you start, and 1 to 20 alerts per watcher
- **Watcher cards**: pause, resume, re-arm, send a test, or delete (with a confirmation), and filter by status
- **Activity log and engine controls**: every check result, alert and change; pause checking or change how often it runs
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

### 3. Set up alerts

Fill in one or more on the **New watcher** tab:

1. **ntfy** (easiest): install the ntfy app, tap **+** and subscribe to a topic nobody would guess, like `arun_tickets_982`. Enter the same topic in BookTick
2. **Telegram**: create a bot with [@BotFather](https://t.me/BotFather) and send it any message, then get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. **Discord**: in your server, **Edit channel → Integrations → Webhooks → New webhook**, then copy its URL

### 4. Deploy to Hugging Face Spaces

Every push to `main` that changes `app.py`, `requirements.txt` or `README.md` syncs those files to the Space through [.github/workflows/hf-sync.yml](.github/workflows/hf-sync.yml).

1. Add a repository secret named `HF_TOKEN`: a Hugging Face access token with write access to the Space
2. Add `DATABASE_URL` as a secret on the Space (see step 2)

## Architecture

```
BookTick/
├── app.py                         # The whole app
│   ├── District scraper           # __NEXT_DATA__ sessions + Schema.org events → shows
│   ├── Directory                  # Cities (sitemap), each city's cinemas and movies, cached
│   ├── Engine                     # One thread; one page fetch per cinema + date per sweep
│   ├── Notifications              # ntfy, Telegram, Discord
│   ├── Storage                    # In memory; Postgres in the background, or a local JSON file
│   └── UI                         # Gradio Blocks + custom HTML, CSS and JS (calendar, live cards)
├── requirements.txt
└── .github/workflows/hf-sync.yml  # Syncs the app to the Hugging Face Space
```

## How a Check Works

| Step | What happens |
|------|--------------|
| Fetch | One request per cinema and date (`?fromdate=YYYY-MM-DD`), shared by every watcher on it |
| Parse | Shows from the page's `__NEXT_DATA__` (seats, prices, formats); Schema.org `ScreeningEvent`s fill gaps and add booking links |
| Match | The title contains one of your movie names, the format contains yours (or any), the show is on your date, and seats are open |
| Alert | One alert per check while a show matches, until the watcher's alert count is used up. Then it's marked **Alerted** |
| Save | Status and alert count are saved in the background; per-check details stay in memory |

## Notes

- All times are Indian Standard Time.
- District has no public API. BookTick reads the same page your browser does, so a redesign on District's side may need a parser update.
- There are no accounts yet: everyone who opens the Space sees the same watchers and activity log.
- Checks run every 5 seconds by default (5 to 60 in **Settings**). Very frequent checks can get the Space blocked by District.
- A free Space goes to sleep after a long time without visitors, and checking stops until someone opens it. Saved watchers come back when it wakes.
- BookTick is an independent project and isn't affiliated with District or Zomato.

---

Built by [Sabarish SB](https://github.com/sabarishh14).
