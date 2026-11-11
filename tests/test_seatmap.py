"""Seat map snapshots, using a saved copy of District's seat data (no browser needed)."""
import io
import json
import os
import time
import unittest
from datetime import datetime
from unittest import mock

from booktick import alerts, engine, seatmap
from booktick.core import IST, state, today_ist

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
with io.open(os.path.join(FIXTURES, "seat_layout.json"), encoding="utf-8") as f:
    LAYOUT = json.load(f)
SHOW = {"title": "Ramayana", "cinema": "PVR Palazzo", "cinema_url": "https://www.district.in/movies/pvr-palazzo-CD1022274",
        "format": "IMAX 3D", "start": datetime(2026, 11, 12, 10, 15, tzinfo=IST), "audi": "", "avail": 100, "total": 442,
        "price": 491.0, "url": "https://www.district.in/movies/seat-layout/eu8~turj0f?encsessionid=1-2-3-eu8~turj0f"}


class FakeBrowser:
    def __init__(self):
        self.opened = 0

    def layout(self, url):
        self.opened += 1
        return LAYOUT

    def png(self, page_html):
        self.html = page_html
        return b"\x89PNG fake"


class Reading(unittest.TestCase):
    def test_rows_and_counts(self):
        sections = seatmap.seat_rows(LAYOUT)
        rows = [row for _, rs in sections for row in rs]
        self.assertEqual([r[0] for r in rows][:3], ["A", "B", "C"])
        free, total = seatmap.counts(sections)
        self.assertEqual(total, 442)
        self.assertEqual(free, sum(1 for _, seats in rows for s in seats if s[1]))
        self.assertTrue(all(not s[1] for s in rows[0][1]))  # row A was fully taken in this capture

    def test_drawing(self):
        page = seatmap.render_html(seatmap.seat_rows(LAYOUT), "Ramayana <IMAX>", "PVR Palazzo")
        self.assertIn("SCREEN THIS WAY", page)
        self.assertIn("Ramayana &lt;IMAX&gt;", page)  # escaped
        self.assertLess(page.index('class="row"'), page.index("SCREEN THIS WAY"))  # screen below the rows


class Snapshots(unittest.TestCase):
    def setUp(self):
        seatmap._images.clear()

    def test_snapshot_is_stored_and_shared_for_a_while(self):
        browser = FakeBrowser()
        first = seatmap.snapshot(browser, SHOW, "PVR Palazzo")
        again = seatmap.snapshot(browser, SHOW, "PVR Palazzo")  # another watcher, same show
        self.assertEqual(first, again)
        self.assertEqual(browser.opened, 1)
        hit = seatmap.image(first)
        self.assertEqual(hit["total"], 442)
        self.assertIn("Thu 12 Nov, 10:15 AM", browser.html)

    def test_images_expire(self):
        image_id = seatmap.store_image(b"x", SHOW, 1, 2)
        seatmap._images[image_id]["expires"] = time.time() - 1
        self.assertIsNone(seatmap.image(image_id))

    def test_links_need_a_public_address(self):
        with mock.patch.object(seatmap, "PUBLIC_URL", ""):
            self.assertIsNone(seatmap.image_url("abc"))
        with mock.patch.object(seatmap, "PUBLIC_URL", "https://booktick.example"):
            self.assertEqual(seatmap.image_url("abc"), "https://booktick.example/seatmap/abc.png")


class Engine(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "w", "owner": "o", "user_label": "T", "status": "Active", "scope": "cinema",
                     "cinema_url": SHOW["cinema_url"], "movie_filter": "Ramayana", "format_filter": "IMAX",
                     "time_slots": [], "target_date": today_ist(), "max_alerts": 1}
        state["tasks"] = {"w": self.task}
        mock.patch("booktick.engine.save_tasks").start()
        mock.patch("booktick.engine.send_task_notification", return_value=[]).start()
        self.request = mock.patch("booktick.engine.seatmap.request").start()

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def show_at(self, hour, n):
        start = datetime.strptime(f"{today_ist()} {hour:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        return dict(SHOW, start=start, url=f"{SHOW['url']}-{n}")

    def test_every_show_gets_a_seat_map_when_bookings_open(self):
        shows = [self.show_at(10, 1), self.show_at(14, 2), self.show_at(18, 3)]
        engine.check_task(self.task, "p", today_ist(), "PVR", shows)      # opened
        self.task["last_alert_ts"] = 0
        engine.check_task(self.task, "p", today_ist(), "PVR", shows)      # nothing new: no more maps
        self.assertEqual([c.args[1]["url"] for c in self.request.call_args_list], [s["url"] for s in shows])

    def test_seat_maps_are_capped(self):
        shows = [self.show_at(6 + i, i) for i in range(engine.MAX_SEATMAPS + 3)]
        engine.check_task(self.task, "p", today_ist(), "PVR", shows)
        self.assertEqual(self.request.call_count, engine.MAX_SEATMAPS)
        self.assertEqual(self.request.call_args_list[0].args[1]["url"], shows[0]["url"])  # earliest first


class FollowUp(unittest.TestCase):
    def test_every_channel_gets_the_image(self):
        task = {"ntfy_topic": "t", "telegram_token": "x", "telegram_chat_id": "1", "discord_webhook": "https://d.example/w"}
        res = mock.Mock(ok=True, status_code=200)
        with mock.patch.object(alerts.requests, "post", return_value=res) as post:
            alerts.send_seatmap(task, SHOW, "PVR Palazzo", b"PNG", 100, 442, "https://booktick.example/seatmap/a.png")
        ntfy, telegram, discord = post.call_args_list
        self.assertEqual(ntfy.kwargs["json"]["attach"], "https://booktick.example/seatmap/a.png")
        self.assertIn("100 of 442 seats free", ntfy.kwargs["json"]["message"])
        self.assertNotIn("click", ntfy.kwargs["json"])  # only for looking at: tapping opens nothing
        self.assertNotIn("<a ", telegram.kwargs["data"]["caption"])
        self.assertNotIn('"url"', discord.kwargs["data"]["payload_json"].replace('"url": "attachment', ""))
        self.assertTrue(telegram.args[0].endswith("/sendPhoto"))
        self.assertEqual(telegram.kwargs["files"]["photo"][1], b"PNG")
        self.assertIn("attachment://seatmap.png", discord.kwargs["data"]["payload_json"])

    def test_ntfy_needs_a_public_link(self):
        res = mock.Mock(ok=True, status_code=200)
        with mock.patch.object(alerts.requests, "post", return_value=res) as post:
            alerts.send_seatmap({"ntfy_topic": "t"}, SHOW, "PVR", b"PNG", 1, 2, None)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
