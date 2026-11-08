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

# 🎟️ BookTick

Get a phone alert the moment tickets open for your movie on [District](https://www.district.in).

BookTick checks a District cinema page in the background, around the clock. When a show that matches your movie, format and date has seats, it sends you an alert with a link straight to seat selection.

## Features

- Watch any District cinema for one or more movies, in a format you choose (IMAX, 4DX, 3D, 2D or your own) on a date you choose.
- Preview what's listed right now, with seats left, prices and booking links.
- Get alerts through ntfy, Telegram or Discord, and send a test alert first.
- Manage each watcher from its own card: pause, resume, re-arm, send a test, or delete (with a confirmation).
- Filter watchers by status, follow the activity log, and pause or tune the checking engine.

## Run it locally

Use Python 3.10, the same version Hugging Face Spaces runs.

```bash
pip install gradio==4.44.0 -r requirements.txt
python app.py
```

Then open http://localhost:7860.

## Deployment

Every push to `main` that changes `app.py`, `requirements.txt` or `README.md` syncs those files to the Hugging Face Space, through [.github/workflows/hf-sync.yml](.github/workflows/hf-sync.yml). The workflow needs a repository secret named `HF_TOKEN`: a Hugging Face access token with write access to the Space.

---

Built by [Sabarish SB](https://github.com/sabarishh14). BookTick is an independent project and isn't affiliated with District or Zomato.
