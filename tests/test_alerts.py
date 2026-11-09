"""What alerts link to. Nothing is actually sent."""
import unittest
from datetime import datetime
from unittest import mock

from booktick import alerts
from booktick.core import IST

SEATS = "https://www.district.in/movies/seat-layout/abc?encsessionid=1-2-3-abc"
CINEMA = "https://www.district.in/movies/pvr-chennai-CD1"


def show(cinema_url=CINEMA):
    return {"title": "Ramayana", "cinema": "PVR", "cinema_url": cinema_url, "format": "IMAX",
            "start": datetime(2026, 11, 10, 18, 30, tzinfo=IST), "audi": "", "avail": 5, "total": 10,
            "price": 200.0, "url": SEATS}


class Links(unittest.TestCase):
    def test_cinema_page_comes_first_on_the_shows_date(self):
        self.assertEqual(alerts.alert_links(show()), [("Open cinema", f"{CINEMA}?fromdate=2026-11-10"),
                                                      ("Seat map (web)", SEATS)])

    def test_without_a_cinema_page_only_the_seat_link_is_left(self):
        self.assertEqual(alerts.alert_links(show(cinema_url=None)), [("Open on District", SEATS)])

    def test_tapping_opens_the_theatre_page(self):
        task = {"ntfy_topic": "t", "telegram_token": "x", "telegram_chat_id": "1", "discord_webhook": "https://d.example/w"}
        res = mock.Mock(ok=True, status_code=200)
        with mock.patch.object(alerts.requests, "post", return_value=res) as post:
            alerts.send_task_notification(task, [show()], "PVR")
        ntfy, telegram, discord = [call.kwargs["json"] for call in post.call_args_list]
        opened = f"{CINEMA}?fromdate=2026-11-10"
        self.assertEqual(ntfy["click"], opened)  # tapping the notification itself
        self.assertNotIn("actions", ntfy)        # no extra buttons under it
        self.assertIn(opened, telegram["text"])
        self.assertIn(SEATS, telegram["text"])
        self.assertEqual(discord["embeds"][0]["url"], opened)
        self.assertIn(SEATS, discord["embeds"][0]["description"])


if __name__ == "__main__":
    unittest.main()
