"""Seat trackers: recording seat counts, reading which seats changed, and the report. No network."""
import copy
import io
import json
import os
import time
import unittest
from datetime import timedelta
from unittest import mock

from booktick import access, alerts, engine, storage, tracker, views
from booktick.config import BLOCK_MIN, MAX_TRACKED_SHOWS, RELEASE_MIN, REPORT_AFTER
from booktick.core import now_ist, state, today_ist

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
with io.open(os.path.join(FIXTURES, "seat_layout.json"), encoding="utf-8") as f:
    LAYOUT = json.load(f)


def show(minutes_from_now, n=1, avail=100, fmt="IMAX 2D", title="Ramayana"):
    start = (now_ist() + timedelta(minutes=minutes_from_now)).replace(second=0, microsecond=0)
    return {"title": title, "cinema": "PVR", "format": fmt, "start": start, "audi": f"A{n}", "avail": avail,
            "total": 442, "price": 300.0, "url": f"https://www.district.in/movies/seat-layout/x?encsessionid=1-2-3-{n}"}


def make_tracker(**extra):
    task = {"id": "t1", "kind": "tracker", "owner": "o", "user_label": "Me <b>", "status": "Active", "scope": "cinema",
            "cinema_url": "https://www.district.in/movies/pvr-CD1", "cinema_name": "PVR", "target_date": today_ist(),
            "target_date_to": today_ist(), "movie_filter": "", "format_filter": "IMAX", "time_slots": [],
            "ntfy_topic": "topic", "report_token": "r" * 24, "created_ts": time.time()}
    task.update(extra)
    return task


class Base(unittest.TestCase):
    def setUp(self):
        self.task = make_tracker()
        state["tasks"] = {"t1": self.task}
        mock.patch("booktick.tracker.save_tasks").start()
        mock.patch("booktick.tracker.seatmap.enabled", return_value=False).start()
        tracker._pending.clear()
        tracker._gate["next"] = 0.0

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def at(self, offset, shows):
        """observe() as if it ran `offset` seconds from now."""
        with mock.patch("booktick.tracker.time.time", return_value=time.time() + offset):
            tracker.observe(self.task, "PVR", shows)


class Recording(Base):
    def test_counts_are_kept_only_when_they_change(self):
        s = show(120)
        self.at(0, [s])
        self.at(15, [s])
        self.at(30, [dict(s, avail=95)])
        rec = self.task["track"]["shows"][tracker.show_key(s)]
        self.assertEqual([c[1] for c in rec["counts"]], [100, 95])
        self.assertTrue(rec["dirty"])  # its seats get read again, to see which changed

    def test_a_release_is_counted_and_logged(self):
        s = show(120)
        self.at(0, [dict(s, avail=10)])
        with mock.patch("booktick.tracker.add_log") as log:
            self.at(15, [dict(s, avail=10 + RELEASE_MIN + 4)])
        track = self.task["track"]
        self.assertEqual((track["releases"], track["released"]), (1, RELEASE_MIN + 4))
        self.assertIn(f"{RELEASE_MIN + 4} seats came back", log.call_args.args[0])
        self.assertIn("1 release", self.task["last_result"])

    def test_only_upcoming_shows_and_at_most_the_cap(self):
        shows = [show(-30, n=0)] + [show(60 + i, n=i + 1) for i in range(MAX_TRACKED_SHOWS + 2)]
        self.at(0, shows)
        track = self.task["track"]
        self.assertEqual(len(track["shows"]), MAX_TRACKED_SHOWS)
        self.assertNotIn(tracker.show_key(shows[0]), track["shows"])  # already started
        self.assertEqual(len(track["skipped"]), 2)

    def test_a_show_that_disappears_was_taken_off_sale(self):
        a, b = show(60, n=1), show(90, n=2)
        self.at(0, [a, b])
        self.at(15, [a])
        self.assertTrue(self.task["track"]["shows"][tracker.show_key(b)]["gone"])

    def test_history_survives_a_restart(self):
        s = show(60)
        self.at(0, [s])
        saved = storage.stored_form(state["tasks"])["t1"]
        self.assertNotIn("track", saved)          # the live copy isn't stored…
        self.assertIn(tracker.show_key(s), saved["track_saved"]["shows"])  # …the periodic one is
        state["tasks"]["t1"] = self.task = dict(saved)
        self.assertEqual(len(tracker.track_of(self.task)["shows"]), 1)


class Seats(Base):
    def seats_with(self, free=(), taken=()):
        layout = copy.deepcopy(LAYOUT)
        for area in layout["seatLayout"]["colAreas"]["objArea"]:
            for row in area["objRow"]:
                for seat in row["objSeat"]:
                    sid = f"{row['PhyRowId']}{seat.get('displaySeatNumber')}"
                    if sid in free:
                        seat["SeatStatus"] = "0"
                    if sid in taken:
                        seat["SeatStatus"] = "1"
        return layout

    def test_which_seats_changed(self):
        s = show(120)
        self.at(0, [s])
        key = tracker.show_key(s)
        row_a = [sid for _, _, sid, free in tracker.seat_list(tracker.seatmap.seat_rows(LAYOUT)) if sid.startswith("A")]
        with mock.patch("booktick.tracker.time.time", return_value=time.time()):
            tracker.took_map("t1", key, LAYOUT, None)                       # first read: the starting point
        rec = self.task["track"]["shows"][key]
        self.assertTrue(all(rec["seats"][sid] == 0 for sid in row_a))     # row A was taken from the start
        with mock.patch("booktick.tracker.time.time", return_value=time.time() + 600):
            tracker.took_map("t1", key, self.seats_with(free=row_a[:8]), None)
        diff = rec["diffs"][-1]
        self.assertEqual(sorted(diff["freed"]), sorted(row_a[:8]))
        self.assertEqual(diff["held"], [])  # taken since tracking began: released, not an unfinished booking
        geo = self.task["track"]["layouts"][rec["layout"]]
        self.assertEqual(tracker.zone_of(row_a[:8], geo)[0], "row A")

    def test_seats_back_within_minutes_were_unfinished_bookings(self):
        s = show(120)
        self.at(0, [s])
        key = tracker.show_key(s)
        free_now = [sid for _, _, sid, free in tracker.seat_list(tracker.seatmap.seat_rows(LAYOUT)) if free][:3]
        t0 = time.time()
        with mock.patch("booktick.tracker.time.time", return_value=t0):
            tracker.took_map("t1", key, LAYOUT, None)
        with mock.patch("booktick.tracker.time.time", return_value=t0 + 300):
            tracker.took_map("t1", key, self.seats_with(taken=free_now), None)  # someone grabs 3 seats
        with mock.patch("booktick.tracker.time.time", return_value=t0 + 900):
            tracker.took_map("t1", key, LAYOUT, None)                         # …and lets them go
        last = self.task["track"]["shows"][key]["diffs"][-1]
        self.assertEqual(sorted(last["held"]), sorted(free_now))

    def test_a_failed_read_is_noted_once_and_retried_later(self):
        s = show(120)
        self.at(0, [s])
        key = tracker.show_key(s)
        with mock.patch("booktick.tracker.add_log") as log:
            tracker.took_map("t1", key, None, RuntimeError("timeout"))
            tracker.took_map("t1", key, None, RuntimeError("timeout"))
        self.assertEqual(log.call_count, 1)
        self.assertGreater(self.task["track"]["shows"][key]["map_ts"], 0)

    def test_reads_are_spaced_out_and_soonest_show_first(self):
        tracker.seatmap.enabled.return_value = True
        later, sooner = show(300, n=1), show(60, n=2)
        with mock.patch("booktick.tracker.seatmap.request_layout", return_value=True) as req, \
                mock.patch("booktick.tracker.seatmap.waiting", return_value=0):
            self.at(0, [later, sooner])
            self.at(15, [later, sooner])  # too soon for another read
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.args[0], sooner["url"])


class Report(Base):
    def track_with(self, counts, start_in=3600, **rec):
        start = now_ist() + timedelta(seconds=start_in)
        base = {"title": "Ramayana", "format": "IMAX 2D", "audi": "1", "start": start.isoformat(), "total": 300,
                "url": "u", "counts": counts, "dirty": False, "map_ts": 0, "seats": None, "diffs": [], "gone": False}
        base.update(rec)
        self.task["track"] = dict(tracker.new_track(), shows={"k": base})
        return start.timestamp()

    def test_releases_timing_and_advice(self):
        now = int(time.time())
        start = self.track_with([[now - 1000, 10], [now - 985, 50], [now - 600, 48], [now - 585, 60]], start_in=2400)
        report = tracker.build_report(self.task)
        s = report["shows"][0]
        self.assertEqual([r["seats"] for r in s["releases"]], [40, 12])
        self.assertAlmostEqual(s["releases"][0]["before"], start - (now - 985), delta=1)
        text = " ".join(report["insights"])
        self.assertIn("2 releases across 1 show: 52 seats came back in one go", text)
        self.assertIn("All of them came back 30–60 min before showtime", text)
        self.assertIn("Next time, look 30–60 min before showtime", text)

    def test_seats_flipping_back_and_forth_come_first(self):
        now = int(time.time())
        self.track_with([[now - 100, 43], [now - 85, 64], [now - 70, 43], [now - 55, 64], [now - 40, 43]])
        report = tracker.build_report(self.task)
        flip = report["shows"][0]["flip"]
        self.assertEqual((flip["swings"], flip["size"], flip["low"], flip["high"]), (4, 21, 43, 64))
        self.assertTrue(report["insights"][0].startswith("Seats kept flipping in the "))
        self.assertIn("about 21 seats were blocked and released 4 times", report["insights"][0])
        self.assertIn("Seats kept flipping", tracker.report_page(self.task, report))

    def test_one_way_moves_are_not_flipping(self):
        now = int(time.time())
        self.track_with([[now - 100, 10], [now - 85, 40], [now - 70, 70]])
        self.assertIsNone(tracker.build_report(self.task)["shows"][0]["flip"])

    def test_one_release_is_reported_without_a_pattern(self):
        now = int(time.time())
        self.track_with([[now - 1000, 10], [now - 985, 50]], start_in=2400)
        lines = tracker.build_report(self.task)["insights"]
        self.assertTrue(lines[0].startswith("1 release: 40 seats in the "))
        self.assertNotIn("Next time", " ".join(lines))

    def test_a_change_across_a_gap_in_checking_is_not_pinned_to_a_moment(self):
        now = int(time.time())
        self.track_with([[now - 4000, 10], [now - 400, 50]])  # the Space was asleep in between
        report = tracker.build_report(self.task)
        self.assertIn("between", report["insights"][0])
        self.assertIn("between", tracker.report_page(self.task, report))

    def test_big_drops_and_small_returns(self):
        now = int(time.time())
        self.track_with([[now - 900, 100], [now - 800, 100 - BLOCK_MIN - 5], [now - 700, 100 - BLOCK_MIN - 3]])
        s = tracker.build_report(self.task)["shows"][0]
        self.assertEqual(s["drops"][0]["seats"], BLOCK_MIN + 5)
        self.assertEqual(s["small_back"], 2)
        self.assertIn("big drop", " ".join(tracker.insights([s])))

    def test_nothing_unusual(self):
        now = int(time.time())
        self.track_with([[now - 900, 100], [now - 800, 98]])
        self.assertIn("didn't seem to hold seats back", tracker.build_report(self.task)["insights"][0])

    def test_no_shows(self):
        self.assertIn("No matching shows", tracker.insights([])[0])


class Finishing(Base):
    def test_due_after_the_last_show_starts(self):
        s = show(30)
        self.at(0, [s])
        self.assertFalse(tracker.due(self.task))
        later = s["start"].timestamp() + REPORT_AFTER + 1
        self.assertTrue(tracker.due(self.task, later))

    def test_due_once_the_day_has_passed_even_without_shows(self):
        self.task["target_date"] = str(now_ist().date() - timedelta(days=1))
        self.assertTrue(tracker.due(self.task))

    def test_finish_stores_and_sends_the_report(self):
        s = show(30)
        self.at(0, [s])
        after = s["start"].timestamp() + REPORT_AFTER + 1
        with mock.patch("booktick.tracker.send_report", return_value=[("ntfy", True, "sent")]) as send, \
                mock.patch("booktick.tracker.PUBLIC_URL", "https://bt.example"), \
                mock.patch("booktick.tracker.time.time", return_value=after):
            tracker.finish("t1")
            tracker.finish("t1")  # a second call does nothing
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[3], "https://bt.example/report/" + "r" * 24)
        self.assertEqual(self.task["status"], "Done")
        self.assertNotIn("track", self.task)
        self.assertTrue(self.task["report"]["final"])

    def test_report_page(self):
        task, report = tracker.report_for("r" * 24)
        page = tracker.report_page(task, report)
        self.assertIn("IMAX shows · PVR", page)
        self.assertIn("So far", page)
        self.assertEqual(tracker.report_for("wrong-token-xxxxxxxx"), (None, None))
        self.assertIn("Report not found", tracker.report_page(None, None))

    def test_housekeeping_sends_due_reports(self):
        self.task["target_date"] = self.task["target_date_to"] = str(now_ist().date() - timedelta(days=1))
        with mock.patch.object(engine, "_pool") as pool, mock.patch("booktick.engine.save_tasks"):
            engine.housekeeping(today_ist())
        pool.submit.assert_called_once_with(tracker.finish, "t1")
        self.assertEqual(self.task["status"], "Active")  # not "Expired" like a watcher


class Engine(Base):
    def test_the_engine_hands_tracker_pages_to_the_tracker(self):
        shows = [show(60, fmt="IMAX 2D"), show(90, n=2, fmt="2D")]
        with mock.patch("booktick.engine.district.fetch_shows", return_value=("PVR", shows)), \
                mock.patch("booktick.engine.tracker.observe") as observe, \
                mock.patch("booktick.engine.check_task") as check:
            engine.run_page("p", {"kind": "cinema", "date": today_ist(), "tasks": [self.task]})
        check.assert_not_called()
        self.assertEqual([s["format"] for s in observe.call_args.args[2]], ["IMAX 2D"])  # only matching shows


class Separation(Base):
    def test_trackers_are_not_watchers(self):
        state["tasks"]["w1"] = {"id": "w1", "owner": "o", "status": "Active", "target_date": today_ist()}
        session = {"owner": "o"}
        self.assertEqual([tid for tid, _ in access.visible_tasks(session)], ["w1"])
        self.assertEqual([tid for tid, _ in access.visible_tasks(session, kind="tracker")], ["t1"])
        self.assertIsNone(access.watcher_limit_error(session, None))
        html = views.trackers_html(session)
        self.assertIn("Report so far", html)
        self.assertIn("Me &lt;b&gt;", html)
        self.assertNotIn("Report so far", views.watchers_html(session))

    def test_tracker_limit(self):
        state["tasks"]["t2"] = make_tracker(id="t2")
        self.assertIn("seat trackers going at once", access.tracker_limit_error({"owner": "o"}))
        self.assertIsNone(access.tracker_limit_error({"owner": "o", "admin": True}))
        self.assertIsNone(access.tracker_limit_error({"owner": "someone-else"}))


class Sending(unittest.TestCase):
    def test_every_channel_gets_the_findings_and_the_link(self):
        task = {"ntfy_topic": "t", "telegram_token": "x", "telegram_chat_id": "1", "discord_webhook": "https://d.example/w"}
        res = mock.Mock(ok=True, status_code=200)
        with mock.patch.object(alerts.requests, "post", return_value=res) as post:
            results = alerts.send_report(task, "📊 Seat report", ["40 seats came back"], "https://bt.example/report/abc")
        ntfy, telegram, discord = post.call_args_list
        self.assertEqual(ntfy.kwargs["json"]["click"], "https://bt.example/report/abc")
        self.assertIn("40 seats came back", ntfy.kwargs["json"]["message"])
        self.assertIn('href="https://bt.example/report/abc"', telegram.kwargs["json"]["text"])
        self.assertEqual(discord.kwargs["json"]["embeds"][0]["url"], "https://bt.example/report/abc")
        self.assertTrue(all(ok for _, ok, _ in results))


if __name__ == "__main__":
    unittest.main()
