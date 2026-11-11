"""The "How to use" page (served at /help, opened from the header in a new tab)."""
from .config import APP_NAME, MAX_RANGE_DAYS, MAX_SEATMAPS, MAX_WATCHERS_PER_PERSON
from .views import RESTART_TIP, logo_svg

GUIDE_HTML = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>How to use {APP_NAME}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
<script>
  // Follow the light/dark choice made with the toggle in BookTick, if there is one.
  try {{ const t = localStorage.getItem("booktick-theme"); if (t) document.documentElement.dataset.theme = t; }} catch (e) {{}}
</script>
<style>
  :root {{ --bg: #f4f4f9; --card: #fff; --soft: #f6f6fa; --text: #161729; --muted: #686a82; --line: #e8e8f0;
          --brand: #f9573a; --brand-bg: #ffebe5; }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{ --bg: #0b0c16; --card: #151624; --soft: #1c1d2e; --text: #ececf4; --muted: #9a9cb5;
                                       --line: #2a2b40; --brand: #ff8a6e; --brand-bg: rgba(249, 87, 58, .15); }}
  }}
  :root[data-theme="dark"] {{ --bg: #0b0c16; --card: #151624; --soft: #1c1d2e; --text: #ececf4; --muted: #9a9cb5;
                              --line: #2a2b40; --brand: #ff8a6e; --brand-bg: rgba(249, 87, 58, .15); }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); font: 16px/1.6 "Plus Jakarta Sans", system-ui, sans-serif; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 28px 16px 56px; }}
  header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 28px; }}
  header svg {{ width: 48px; height: 48px; flex: none; }}
  h1 {{ margin: 0; font-size: 1.6rem; letter-spacing: -.02em; }}
  header p {{ margin: 2px 0 0; color: var(--muted); }}
  section {{ margin: 0 0 16px; padding: 20px 22px; border-radius: 18px; background: var(--card); border: 1px solid var(--line); }}
  h2 {{ margin: 0 0 10px; font-size: 1.1rem; letter-spacing: -.01em; }}
  p {{ margin: 0 0 10px; }} p:last-child {{ margin-bottom: 0; }}
  ol, ul {{ margin: 0; padding-left: 1.3em; }} li {{ margin: 0 0 8px; }} li:last-child {{ margin-bottom: 0; }}
  b {{ font-weight: 700; }} .muted {{ color: var(--muted); }}
  code {{ padding: 1px 6px; border-radius: 6px; background: var(--soft); border: 1px solid var(--line); font-size: .88em; }}
  .alerts {{ display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }}
  .alerts li {{ margin: 0; padding: 12px 14px; border-radius: 12px; background: var(--soft); }}
  .alerts li > b:first-child {{ display: block; }}
  .tag {{ display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--brand-bg); color: var(--brand);
          font-size: .82rem; font-weight: 700; }}
  a {{ color: var(--brand); font-weight: 600; }}
  footer {{ margin-top: 22px; font-size: .85rem; color: var(--muted); }}
</style></head>
<body><main>
<header>{logo_svg("guide")}<div><h1>How to use {APP_NAME}</h1>
  <p>Get an alert on your phone the moment tickets open on District, at your cinema or anywhere in your city.</p></div></header>

<section>
  <h2>1. Set up a watcher</h2>
  <ol>
    <li><b>City and cinema.</b> Pick your city, then a cinema. Pick <b>Any cinema in &lt;city&gt;</b> to hear the moment your
      movie opens anywhere there. A cinema that isn't listed? Paste its link from district.in under <b>Or paste a cinema link</b>.</li>
    <li><b>Movie.</b> Pick one, or type the name and press Enter if bookings haven't opened yet. Part of the name is enough,
      and you can add more than one.</li>
    <li><b>Format.</b> IMAX, 4DX, 3D, 2D, your own, or ANY.</li>
    <li><b>Show date.</b> One day, or up to {MAX_RANGE_DAYS} in a row with <b>A few days</b>. Times are Indian Standard Time.</li>
    <li><b>Show time.</b> Tick morning, afternoon, evening or night, or leave them all unticked for any time.</li>
    <li><b>Preview shows</b> shows what's listed right now, so you can check your filters before you start.</li>
  </ol>
</section>

<section>
  <h2>2. Choose where alerts go</h2>
  <p>Fill in one or more. ntfy is the quickest.</p>
  <ul>
    <li><b>ntfy:</b> install the free ntfy app, tap <b>+</b> and subscribe to a topic, then enter the same topic in BookTick.
      Pick a name nobody would guess, like <code>arun_tickets_982</code>: anyone who knows it can read your alerts.</li>
    <li><b>Telegram:</b> create a bot with <b>@BotFather</b> and send it any message. Get your chat ID from
      <b>@userinfobot</b>, then enter the bot token and chat ID.</li>
    <li><b>Discord:</b> in your server, <b>Edit channel → Integrations → Webhooks → New webhook</b>, then copy its URL.</li>
  </ul>
  <p>Tap <b>Send test alert</b> to check it reaches you, then <b>Start watching</b>.</p>
</section>

<section>
  <h2>3. What you'll get</h2>
  <ul class="alerts">
    <li><b>🚨 Tickets open</b> As soon as a matching show has seats. Tap it to open the theatre's page for that date.
      It repeats as reminders (set by <b>Alerts when bookings open</b>) until you tap a button in it.</li>
    <li><b>🗺️ Seat map</b> A few seconds later, a picture of each matching show's seats (up to {MAX_SEATMAPS}): outlined seats
      are free, grey ones are sold or blocked, and the screen is at the bottom. It's just for looking, so tapping it does nothing.</li>
    <li><b>⏸️ Bookings closed</b> If the shows sell out or are taken off sale, BookTick tells you and keeps watching.</li>
    <li><b>🔁 Bookings reopened</b> and <b>🎟️ More seats</b> If they come back, or a show suddenly gets more seats
      (at most once every 15 minutes).</li>
  </ul>
  <p style="margin-top:12px">ntfy alerts have two buttons: <span class="tag">Booked it, stop watching</span> and
    <span class="tag">Got it, keep watching</span> (which stops the reminders but keeps watching). Telegram and Discord
    alerts have a link to a page with the same two buttons.</p>
</section>

<section>
  <h2>4. Manage your watchers</h2>
  <ul>
    <li>The <b>Watchers</b> tab shows each one: where bookings stand, the last check, alerts sent and where alerts go.</li>
    <li><b>Pause</b> or <b>Resume</b> checking, <b>Re-arm</b> to start over, <b>Test</b> to send a test alert, or <b>Delete</b>.</li>
    <li>A watcher stops by itself once its dates pass, and is removed a week later.</li>
    <li>You can have {MAX_WATCHERS_PER_PERSON} watchers going at once.</li>
  </ul>
</section>

<section>
  <h2>5. Good to know</h2>
  <ul>
    <li><b>Your watchers belong to this browser.</b> To see them on another device, use <b>Copy my link</b> on the Watchers
      tab and open it there. Keep that link to yourself: anyone with it can manage your watchers. If you clear this
      browser's data, you lose access to them, but they keep running and alerting.</li>
    <li><b>Be ready to book.</b> Stay signed in to District on your phone, so you can book straight away when an alert comes.</li>
    <li>{RESTART_TIP}</li>
  </ul>
</section>

<footer>{APP_NAME} is an independent project and isn't affiliated with District or Zomato.</footer>
</main></body></html>"""
