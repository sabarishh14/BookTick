"""The "How to use" tab: the full guide, opened from the tab bar or the header button."""
from .config import MAX_RANGE_DAYS, MAX_SEATMAPS, MAX_WATCHERS_PER_PERSON
from .views import RESTART_TIP

GUIDE_HTML = f"""
<div class="bt-guide">
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
    <li><b>🔁 Bookings reopened · 🎟️ More seats</b> If they come back, or a show suddenly gets more seats
      (at most once every 15 minutes).</li>
  </ul>
  <p class="bt-guide-after">ntfy alerts have two buttons: <span class="tag">Booked it, stop watching</span> and
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
</div>"""
