"""The "How to use" tab: the full guide, opened from the tab bar or the header button."""
from .config import (MAX_CINEMAS_PER_WATCHER, MAX_RANGE_DAYS, MAX_SEATMAPS, MAX_TRACKED_SHOWS, MAX_TRACKERS_PER_PERSON,
                     MAX_WATCHERS_PER_PERSON, NEW_SHOWS_GAP, SEATS_BACK_GAP, SEATS_MIN_DEFAULT)
from .views import RESTART_TIP

GUIDE_HTML = f"""
<div class="bt-guide">
<section>
  <h2>1. Set up a watcher</h2>
  <ol>
    <li><b>Movie.</b> Tap <b>Choose a movie</b> and pick a poster: <b>Now showing</b> puts your cinemas' movies first,
      and <b>Coming soon</b> has every upcoming movie on District with its release date. Search by name or filter by
      language. Each movie gets its own watcher, so pick two and you get two. Not on District yet? Type its name and
      tap <b>Watch for it anyway</b>.</li>
    <li><b>Where.</b> Pick up to {MAX_CINEMAS_PER_WATCHER} cinemas in your city, and you hear from whichever opens first
      (then again if another one opens). Or pick <b>Anywhere in &lt;city&gt;</b> to hear the moment it opens at any
      cinema there, then which cinemas open after it (a few minutes apart at most, so a rush is one alert). Pick both, and you hear the moment it opens anywhere, then again when each of your cinemas
      opens. A cinema that isn't listed? Paste its link from district.in under <b>Or paste a cinema link</b>.</li>
    <li><b>BookMyShow-only cinemas.</b> Cinemas that sell only on BookMyShow, like Rohini or GK Cinemas, are in the
      cinema list too, marked <b>BookMyShow</b> (or paste a cinema's link from bookmyshow.com). You get the same alerts
      when bookings open, close and reopen there, and they open the cinema on BookMyShow. A cinema on both is always
      watched on District, which BookTick reads more reliably, even if you paste its BookMyShow link. BookMyShow only
      says how full a show is (filling fast, almost full), not how many seats are left, so <b>Seats released</b>
      alerts, seat maps and seat trackers are District only. <b>Anywhere in &lt;city&gt;</b> covers District's cinemas;
      tick BookMyShow ones as well to hear about them.</li>
    <li><b>Format.</b> Tick the ones you'd watch, like IMAX and 4DX, or leave them all unticked for any format.</li>
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
    <li><b>🚨 Bookings open</b> As soon as a matching show has seats and its seat page opens. (District sometimes lists
      shows before the cinema takes bookings, and tapping one says <i>This showtime isn't available right now</i>:
      BookTick checks, and waits for those.) Tap it to open the theatre's page for that date.
      It repeats as reminders (set by <b>Alerts when bookings open</b>) until you tap a button in it.</li>
    <li><b>🏛️ New cinema open</b> Watching a few cinemas, or a whole city? Each cinema that opens later is named
      when its first shows open (across a city, ones that open within a few minutes share an alert).</li>
    <li><b>🆕 New shows added</b> A cinema that's already open adds shows, like a late-night IMAX one
      (at most once every {NEW_SHOWS_GAP // 60} minutes; shows added in between come in the next one).</li>
    <li><b>🎟️ Seats released</b> Seats come back in a show that's already listed, unblocked by the cinema or back
      from sold out. The <b>Seats released alerts</b> slider sets how many seats a show must get back at once
      ({SEATS_MIN_DEFAULT} unless you change it, 0 for none): booking for 10? Set 10, and a seat or two coming back
      (usually someone's payment that didn't go through) won't disturb you. You can change it on the watcher's card
      later. Each line says how many (at most once every {SEATS_BACK_GAP // 60} minutes).</li>
    <li><b>🗺️ Seat map</b> A few seconds later, a picture of each matching show's seats (up to {MAX_SEATMAPS}): outlined seats
      are free, grey ones are sold or blocked, and the screen is at the bottom. It's just for looking, so tapping it does nothing.</li>
    <li><b>⏸️ Cinema closed · 🔁 Cinema reopened</b> Watching a few cinemas? If one sells out or takes its shows
      off sale while others still have seats, you hear which, and again if it gets seats back.</li>
    <li><b>⏸️ Bookings closed</b> If the shows sell out or are taken off sale everywhere you watch, BookTick tells you
      and keeps watching. Shows that simply start (the day's last one, say) don't count as closing.</li>
    <li><b>🔁 Bookings reopened</b> If they come back after that.</li>
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
    <li><b>Edit</b> opens a watcher in the form, so you can change anything: the movie, cinemas, formats, dates, show
      times, alerts or where they go. Changing what it looks for starts it over (a watcher that finished watches
      again); a saved Telegram bot token or Discord webhook shows as <b>(saved)</b>, and stays unless you change it.</li>
    <li>A watcher stops by itself once its dates pass, and is removed a week later.</li>
    <li>You can have {MAX_WATCHERS_PER_PERSON} watchers going at once.</li>
  </ul>
</section>

<section>
  <h2>5. Track how seats are blocked</h2>
  <p>Some cinemas hold seats back and release them shortly before the show. The <b>Seat tracker</b> tab follows one
    cinema's shows for a day (every format, or just one, and every movie or just one) and works out when that happens.</p>
  <ul>
    <li>It notes every show's seats left on each check, and reads which seats changed whenever the count moves.</li>
    <li>After the last show starts, it sends a report to your alert channels: how many seats came back, when (how long
      before showtime), and where in the hall. The card's <b>Report so far</b> button shows it any time.</li>
    <li>District only says whether a seat is free or taken. Seats that come back were released by the cinema, or held
      by someone who didn't finish paying (a few seats, back within minutes). Many seats going at once were held back
      by the cinema, or booked by a big group.</li>
    <li>It follows up to {MAX_TRACKED_SHOWS} shows, and you can have {MAX_TRACKERS_PER_PERSON} trackers going at once.</li>
  </ul>
</section>

<section>
  <h2>6. Good to know</h2>
  <ul>
    <li><b>Your watchers belong to this browser.</b> To see them on another device, use <b>Copy my link</b> on the Watchers
      tab and open it there. Keep that link to yourself: anyone with it can manage your watchers. If you clear this
      browser's data, you lose access to them, but they keep running and alerting.</li>
    <li><b>Be ready to book.</b> Stay signed in to District (and BookMyShow, if you watch cinemas there) on your phone, so
      you can book straight away when an alert comes.</li>
    <li>{RESTART_TIP}</li>
  </ul>
</section>
</div>"""
