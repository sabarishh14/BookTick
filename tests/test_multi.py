"""One movie per watcher, at up to a few cinemas, in any of a few formats. No network."""
import json
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
        self.assertEqual(self.send.call_args.args[2], "Cinemas in Chennai")
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
        self.task["last_spread_ts"] -= engine.CITY_SPREAD_GAP  # as if a few minutes went by

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
        self.assertEqual(self.send.call_args.args[2], "AGS and 1 more cinema")
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
        task = watcher(cinemas=[{"url": PVR, "name": "PVR Palazzo"}])
        self.assertNotIn('class="bt-cinemas"', views.watcher_card("w1", task, {"owner": "o"}))


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
                                      dates or today_ist(), 3, "topic", "", "", "", self.request)

    def test_one_watcher_per_movie_sharing_cinemas_and_formats(self):
        msg, _, _ = self.add([PVR, INOX], ["Avengers: Doomsday", "Superhero"], formats=("IMAX", "4dx"))
        tasks = sorted(state["tasks"].values(), key=lambda t: t["created_ts"])
        self.assertEqual([t["movie_filter"] for t in tasks], ["Avengers: Doomsday", "Superhero"])
        self.assertEqual([[c["url"] for c in t["cinemas"]] for t in tasks], [[PVR, INOX]] * 2)
        self.assertEqual(tasks[0]["formats"], ["IMAX", "4DX"])
        self.assertIsNot(tasks[0]["cinemas"], tasks[1]["cinemas"])  # each keeps its own cinema names
        self.assertIn("2 watchers are live", msg)

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


if __name__ == "__main__":
    unittest.main()
