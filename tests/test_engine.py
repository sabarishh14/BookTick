"""Matching, scheduling, back-off, expiry and the alert rules. Nothing here touches the network."""
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from booktick import district, engine
from booktick.core import IST, now_ist, state, today_ist


def show(hour, minute=0, title="Ramayana", fmt="IMAX 2D", avail=10, date=None):
    d = datetime.strptime(date or today_ist(), "%Y-%m-%d").replace(hour=hour, minute=minute, tzinfo=IST)
    return {"title": title, "cinema": "PVR", "format": fmt, "start": d, "audi": "", "avail": avail,
            "total": 100, "price": 200.0, "url": "https://www.district.in/x"}


def day(n):
    return str(now_ist().date() + timedelta(days=n))


class Matching(unittest.TestCase):
    def test_keywords_and_format(self):
        self.assertTrue(engine.show_matches(show(10), ["rama"], "IMAX"))
        self.assertFalse(engine.show_matches(show(10), ["jailer"], "IMAX"))
        self.assertFalse(engine.show_matches(show(10, fmt="2D"), ["rama"], "IMAX"))
        self.assertTrue(engine.show_matches(show(10, fmt="2D"), [], "ANY"))

    def test_time_slots(self):
        self.assertTrue(engine.in_time_slots(show(9), ["morning"]))
        self.assertTrue(engine.in_time_slots(show(18, 30), ["evening"]))
        self.assertFalse(engine.in_time_slots(show(18, 30), ["morning", "night"]))
        self.assertTrue(engine.in_time_slots(show(1), ["night"]))  # a 1 AM show counts as night
        self.assertTrue(engine.in_time_slots(show(1), []))         # nothing ticked = any time

    def test_seats(self):
        self.assertTrue(engine.seats_open(show(10, avail=None)))
        self.assertFalse(engine.seats_open(show(10, avail=0)))


class Planning(unittest.TestCase):
    def setUp(self):
        district._cache.pop("city:testcity", None)

    def test_cinema_watcher_gets_one_page_per_future_date(self):
        task = {"scope": "cinema", "cinema_url": "https://www.district.in/movies/x-CD1",
                "target_date": day(-1), "target_date_to": day(1)}
        jobs = engine.page_jobs(task, today_ist())
        self.assertEqual([j[2] for j in jobs], [day(0), day(1)])  # yesterday is skipped
        self.assertTrue(all(j[0] == "cinema" and "fromdate=" in j[1] for j in jobs))

    def test_city_watcher_reads_the_list_until_its_movie_appears(self):
        task = {"scope": "city", "city": "testcity", "movie_filter": "Ramayana", "target_date": day(0)}
        self.assertEqual(engine.page_jobs(task, today_ist()), [("list", "list:testcity", None)])
        district._cache["city:testcity"] = {"value": {"cinemas": [], "movies": [
            {"id": "1", "name": "Ramayana", "url": "https://www.district.in/movies/ramayana-movie-tickets-MV1"}]},
            "expires": time.time() + 600, "loaded": time.time()}
        jobs = engine.page_jobs(task, today_ist())
        self.assertEqual(jobs, [("city", f"https://www.district.in/movies/ramayana-movie-tickets-in-testcity-MV1?fromdate={day(0)}", day(0))])

    def test_watchers_on_the_same_page_share_it(self):
        url = "https://www.district.in/movies/x-CD1"
        a = {"scope": "cinema", "cinema_url": url, "target_date": day(0)}
        b = dict(a)
        pages = engine.plan([a, b], today_ist())
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(next(iter(pages.values()))["tasks"]), 2)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "w_test", "owner": "o", "user_label": "T", "status": "Active", "scope": "cinema",
                     "cinema_url": "https://www.district.in/movies/x-CD1", "movie_filter": "Ramayana",
                     "format_filter": "IMAX", "time_slots": [], "target_date": today_ist(), "max_alerts": 2, "alert_count": 0}
        state["tasks"] = {"w_test": self.task}
        self.send = mock.patch("booktick.engine.send_task_notification", return_value=[("ntfy", True, "sent")]).start()
        mock.patch("booktick.engine.save_tasks").start()

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def check(self, shows, page="page", next_round=True):
        if next_round:
            self.task["last_alert_ts"] = 0
        engine.check_task(self.task, page, today_ist(), "PVR", shows)

    def kinds(self):
        return [call.kwargs["kind"] for call in self.send.call_args_list]

    def test_no_alert_without_a_match(self):
        self.check([show(10, title="Jailer 2")])
        self.send.assert_not_called()
        self.assertIn("No match", self.task["last_result"])

    def test_sold_out_shows_dont_alert(self):
        self.check([show(10, avail=0)])
        self.send.assert_not_called()

    def test_opening_alert_then_reminders_then_quiet(self):
        self.check([show(10)])
        self.check([show(10)], next_round=False)  # same round: no second alert
        self.check([show(10)])
        self.check([show(10)])  # max_alerts is 2, so no more reminders
        self.assertEqual(self.kinds(), ["opened", "reminder"])
        self.assertEqual(self.task["status"], "Active")  # still watching, not finished
        self.assertTrue(self.task["action_token"])

    def test_closed_then_reopened(self):
        self.check([show(10)])
        self.check([show(10, avail=0)])     # sold out
        self.check([])                       # then taken off sale: already closed, no new alert
        self.check([show(10, avail=5)])      # back
        self.assertEqual(self.kinds(), ["opened", "closed", "reopened"])
        self.assertEqual(self.send.call_args_list[1].kwargs["reason"], "sold_out")
        self.assertEqual(self.task["phase"], "open")
        self.assertEqual(self.task["reminders_left"], 1)  # reminders start over

    def test_taken_off_sale(self):
        self.check([show(10)])
        self.check([])
        self.assertEqual(self.send.call_args_list[-1].kwargs["reason"], "pulled")

    def test_more_seats_after_the_reminders(self):
        self.task["max_alerts"] = 1
        self.check([show(10, avail=2)])
        self.check([show(10, avail=5)])   # only 3 more: not enough
        self.check([show(10, avail=40)])  # 35 more
        self.check([show(10, avail=40), show(18, title="Ramayana")])  # a new show, but too soon after the last
        self.assertEqual(self.kinds(), ["opened", "more_seats"])

    def test_closed_only_once_every_date_has_reported(self):
        self.task["expected_pages"] = ["day1", "day2"]
        self.check([show(10)], page="day1")
        self.check([], page="day1")  # day 2 hasn't been checked yet: can't say it's closed
        self.assertEqual(self.kinds(), ["opened"])
        self.check([], page="day2")
        self.assertEqual(self.kinds(), ["opened", "closed"])

    def test_paused_watchers_are_skipped(self):
        self.task["status"] = "Paused"
        self.check([show(10)])
        self.send.assert_not_called()


class BackoffAndExpiry(unittest.TestCase):
    def setUp(self):
        mock.patch("booktick.engine.save_tasks").start()
        engine._pages.clear()
        engine._block.update(until=0.0, strikes=0)

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}
        engine._block.update(until=0.0, strikes=0)

    def test_failing_pages_wait_longer_each_time(self):
        job = {"kind": "cinema", "tasks": []}
        waits = []
        for _ in range(3):
            engine.page_failed("p", job, ValueError("boom"))
            waits.append(engine._pages["p"]["next"] - time.time())
        self.assertLess(waits[0], waits[1])
        self.assertLess(waits[1], waits[2])
        self.assertEqual(engine._block["until"], 0.0)  # an ordinary error doesn't pause everyone

    def test_being_turned_away_pauses_everyone(self):
        err = Exception("429")
        err.response = mock.Mock(status_code=429)
        engine.page_failed("p", {"kind": "cinema", "tasks": []}, err)
        self.assertGreater(engine._block["until"], time.time() + 30)

    def test_success_clears_the_failures(self):
        engine.page_failed("p", {"kind": "cinema", "tasks": []}, ValueError("boom"))
        engine.page_succeeded("p", "cinema", {"kind": "cinema", "tasks": []})
        self.assertEqual(engine._pages["p"]["fails"], 0)

    def test_past_watchers_stop_then_disappear(self):
        state["tasks"] = {
            "old": {"id": "old", "status": "Active", "target_date": day(-2)},
            "ancient": {"id": "ancient", "status": "Done", "target_date": day(-30)},
            "live": {"id": "live", "status": "Active", "target_date": day(-1), "target_date_to": day(1)},
        }
        engine.housekeeping(today_ist())
        self.assertEqual(state["tasks"]["old"]["status"], "Expired")
        self.assertNotIn("ancient", state["tasks"])
        self.assertEqual(state["tasks"]["live"]["status"], "Active")  # its range isn't over yet


if __name__ == "__main__":
    unittest.main()
