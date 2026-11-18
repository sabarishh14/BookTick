"""Holding an opening alert until District takes bookings there. No browser: the seat page check is faked."""
import time
import unittest
from datetime import datetime
from unittest import mock

from booktick import onsale
from booktick.core import IST

DATE = "2026-11-20"


def show(cinema, hour, avail=50, cid=None):
    cid = cid or {"PVR": 1, "INOX": 2, "AGS": 3, "Kasi": 4}.get(cinema, 9)
    return {"title": "Avengers: Doomsday", "cinema": cinema, "cinema_url": f"https://www.district.in/movies/{cinema.lower()}-CD{cid}",
            "start": datetime(2026, 11, 20, hour, 0, tzinfo=IST), "avail": avail,
            "url": f"https://www.district.in/movies/seat-layout/x?encsessionid={cid}-{hour}-a-b"}


class Verdict(unittest.TestCase):
    def setUp(self):
        onsale.reset()
        mock.patch("booktick.onsale.seatmap.enabled", return_value=True).start()
        mock.patch("booktick.onsale.add_log").start()
        self.asked = []  # (seat page, answer) for each seat page BookTick would open
        mock.patch("booktick.onsale.seatmap.check_on_sale",
                   side_effect=lambda url, done: self.asked.append((url, done)) or True).start()
        self.again = mock.Mock()

    def tearDown(self):
        mock.patch.stopall()
        onsale.reset()

    def ask(self, shows, page="p"):
        return onsale.verdict(shows, DATE, page, self.again)

    def test_asks_holds_and_opens(self):
        shows = [show("PVR", 14), show("PVR", 10)]
        self.assertEqual(self.ask(shows), ("asking", ""))
        self.assertEqual([url for url, _ in self.asked], [shows[1]["url"]])  # one seat page: the cinema's first show
        self.assertEqual(self.ask(shows, page="q"), ("asking", ""))      # already asking: no second look
        self.assertEqual(len(self.asked), 1)
        self.asked[0][1]("no", "This showtime isn't available right now")
        self.again.assert_called_once_with({"p", "q"})                    # both pages are checked again now
        self.assertEqual(self.ask(shows), ("no", "This showtime isn't available right now"))
        self.assertEqual(len(self.asked), 1)                              # not again while nothing moves
        booking = [dict(shows[0], avail=46), shows[1]]
        self.assertEqual(self.ask(booking), ("asking", ""))               # seats moving: people are booking
        self.asked[1][1]("yes", "")
        self.assertEqual(self.ask(booking), ("yes", ""))
        self.assertEqual(len(self.asked), 2)

    def test_asks_again_after_a_while(self):
        shows = [show("PVR", 10)]
        self.ask(shows)
        self.asked[0][1]("no", "")
        later = time.time() + onsale.NO_FOR + 1
        with mock.patch("booktick.onsale.time.time", return_value=later):
            self.assertEqual(self.ask(shows), ("asking", ""))
        self.assertEqual(len(self.asked), 2)

    def test_when_it_cant_tell_the_shows_count_as_open(self):
        shows = [show("PVR", 10)]
        self.ask(shows)
        self.asked[0][1]("unknown", "Timeout 30000ms exceeded")
        self.assertEqual(self.ask(shows), ("yes", ""))

    def test_no_browser_or_no_seat_page(self):
        self.assertEqual(self.ask([dict(show("PVR", 10), url="https://www.district.in/movies/pvr-CD1?x")]), ("yes", ""))
        with mock.patch("booktick.onsale.seatmap.enabled", return_value=False):
            self.assertEqual(self.ask([show("INOX", 10)]), ("yes", ""))
        self.assertEqual(self.asked, [])

    def test_a_full_queue_doesnt_hold_anything(self):
        with mock.patch("booktick.onsale.seatmap.check_on_sale", return_value=False):
            self.assertEqual(self.ask([show("PVR", 10)]), ("asking", ""))
        self.assertEqual(self.ask([show("PVR", 10)]), ("yes", ""))

    def test_a_citys_listings(self):
        shows = [show("PVR", 10), show("INOX", 11), show("AGS", 12), show("Kasi", 13)]
        self.assertEqual(self.ask(shows), ("asking", ""))
        self.assertEqual(len(self.asked), onsale.ASK_AT_ONCE)  # a few cinemas at a time
        self.asked[0][1]("no", "")
        self.assertEqual(self.ask(shows), ("asking", ""))      # one is still being asked; one more asked
        self.assertEqual(len(self.asked), onsale.ASK_AT_ONCE + 1)
        self.asked[1][1]("yes", "")
        self.assertEqual(self.ask(shows), ("yes", ""))          # one cinema taking bookings opens the page

    def test_watchers_with_other_filters_on_the_same_cinema(self):
        imax, other = show("PVR", 10), show("PVR", 13)
        self.ask([imax, other])
        self.asked[0][1]("no", "")
        self.assertEqual(self.ask([imax])[0], "no")           # a watcher that only wants IMAX sees fewer shows
        self.assertEqual(self.ask([imax, other])[0], "no")    # and that isn't seats moving
        self.assertEqual(self.ask([imax, show("PVR", 22)])[0], "no")  # nor is a show it hadn't seen
        self.assertEqual(len(self.asked), 1)

    def test_each_day_on_its_own(self):
        self.ask([show("PVR", 10)])
        self.asked[0][1]("yes", "")
        self.assertEqual(onsale.verdict([show("PVR", 10)], "2026-11-21", "p2", self.again), ("asking", ""))


if __name__ == "__main__":
    unittest.main()
