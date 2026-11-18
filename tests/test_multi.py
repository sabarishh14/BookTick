"""One movie per watcher, at up to a few cinemas, in any of a few formats. No network."""
import json
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from booktick import access, engine, views
from booktick.core import IST, movie_list, now_ist, state, task_cinemas, task_formats, today_ist

try:
    import gradio
except ImportError:  # the CI unit job doesn't install Gradio
    gradio = None

PVR = "https://www.district.in/movies/pvr-palazzo-chennai-CD1022274"
INOX = "https://www.district.in/movies/inox-phoenix-velachery-chennai-CD1020779"
AGS = "https://www.district.in/movies/ags-villivakkam-chennai-CD9505"


def show(cinema, fmt="IMAX 3D", avail=10, hour=19, n=1):
    start = datetime.strptime(f"{today_ist()} {hour:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    return {"title": "Avengers: Doomsday", "cinema": cinema, "cinema_url": None, "format": fmt, "start": start, "audi": "",
            "avail": avail, "total": 300, "price": 300.0,
            "url": f"https://www.district.in/movies/seat-layout/x?encsessionid=1-{cinema}{n}-a-b"}


def watcher(**extra):
    task = {"id": "w1", "owner": "o", "user_label": "Me", "status": "Active", "scope": "cinema", "phase": "waiting",
            "cinemas": [{"url": PVR, "name": "PVR Palazzo"}, {"url": INOX, "name": "INOX Velachery"},
                        {"url": AGS, "name": "AGS Villivakkam"}],
            "movie_filter": "Avengers: Doomsday", "formats": ["IMAX", "4DX"], "time_slots": [], "target_date": today_ist(),
            "max_alerts": 1, "ntfy_topic": "t"}
    task.update(extra)
    return task


class Fields(unittest.TestCase):
    def test_watchers_from_before_still_read(self):
        old = {"scope": "cinema", "cinema_url": PVR, "cinema_name": "PVR", "format_filter": "IMAX"}
        self.assertEqual(task_cinemas(old), [{"url": PVR, "name": "PVR"}])
        self.assertEqual(task_formats(old), ["IMAX"])
        self.assertEqual(task_formats({"format_filter": "ANY"}), [])
        self.assertEqual(task_cinemas({"scope": "city", "city": "chennai"}), [])
        self.assertEqual(task_cinemas({"scope": "city", "city": "chennai", "cinema_url": PVR}), [])  # old city watchers
        self.assertEqual(task_cinemas({"scope": "city", "cinemas": [{"url": PVR, "name": "PVR"}]}), [{"url": PVR, "name": "PVR"}])

    def test_formats_any_of(self):
        self.assertTrue(engine.show_matches(show("PVR", "4DX 3D"), [], ["IMAX", "4DX"]))
        self.assertFalse(engine.show_matches(show("PVR", "2D"), [], ["IMAX", "4DX"]))
        self.assertTrue(engine.show_matches(show("PVR", "2D"), [], []))
        self.assertTrue(engine.show_matches(show("PVR", "IMAX 2D"), [], "IMAX"))  # the old single format

    def test_screen_features_are_found_in_screen_and_cinema_names(self):
        laser = dict(show("Kasi RGB Laser Dolby Atmos", "2D"), audi="KASI THEATRE")
        self.assertTrue(engine.show_matches(laser, [], ["Dolby Atmos"]))
        self.assertTrue(engine.show_matches(laser, [], ["Laser"]))
        self.assertFalse(engine.show_matches(laser, [], ["4K"]))
        self.assertTrue(engine.show_matches(dict(show("PVR"), audi="AUDI 2 RECLINER"), [], ["Recliner"]))  # typed in
        self.assertFalse(engine.show_matches(dict(show("PVR", "2D"), audi="SERVICE LOUNGE"), [], ["ICE"]))  # whole words
        self.assertFalse(engine.show_matches(show("Cinema 3D 7.1", "2D"), [], ["3D"]))  # 3D is the show's, not the cinema's

    def test_each_city_gets_its_own_format_chips(self):
        directory = {"cinemas": [{"name": "Kasi RGB Laser Dolby Atmos"}, {"name": "Bharath 4K RGB Laser"},
                                 {"name": "Murugan Cinemas PLF 4K"}, {"name": "Sangam Laser"}]}
        self.assertEqual(views.city_formats(directory), ["IMAX", "4DX", "3D", "2D", "Laser", "4K", "Dolby Atmos", "PLF"])
        self.assertEqual(views.city_formats(None), ["IMAX", "4DX", "3D", "2D"])

    def test_the_name_and_topic_this_browser_used_last(self):
        self.assertEqual(access.saved_defaults(json.dumps({"name": "  Sabs ", "ntfy": "ntfy.sh/sabs_tickets"})),
                         ("Sabs", "sabs_tickets"))
        self.assertEqual(access.saved_defaults(json.dumps({"name": "", "ntfy": "bad topic!"})), ("", ""))
        self.assertEqual(access.saved_defaults("not json"), ("", ""))

    def test_messages_say_what_happened(self):
        from booktick import alerts
        s = dict(show("PVR"), was=5, avail=40)
        head, intro = alerts.wording("seats_back", "Avengers", [s], 1, 1, None, place="PVR Palazzo")
        self.assertEqual(head, "🎟️ Seats released: Avengers")
        self.assertIn("PVR Palazzo released seats in 1 show", intro)
        self.assertIn("+35 seats · 40 of 300 seats left", alerts.show_lines([s])[0])
        self.assertIn("back from sold out", alerts.show_lines([dict(s, was=0)])[0])
        head, intro = alerts.wording("new_shows", "Avengers", [s, s], 1, 1, None, place="PVR Palazzo")
        self.assertEqual(head, "🆕 New shows added: Avengers")
        self.assertIn("PVR Palazzo added 2 new shows", intro)
        head, intro = alerts.wording("also_open", "Avengers", [s], 1, 1, None, place="INOX Velachery")
        self.assertEqual(head, "🏛️ New cinema open: Avengers")
        self.assertIn("INOX Velachery just opened bookings for the first time", intro)

    def test_picked_movies(self):
        self.assertEqual(movie_list('["Avengers: Doomsday", "Superhero", "Superhero", " "]'), ["Avengers: Doomsday", "Superhero"])


class Checking(unittest.TestCase):
    def setUp(self):
        self.task = watcher()
        state["tasks"] = {"w1": self.task}
        engine._results.clear()
        self.send = mock.patch("booktick.engine.send_task_notification", return_value=[]).start()
        mock.patch("booktick.engine.save_tasks").start()
        mock.patch("booktick.engine.request_seatmap").start()
        mock.patch("booktick.engine.onsale.verdict", return_value=("yes", "")).start()  # no browser here
        self.day_end = mock.patch("booktick.engine.DAY_END_MARGIN", -10 ** 9)  # shows count as ahead, any hour
        self.day_end.start()
        engine.plan([self.task], today_ist())
        self.pages = {c["url"]: engine.district.query_url(c["url"], today_ist()) for c in self.task["cinemas"]}

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def check(self, url, shows, place):
        engine.check_task(self.task, self.pages[url], today_ist(), place, shows)

    def kinds(self):
        return [c.kwargs["kind"] for c in self.send.call_args_list]

    def test_one_page_per_cinema_and_day(self):
        jobs = engine.page_jobs(self.task, today_ist())
        self.assertEqual(sorted(page for _, page, _ in jobs), sorted(self.pages.values()))

    def open_two(self):
        """PVR opens, then INOX; AGS has nothing yet."""
        self.check(PVR, [show("PVR")], "PVR Palazzo")
        self.check(INOX, [show("INOX")], "INOX Velachery")
        self.check(AGS, [], "AGS Villivakkam")
        self.task["settle_until"] = 0
        self.assertEqual(self.kinds(), ["opened", "also_open"])

    def test_another_cinema_waits_for_district_to_take_bookings(self):
        self.check(PVR, [show("PVR")], "PVR Palazzo")
        with mock.patch("booktick.engine.onsale.verdict", return_value=("no", "")):
            self.check(INOX, [show("INOX")], "INOX Velachery")
        self.assertEqual(self.kinds(), ["opened"])
        self.assertEqual(dict((n, s) for n, _, s, _ in engine.cinema_statuses("w1"))["INOX Velachery"], "waiting")
        self.task["last_alert_ts"] = 0
        self.check(INOX, [show("INOX")], "INOX Velachery")
        self.assertEqual(self.kinds(), ["opened", "also_open"])

    def test_one_cinema_sells_out_while_another_is_open(self):
        self.open_two()
        self.check(PVR, [show("PVR", avail=0)], "PVR Palazzo")
        self.assertEqual(self.kinds()[-1], "cinema_closed")
        self.assertEqual((self.send.call_args.args[2], self.send.call_args.kwargs["reason"]), ("PVR Palazzo", "sold_out"))
        self.check(PVR, [show("PVR", avail=6)], "PVR Palazzo")
        self.assertEqual(self.kinds()[-1], "cinema_reopened")  # not "new cinema open": it opened before

    def test_one_cinema_takes_its_shows_off_sale(self):
        self.open_two()
        self.check(INOX, [], "INOX Velachery")
        self.assertEqual(self.kinds()[-1], "cinema_closed")
        self.assertEqual(self.send.call_args.kwargs["reason"], "pulled")
        self.assertEqual(self.send.call_args.kwargs["cinema_url"], INOX)  # links INOX, not the first cinema
        statuses = {name: status for name, _, status, _ in engine.cinema_statuses("w1")}
        self.assertEqual(statuses["INOX Velachery"], "offsale")
        self.assertIn("Off sale", views.watcher_card("w1", self.task, {"owner": "o"}))

    def test_seats_blinking_at_zero_dont_spam(self):
        self.open_two()
        self.check(PVR, [show("PVR", avail=0)], "PVR Palazzo")
        self.check(PVR, [show("PVR", avail=1)], "PVR Palazzo")
        self.check(PVR, [show("PVR", avail=0)], "PVR Palazzo")  # again, a minute later
        self.check(PVR, [show("PVR", avail=1)], "PVR Palazzo")
        self.assertEqual(self.kinds(), ["opened", "also_open", "cinema_closed", "cinema_reopened"])

    def test_after_everything_closed_each_cinema_says_when_it_reopens(self):
        self.open_two()
        self.check(PVR, [show("PVR", avail=0)], "PVR Palazzo")
        self.check(INOX, [show("INOX", avail=0)], "INOX Velachery")
        self.assertEqual(self.kinds()[-1], "closed")
        self.check(PVR, [show("PVR", avail=4)], "PVR Palazzo")
        self.check(INOX, [show("INOX", avail=4)], "INOX Velachery")
        self.assertEqual(self.kinds()[-2:], ["reopened", "cinema_reopened"])

    def test_a_cinema_whose_shows_have_started_is_quiet(self):
        self.open_two()
        self.day_end.stop()
        self.task["page_last_start"][self.pages[PVR]] = time.time() - 3600  # its last show began an hour ago
        self.check(PVR, [], "PVR Palazzo")
        self.assertEqual(self.kinds(), ["opened", "also_open"])  # its day ended; nothing closed

    def test_first_cinema_to_open_alerts_then_another_says_also_open(self):
        self.check(PVR, [show("PVR")], "PVR Palazzo")
        self.check(INOX, [], "INOX Velachery")
        self.check(AGS, [], "AGS Villivakkam")
        self.assertEqual(self.kinds(), ["opened"])
        self.assertEqual(self.send.call_args.args[2], "PVR Palazzo")   # named after the cinema that opened
        self.check(INOX, [show("INOX", "4DX 3D")], "INOX Velachery")
        self.assertEqual(self.kinds(), ["opened", "also_open"])
        self.assertEqual(self.send.call_args.args[2], "INOX Velachery")
        statuses = {name: status for name, _, status, _ in engine.cinema_statuses("w1")}
        self.assertEqual(statuses, {"PVR Palazzo": "open", "INOX Velachery": "open", "AGS Villivakkam": "waiting"})

    def test_a_cinema_already_open_adds_a_show(self):
        self.check(PVR, [show("PVR")], "PVR Palazzo")
        self.task["settle_until"] = 0
        self.check(INOX, [], "INOX Velachery")
        self.check(PVR, [show("PVR"), show("PVR", n=2, hour=22)], "PVR Palazzo")
        self.assertEqual(self.kinds(), ["opened", "new_shows"])
        self.check(INOX, [show("INOX")], "INOX Velachery")  # INOX's first shows: a new cinema, not new shows
        self.assertEqual(self.kinds(), ["opened", "new_shows", "also_open"])

    def test_closed_only_once_every_cinema_has_no_seats(self):
        self.check(PVR, [show("PVR")], "PVR Palazzo")
        self.check(INOX, [show("INOX", avail=4)], "INOX Velachery")
        self.check(AGS, [], "AGS Villivakkam")
        self.check(PVR, [show("PVR", avail=0)], "PVR Palazzo")
        self.assertNotIn("closed", self.kinds())                      # INOX still has seats
        self.check(INOX, [show("INOX", avail=0)], "INOX Velachery")
        self.assertEqual(self.kinds()[-1], "closed")
        self.assertIn("and 2 more cinemas", self.send.call_args.args[2])  # every cinema closed: name them all

    def test_cinemas_get_districts_own_names(self):
        self.check(AGS, [], "AGS Cinemas: Villivakkam")
        self.assertEqual(self.task["cinemas"][2]["name"], "AGS Cinemas: Villivakkam")

    def test_a_watcher_from_before_is_moved_to_the_new_fields(self):
        old = {"id": "w2", "owner": "o", "status": "Active", "scope": "cinema", "phase": "waiting", "cinema_url": PVR,
               "cinema_name": "PVR", "movie_filter": "Avengers", "format_filter": "IMAX", "target_date": today_ist()}
        state["tasks"]["w2"] = old
        engine.plan([old], today_ist())
        engine.check_task(old, engine.district.query_url(PVR, today_ist()), today_ist(), "PVR Palazzo", [show("PVR")])
        self.assertEqual(old["cinemas"], [{"url": PVR, "name": "PVR Palazzo"}])
        self.assertEqual(self.kinds(), ["opened"])


MOVIE = "https://www.district.in/movies/avengers-doomsday-movie-tickets-MV123"


class AnywhereAndCinemas(unittest.TestCase):
    """Anywhere in the city, plus a couple of cinemas that each get their own alert."""

    def setUp(self):
        self.task = watcher(scope="city", city="chennai", cinemas=[{"url": PVR, "name": "PVR Palazzo"},
                                                                   {"url": INOX, "name": "INOX Velachery"}])
        state["tasks"] = {"w1": self.task}
        engine._results.clear()
        self.send = mock.patch("booktick.engine.send_task_notification", return_value=[]).start()
        mock.patch("booktick.engine.save_tasks").start()
        mock.patch("booktick.engine.request_seatmap").start()
        mock.patch("booktick.engine.onsale.verdict", return_value=("yes", "")).start()  # no browser here
        self.day_end = mock.patch("booktick.engine.DAY_END_MARGIN", -10 ** 9)  # shows count as ahead, any hour
        self.day_end.start()
        listing = {"value": {"movies": [{"name": "Avengers: Doomsday", "url": MOVIE}]}, "age": 0}
        mock.patch("booktick.engine.district.peek", return_value=listing).start()
        engine.plan([self.task], today_ist())
        self.city_page = engine.district.city_movie_url(MOVIE, "chennai", today_ist())
        self.pages = {c["url"]: engine.district.query_url(c["url"], today_ist()) for c in self.task["cinemas"]}

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def kinds(self):
        return [c.kwargs["kind"] for c in self.send.call_args_list]

    def test_checks_the_city_and_each_cinema(self):
        kinds = sorted(kind for kind, _, _ in engine.page_jobs(self.task, today_ist()))
        self.assertEqual(kinds, ["cinema", "cinema", "city"])
        self.assertEqual(len(self.task["expected_pages"]), 3)

    def test_opens_anywhere_then_says_when_a_cinema_of_yours_opens(self):
        engine.check_task(self.task, self.city_page, today_ist(), "Cinemas in Chennai", [show("Kasi")])
        self.assertEqual(self.kinds(), ["opened"])
        self.assertEqual(self.send.call_args.args[2], "Kasi")  # the cinema that opened, not just the city
        engine.check_task(self.task, self.pages[PVR], today_ist(), "PVR Palazzo", [])
        engine.check_task(self.task, self.pages[INOX], today_ist(), "INOX Velachery", [show("INOX")])
        self.assertEqual(self.kinds(), ["opened", "also_open"])
        self.assertEqual(self.send.call_args.args[2], "INOX Velachery")
        engine.check_task(self.task, self.pages[PVR], today_ist(), "PVR Palazzo", [show("PVR")])
        self.assertEqual(self.kinds(), ["opened", "also_open", "also_open"])  # each cinema gets its own
        self.assertEqual(self.send.call_args.args[2], "PVR Palazzo")

    def test_a_cinema_of_yours_opening_first_is_named(self):
        engine.check_task(self.task, self.pages[PVR], today_ist(), "PVR Palazzo", [show("PVR")])
        self.assertEqual(self.kinds(), ["opened"])
        self.assertEqual(self.send.call_args.args[2], "PVR Palazzo")

    def test_two_movies_with_the_name_add_up_on_the_card(self):
        other = engine.district.city_movie_url(MOVIE.replace("MV123", "MV456"), "chennai", today_ist())
        self.task["expected_pages"] = self.task["expected_pages"] + [other]
        engine.check_task(self.task, self.city_page, today_ist(), "Cinemas in Chennai", [show("Kasi"), show("AGS", n=2)])
        engine.check_task(self.task, other, today_ist(), "Cinemas in Chennai", [])
        self.assertEqual(self.task["last_result"], "🔥 2 shows open")  # not the empty page checked last

    def test_the_card_keeps_the_citys_result(self):
        engine.check_task(self.task, self.city_page, today_ist(), "Cinemas in Chennai", [show("Kasi", avail=0)])
        engine.check_task(self.task, self.pages[PVR], today_ist(), "PVR Palazzo", [])
        self.assertTrue(self.task["last_result"].startswith("Sold out"))
        card = views.watcher_card("w1", self.task, {"owner": "o"})
        self.assertIn("Any cinema in Chennai", card)
        self.assertEqual(card.count("<li"), 2)
        self.assertIn("each of these opens", card)


def at(cinema, cid, n=1, avail=10):
    """A show on a city page, which links its cinema."""
    return dict(show(cinema, n=n, avail=avail), cinema_url=f"https://www.district.in/movies/{cinema.lower()}-CD{cid}")


class AcrossTheCity(unittest.TestCase):
    """Anywhere in the city: after the first alert, the cinemas that open next are named."""

    def setUp(self):
        self.task = watcher(scope="city", city="chennai", cinemas=[], max_alerts=1)
        state["tasks"] = {"w1": self.task}
        engine._results.clear()
        self.send = mock.patch("booktick.engine.send_task_notification", return_value=[]).start()
        mock.patch("booktick.engine.save_tasks").start()
        mock.patch("booktick.engine.request_seatmap").start()
        mock.patch("booktick.engine.onsale.verdict", return_value=("yes", "")).start()  # no browser here
        self.day_end = mock.patch("booktick.engine.DAY_END_MARGIN", -10 ** 9)  # shows count as ahead, any hour
        self.day_end.start()
        listing = {"value": {"movies": [{"name": "Avengers: Doomsday", "url": MOVIE}]}, "age": 0}
        mock.patch("booktick.engine.district.peek", return_value=listing).start()
        engine.plan([self.task], today_ist())
        self.page = engine.district.city_movie_url(MOVIE, "chennai", today_ist())

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def check(self, *shows):
        engine.check_task(self.task, self.page, today_ist(), "Cinemas in Chennai", list(shows))

    def wait(self):
        """As if a few minutes went by (since the alert, and since the cinemas named so far)."""
        self.task["last_spread_ts"] -= engine.CITY_SPREAD_GAP
        self.task["told_cinemas"] = {k: (at - engine.CITY_SPREAD_GAP if at else 0)
                                     for k, at in engine.told_cinemas(self.task).items()}

    def kinds(self):
        return [c.kwargs["kind"] for c in self.send.call_args_list]

    def test_the_next_cinemas_to_open_are_named_together(self):
        self.check(at("Kasi", 1))
        self.assertEqual(self.kinds(), ["opened"])
        self.check(at("Kasi", 1), at("AGS", 2))
        self.check(at("Kasi", 1), at("AGS", 2), at("Rohini", 3), at("Rohini", 3, n=2))
        self.assertEqual(self.kinds(), ["opened"])  # opened a moment ago: these wait, and no "more seats" either
        self.wait()
        self.check(at("Kasi", 1), at("AGS", 2), at("Rohini", 3), at("Rohini", 3, n=2))
        self.assertEqual(self.kinds(), ["opened", "spread"])
        self.assertEqual(self.send.call_args.args[2], "2 cinemas in Chennai")  # the alert lists them
        self.assertEqual(len(self.send.call_args.args[1]), 3)  # their shows only, not Kasi's
        self.wait()
        self.check(at("Kasi", 1), at("AGS", 2), at("Rohini", 3))
        self.assertEqual(self.kinds(), ["opened", "spread"])  # each cinema once

    def test_watchers_open_from_before_dont_repeat_whats_open(self):
        self.task.update(phase="open", reminders_left=0)
        self.check(at("Kasi", 1), at("AGS", 2))
        self.assertEqual(self.kinds(), [])
        self.task["told_quiet_until"] = 0
        self.check(at("Kasi", 1), at("AGS", 2), at("Rohini", 3))
        self.assertEqual(self.kinds(), ["spread"])
        self.assertEqual(self.send.call_args.args[2], "Rohini")

    def test_a_named_cinema_adding_shows_or_releasing_seats(self):
        self.check(at("Kasi", 1, avail=5))
        self.task["settle_until"] = 0
        self.check(at("Kasi", 1, avail=5), at("Kasi", 1, n=2))  # Kasi added a show
        self.assertEqual(self.kinds(), ["opened", "new_shows"])
        self.assertEqual(self.send.call_args.args[2], "Kasi")
        self.check(at("Kasi", 1, avail=40), at("Kasi", 1, n=2))  # 35 seats unblocked
        self.assertEqual(self.kinds(), ["opened", "new_shows", "seats_back"])
        self.assertEqual(self.send.call_args.args[1][0]["was"], 5)

    def test_a_named_cinema_opening_another_day_is_new_shows(self):
        tomorrow = str(now_ist().date() + timedelta(days=1))
        page2 = engine.district.city_movie_url(MOVIE, "chennai", tomorrow)
        later = lambda s: dict(s, start=s["start"] + timedelta(days=1), url=s["url"] + "-t")  # noqa: E731
        self.check(at("Kasi", 1))
        self.task["settle_until"] = 0
        engine.check_task(self.task, page2, tomorrow, "Cinemas in Chennai", [])
        engine.check_task(self.task, page2, tomorrow, "Cinemas in Chennai", [later(at("Kasi", 1))])
        self.assertEqual(self.kinds(), ["opened", "new_shows"])  # like a watcher on Kasi would say

    def test_the_card_shows_where_its_open_and_the_alerts_sent(self):
        self.check(at("Kasi", 1))
        self.wait()
        self.check(at("Kasi", 1), at("AGS", 2), at("AGS", 2, n=2))
        log = self.task["alert_log"]
        self.assertEqual([(r["kind"], r["place"]) for r in log], [("opened", "Kasi"), ("spread", "AGS")])
        self.assertIn("kasi-CD1?fromdate=", log[0]["link"])  # one cinema: the alert links to it
        card = views.watcher_card("w1", self.task, {"owner": "o"})
        self.assertIn("Open at 2 cinemas", card)
        self.assertIn("Open now at 2 cinemas", card)
        self.assertRegex(card, r"2 shows · \d{1,2}:\d{2} [AP]M<")  # AGS opened after the first alert: when
        self.assertIn("1 show</span>", card)           # Kasi was in the first alert
        self.assertLess(card.index(">AGS<"), card.index(">Kasi<"))
        self.assertIn("ags-CD2?fromdate=", card)       # each cinema links to its page
        self.assertIn("New cinema open", card)         # the alerts sent, newest first
        self.assertIn('class="bt-sent"', card)

    def test_watchers_saved_with_a_list_still_work(self):
        # An earlier version saved told_cinemas as a list of keys; reading it as a dict crashed.
        self.task.update(phase="open", reminders_left=0, told_cinemas=[f"{today_ist()}|1"])
        self.check(at("Kasi", 1), at("AGS", 2))
        self.assertEqual(self.kinds(), ["spread"])  # Kasi was told before; AGS is new
        self.assertEqual(self.send.call_args.args[2], "AGS")
        self.assertIsInstance(self.task["told_cinemas"], dict)  # saved in the new form
        self.task["told_cinemas"] = [f"{today_ist()}|1"]
        self.assertIn("Open now at 2 cinemas", views.watcher_card("w1", self.task, {"owner": "o"}))

    def test_a_card_that_cant_be_drawn_doesnt_break_the_tab(self):
        with mock.patch("booktick.views.full_watcher_card", side_effect=ValueError("bad")), \
                mock.patch("booktick.views.traceback.print_exc"):
            card = views.watcher_card("w1", self.task, {"owner": "o"})
        self.assertIn("Avengers: Doomsday", card)
        self.assertIn("still being checked", card)

    def test_a_long_list_folds_away(self):
        self.check(*[at(f"Cinema{i}", i) for i in range(8)])
        card = views.watcher_card("w1", self.task, {"owner": "o"})
        self.assertIn("Open now at 8 cinemas", card)
        self.assertIn("3 more cinemas</summary>", card)

    def test_the_alert_names_every_cinema(self):
        from booktick import alerts
        one = alerts.show_lines([at("Kasi", 1), at("Kasi", 1, n=2)], city="chennai")
        self.assertEqual(len(one), 2)  # one cinema: a line per show (the alert's place line names it)
        names = [f"Cinema {i}, Velachery, Chennai" for i in range(10)]
        lines = alerts.show_lines([dict(at("X", i), cinema=n) for i, n in enumerate(names)], city="chennai")
        self.assertEqual(lines[0].split(" · ")[0], "• Cinema 0, Velachery")  # the city is cut off the end
        self.assertIn("7:00 PM", lines[0])  # still says when, even when shortened to a line per cinema
        self.assertEqual(len(lines), alerts.CINEMA_LINES + 1)
        self.assertEqual(lines[-1], "…and 2 more cinemas")

    def test_a_few_shows_at_two_cinemas_are_each_listed(self):
        from booktick import alerts
        shows = [dict(at("Kasi", 1, avail=12), was=10), dict(at("Kasi", 1, n=2, avail=8), was=0),
                 dict(at("AGS, Chennai", 2, avail=50), was=40)]
        shows[1]["start"] = shows[1]["start"].replace(hour=22, minute=45)
        lines = alerts.show_lines(shows, city="chennai", kind="seats_back")
        self.assertEqual(lines[0], "📍 Kasi")
        self.assertIn("7:00 PM", lines[1])
        self.assertIn("+2 seats", lines[1])
        self.assertIn("10:45 PM", lines[2])
        self.assertIn("back from sold out", lines[2])
        self.assertEqual(lines[3:5], ["", "📍 AGS"])
        self.assertIn("+10 seats", lines[5])
        self.assertEqual(len(lines), 6)

    def test_times_in_short(self):
        from booktick import alerts
        shows = [at("Kasi", 1, n=i) for i in range(5)]
        for i, s in enumerate(shows):
            s["start"] = s["start"].replace(hour=10 + 3 * i)
        self.assertTrue(alerts.times_text(shows).endswith("10:00 AM, 1:00 PM, 4:00 PM +2 more"))
        shows[1]["start"] += timedelta(days=1)
        self.assertEqual(alerts.times_text(shows[:2]).count(" AM") + alerts.times_text(shows[:2]).count(" PM"), 2)
        self.assertEqual(alerts.times_text([dict(shows[0], start=None)]), "")

    def test_its_own_cinemas_arent_named_twice(self):
        self.task["cinemas"] = [{"url": "https://www.district.in/movies/ags-CD2", "name": "AGS"}]
        engine.plan([self.task], today_ist())
        self.check(at("Kasi", 1))
        self.wait()
        self.check(at("Kasi", 1), at("AGS", 2))  # AGS has its own alert, from its own page
        self.assertEqual(self.kinds(), ["opened"])


class Cards(unittest.TestCase):
    def setUp(self):
        state["tasks"] = {"w1": watcher(last_checked="10:00:00", last_checked_ts=1)}

    def tearDown(self):
        state["tasks"] = {}

    def test_card_lists_each_cinema_and_the_formats(self):
        card = views.watcher_card("w1", state["tasks"]["w1"], {"owner": "o"})
        self.assertEqual(card.count("<li"), 3)
        self.assertIn("PVR Palazzo", card)
        self.assertIn("IMAX, 4DX", card)
        self.assertIn("Not checked yet", card)

    def test_one_cinema_keeps_the_single_line(self):
        task = watcher(cinemas=[{"url": PVR, "name": "PVR Palazzo, The Nexus Vijaya Mall, Chennai"}])
        card = views.watcher_card("w1", task, {"owner": "o"})
        self.assertNotIn('class="bt-cinemas"', card)
        self.assertIn(f'href="{PVR}"', card)  # its own name and link, not "Cinema"
        self.assertIn(">PVR Palazzo, The Nexus Vijaya Mall<svg", card)  # the city cut, as everywhere

    def test_the_row_says_when_there_are_more_cinemas(self):
        row = views.watcher_row("w1", state["tasks"]["w1"], {"owner": "o", "admin": True})
        self.assertIn(">+2 cinemas</span>", row)
        self.assertIn("Also watching INOX Velachery, AGS Villivakkam", row)
        one = views.watcher_row("w1", watcher(cinemas=[{"url": PVR, "name": "PVR Palazzo"}]), {"owner": "o"})
        self.assertNotIn("bt-wrow-more", one)
        city = views.watcher_row("w1", watcher(scope="city", city="chennai", cinemas=[{"url": PVR, "name": "PVR"}]),
                                 {"owner": "o"})
        self.assertIn("Any cinema in Chennai", city)
        self.assertIn(">+1 cinema</span>", city)

    def test_cinema_names_drop_only_a_real_city(self):
        short = engine.district.short_cinema_name
        self.assertEqual(short("PVR Grand Mall, Velachery, Chennai"), "PVR Grand Mall, Velachery")
        self.assertEqual(short("PVR Aerohub, Chennai (Audi 3-All New 3D Projection)"),
                         "PVR Aerohub, Chennai (Audi 3-All New 3D Projection)")
        self.assertEqual(short("Sri Ganga Cinemas, Kolathur"), "Sri Ganga Cinemas, Kolathur")
        self.assertEqual(short("Cinepolis, Phoenix, New Delhi"), "Cinepolis, Phoenix")


class Limits(unittest.TestCase):
    def tearDown(self):
        state["tasks"] = {}

    def test_several_movies_count_as_several_watchers(self):
        state["tasks"] = {f"w{i}": watcher(id=f"w{i}") for i in range(3)}
        self.assertIsNone(access.watcher_limit_error({"owner": "o"}, None, count=2))
        self.assertIn("room for 2 more watchers", access.watcher_limit_error({"owner": "o"}, None, count=3))


@unittest.skipUnless(gradio, "Gradio isn't installed")
class NewWatchers(unittest.TestCase):
    def setUp(self):
        from booktick import handlers
        self.h = handlers
        state["tasks"] = {}
        mock.patch("booktick.handlers.save_tasks").start()
        self.request = mock.Mock(headers={"x-forwarded-for": "1.2.3.4"}, client=None)

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def add(self, cinemas, movies, formats=("IMAX",), dates=None):
        return self.h.add_new_watcher({"owner": "o"}, "Me", json.dumps(cinemas), "", json.dumps(movies), list(formats), [],
                                      dates or today_ist(), 3, 5, "topic", "", "", "", self.request)

    def test_one_watcher_per_movie_sharing_cinemas_and_formats(self):
        msg, _, _ = self.add([PVR, INOX], ["Avengers: Doomsday", "Superhero"], formats=("IMAX", "4dx"))
        tasks = sorted(state["tasks"].values(), key=lambda t: t["created_ts"])
        self.assertEqual([t["movie_filter"] for t in tasks], ["Avengers: Doomsday", "Superhero"])
        self.assertEqual([[c["url"] for c in t["cinemas"]] for t in tasks], [[PVR, INOX]] * 2)
        self.assertEqual(tasks[0]["formats"], ["IMAX", "4DX"])
        self.assertIsNot(tasks[0]["cinemas"], tasks[1]["cinemas"])  # each keeps its own cinema names
        self.assertIn("2 watchers are live", msg)

    def test_seats_released_alerts_choice(self):
        self.add([PVR], ["Avengers: Doomsday"])
        task_id, task = next(iter(state["tasks"].items()))
        self.assertEqual(task["seats_min"], 5)
        card = views.watcher_card(task_id, task, {"owner": "o"})
        self.assertIn(f'data-bt-pick="seats" data-bt-id="{task_id}"', card)
        self.assertIn('<option value="5" selected>5 or more</option>', card)
        with mock.patch("booktick.handlers.toast") as toast:
            self.h.run_watcher_action(f"seats-0:{task_id}", {"owner": "o"})
            self.assertEqual(task["seats_min"], 0)
            self.assertIn("won't send seats released alerts", toast.call_args.args[1])
            self.h.run_watcher_action(f"seats-1:{task_id}", {"owner": "o"})
            self.assertEqual(task["seats_min"], 1)
            self.assertIn("when any seat comes back", toast.call_args.args[1])
            self.h.run_watcher_action(f"seats-25:{task_id}", {"owner": "o"})  # more than the slider goes
            self.assertEqual(task["seats_min"], 1)
            self.h.run_watcher_action(f"seats-2:{task_id}", {"owner": "someone else"})
            self.assertEqual(task["seats_min"], 1)
            self.assertFalse(toast.call_args.args[0])
        self.assertIn('<option value="1" selected>Any seat</option>', views.watcher_card(task_id, task, {"owner": "o"}))
        with mock.patch("booktick.handlers.toast"):
            self.h.run_watcher_action(f"seats-8:{task_id}", {"owner": "o"})  # any number the form's slider can set
        self.assertEqual(task["seats_min"], 8)
        card = views.watcher_card(task_id, task, {"owner": "o"})
        self.assertIn('<option value="5">5 or more</option><option value="8" selected>8 or more</option>'
                      '<option value="10">10 or more</option>', card)
        self.assertTrue(card.index('value="20"') < card.index('value="0"'))  # Off stays last

    def test_anywhere_in_the_city(self):
        self.add(["city:chennai"], ["Avengers: Doomsday"])
        task = next(iter(state["tasks"].values()))
        self.assertEqual((task["scope"], task["city"], task["cinemas"]), ("city", "chennai", []))

    def test_anywhere_in_the_city_and_a_cinema(self):
        msg, _, _ = self.add(["city:chennai", PVR], ["Avengers: Doomsday"])
        task = next(iter(state["tasks"].values()))
        self.assertEqual((task["scope"], task["city"], [c["url"] for c in task["cinemas"]]), ("city", "chennai", [PVR]))
        self.assertIn("with its own alert when", msg)

    def test_too_many_cinema_days(self):
        week = f"{today_ist()}..{now_ist().date() + timedelta(days=6)}"
        msg, _, _ = self.add([PVR, INOX, AGS], ["Avengers: Doomsday"], dates=week)
        self.assertIn("21 cinema-days", msg)
        self.assertEqual(state["tasks"], {})

    def test_the_same_cinema_twice_counts_once(self):
        self.add([PVR, PVR + "?fromdate=2026-11-20"], ["Avengers: Doomsday"])
        self.assertEqual(len(next(iter(state["tasks"].values()))["cinemas"]), 1)


@unittest.skipUnless(gradio, "Gradio isn't installed")
class Editing(unittest.TestCase):
    """A watcher's Edit button loads it into the form; Save changes updates it in place."""

    def setUp(self):
        from booktick import handlers
        self.h = handlers
        state["tasks"] = {}
        mock.patch("booktick.handlers.save_tasks").start()
        self.toast = mock.patch("booktick.handlers.toast").start()
        self.request = mock.Mock(headers={"x-forwarded-for": "1.2.3.4"}, client=None)
        self.me = {"owner": "o"}
        handlers.add_new_watcher(self.me, "Me", json.dumps([PVR, INOX]), "", '["Avengers: Doomsday"]', ["IMAX"], [],
                                 today_ist(), 3, 5, "topic", "123:SECRET", "42", "https://discord.com/api/webhooks/1/abcd",
                                 self.request)
        self.id, self.task = next(iter(state["tasks"].items()))
        self.before = dict(city="chennai", cinema_value="[]", cin_url="", movie_value='["Other"]', formats_value="[]",
                           slots=[], date=today_ist(), max_alerts=5, seats_min=5, name="Typed", ntfy="typed_topic",
                           tg_token="", tg_chat="", discord="")

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def edit(self, session=None):
        out = self.h.start_edit(self.id, session or self.me, *[self.before[k] for k in self.h.FORM_KEYS])
        return out, dict(zip(self.h.FORM_KEYS[1:], out[10:]))  # after session, mode (4), result, city and its lists

    def save(self, session=None, **changes):
        _, form = self.edit(session)
        form.update(changes)
        f = lambda k: form[k]  # noqa: E731
        return self.h.save_watcher(session or self.me, f("name"), f("cinema_value"), f("cin_url"), f("movie_value"),
                                   f("formats_value"), f("slots"), f("date"), f("max_alerts"), f("seats_min"), f("ntfy"),
                                   f("tg_token"), f("tg_chat"), f("discord"), "chennai", self.request)

    def test_the_form_shows_the_watcher(self):
        out, form = self.edit()
        self.assertEqual(self.me["editing"], self.id)
        self.assertTrue(json.loads(out[1])["go"])
        self.assertEqual(out[3]["value"], "Save changes")
        self.assertTrue(out[4]["visible"])
        self.assertEqual(json.loads(form["cinema_value"]), [PVR, INOX])
        self.assertEqual(json.loads(form["movie_value"]), ["Avengers: Doomsday"])
        self.assertEqual(json.loads(form["formats_value"]), ["IMAX"])
        self.assertEqual((form["name"], form["ntfy"], form["tg_chat"]), ("Me", "topic", "42"))
        self.assertEqual(form["tg_token"], "(saved)")  # secrets never go back to the page
        self.assertEqual(form["discord"], "(saved) …abcd")
        self.assertNotIn("SECRET", json.dumps(out[1:], default=str))
        self.assertEqual(self.me["form_before_edit"]["name"], "Typed")  # what the form held, for afterwards

    def test_saving_what_it_watches_starts_it_over(self):
        self.task.update(phase="open", seat_base={"p": {}}, alert_count=2, status="Paused")
        tomorrow = str(now_ist().date() + timedelta(days=1))
        out = self.save(formats_value='["4DX"]', date=tomorrow, seats_min=2)
        self.assertIn("Saved the Avengers: Doomsday watcher", out[0])
        self.assertIn("formats, dates, seats released alerts", out[0])
        self.assertEqual((self.task["formats"], self.task["target_date"], self.task["seats_min"]), (["4DX"], tomorrow, 2))
        self.assertEqual(self.task["phase"], "waiting")
        self.assertNotIn("seat_base", self.task)
        self.assertEqual((self.task["alert_count"], self.task["status"]), (2, "Paused"))  # history and pause stay
        self.assertEqual(self.task["telegram_token"], "123:SECRET")  # "(saved)" kept it
        self.assertEqual(self.task["discord_webhook"], "https://discord.com/api/webhooks/1/abcd")
        self.assertNotIn("editing", self.me)
        form = dict(zip(self.h.FORM_KEYS[1:], out[12:]))
        self.assertEqual((form["name"], form["ntfy"]), ("Typed", "typed_topic"))  # the form is as it was

    def test_a_new_name_or_channel_doesnt_start_it_over(self):
        self.task.update(phase="open", seat_base={"p": {}})
        out = self.save(name="Arun", tg_token="", tg_chat="")
        self.assertIn("Changed: alert channels, name.", out[0])
        self.assertEqual((self.task["phase"], self.task["user_label"]), ("open", "Arun"))
        self.assertEqual((self.task["telegram_token"], self.task["telegram_chat_id"]), ("", ""))  # cleared: removed

    def test_nothing_changed(self):
        self.assertIn("Nothing to save", self.save()[0])

    def test_where_it_looks(self):
        self.save(cinema_value=json.dumps(["city:chennai", AGS]))
        self.assertEqual((self.task["scope"], self.task["city"]), ("city", "chennai"))
        self.assertEqual([c["url"] for c in self.task["cinemas"]], [AGS])
        self.assertIn("Which movie?", self.save(cinema_value='["city:chennai"]', movie_value="[]")[0])
        self.assertIn("One movie per watcher", self.save(movie_value='["A", "B"]')[0])
        self.assertEqual(self.me["editing"], self.id)  # still editing after a mistake

    def test_a_booked_watcher_watches_again(self):
        self.task.update(status="Done", booked=True)
        self.save(name="Me again")
        self.assertEqual(self.task["status"], "Done")  # just a new name
        self.save(date=str(now_ist().date() + timedelta(days=2)))
        self.assertEqual((self.task["status"], self.task["booked"]), ("Active", False))

    def test_only_its_owner_or_an_admin(self):
        stranger = {"owner": "x"}
        out, _ = self.edit(stranger)
        self.assertNotIn("editing", stranger)
        self.assertFalse(self.toast.call_args.args[0])
        stranger["editing"] = self.id  # even if it tried
        self.assertIn("That watcher is gone", self.save(stranger)[0])
        admin = {"owner": "x", "admin": True}
        out, _ = self.edit(admin)
        self.assertIn("(Me&#x27;s)", out[2]["value"])
        self.save(admin, name="Renamed")
        self.assertEqual((self.task["user_label"], self.task["owner"]), ("Renamed", "o"))

    def test_cancel_puts_the_form_back(self):
        self.edit()
        out = self.h.cancel_edit(self.me, "chennai")
        self.assertNotIn("editing", self.me)
        self.assertEqual(out[3]["value"], "Start watching")
        form = dict(zip(self.h.FORM_KEYS[1:], out[10:]))
        self.assertEqual((form["name"], form["movie_value"], form["tg_token"]), ("Typed", '["Other"]', ""))

    def test_a_test_alert_while_editing_uses_its_saved_token(self):
        self.edit()
        with mock.patch("booktick.handlers.send_task_notification", return_value=[("Telegram", True, "sent")]) as send, \
                mock.patch("booktick.handlers.access.claim_test", return_value=None):
            self.h.test_user_notification(self.me, "Me", json.dumps([PVR]), "", "topic", "(saved)", "42", "")
        self.assertEqual(send.call_args.args[0]["telegram_token"], "123:SECRET")

    def test_a_cinema_watchers_city(self):
        from booktick import district
        self.assertEqual(district.city_of_cinema(PVR), "chennai")
        cities = {"value": ["chennai", "mumbai", "navi-mumbai"], "age": 0}
        with mock.patch("booktick.district.peek", return_value=cities):
            self.assertEqual(district.city_of_cinema("https://www.district.in/movies/pvr-navi-mumbai-CD1"), "navi-mumbai")
        self.assertIsNone(district.city_of_cinema("https://www.district.in/movies/somewhere-CD1"))


if __name__ == "__main__":
    unittest.main()
