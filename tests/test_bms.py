"""Watching a BookMyShow cinema. No network: BookMyShow's answer is a saved copy of a real one
(PVR Sathyam, 20 Nov 2026, trimmed), and requests are faked."""
import json
import os
import time
import unittest
from datetime import datetime
from unittest import mock

import requests

from booktick import alerts, bms, district, engine, onsale
from booktick.core import IST, seats_text, state

HERE = os.path.dirname(__file__)
LINK = "https://in.bookmyshow.com/cinemas/chen/pvr-sathyam-royapettah/buytickets/PVSR"
PAGE = f"{LINK}?fromdate=2026-11-20"


def answer():
    with open(os.path.join(HERE, "fixtures", "bms_showtimes.json"), encoding="utf-8") as f:
        return json.load(f)


class Links(unittest.TestCase):
    def test_a_pasted_cinema_link(self):
        for pasted in [f"{LINK}/20261119", "in.bookmyshow.com/cinemas/chen/pvr-sathyam-royapettah/buytickets/pvsr/",
                       f"{LINK}/20261119/filters?x=1"]:
            self.assertEqual(district.clean_cinema_url(pasted), LINK)
        self.assertEqual(district.cinema_id_from_url(LINK), "bms-PVSR")
        self.assertEqual(district.cinema_name_from_url(LINK), "Pvr Sathyam Royapettah (BookMyShow)")
        self.assertIsNone(district.clean_cinema_url("https://in.bookmyshow.com/explore/movies-chennai"))  # not a cinema
        self.assertIsNone(district.clean_cinema_url("https://in.bookmyshow.com.evil.example/cinemas/a/b/buytickets/X"))
        self.assertEqual(district.cinema_id_from_url("https://www.district.in/movies/x-CD12"), "12")  # District's as before

    def test_the_page_checked_and_the_page_opened(self):
        self.assertEqual(district.query_url(LINK, "2026-11-20"), PAGE)  # same shape as District's pages
        self.assertEqual(engine.page_base(PAGE), LINK)                   # so it's the same cinema every day
        self.assertEqual(district.cinema_link(LINK, "2026-11-20"), f"{LINK}/20261120")  # BookMyShow's own day page
        self.assertEqual(district.cinema_link(PAGE, "2026-11-21"), f"{LINK}/20261121")
        self.assertEqual(district.cinema_link("https://www.district.in/movies/x-CD1", "2026-11-20"),
                         "https://www.district.in/movies/x-CD1?fromdate=2026-11-20")

    def test_bookmyshow_seat_pages_are_never_opened(self):
        _, shows = bms.parse_showtimes(answer(), PAGE, "2026-11-20")
        self.assertIn("/seat-layout/", shows[0]["url"])
        self.assertFalse(district.is_seat_page(shows[0]["url"]))  # behind BookMyShow's bot checks
        self.assertTrue(district.is_seat_page("https://www.district.in/movies/seat-layout/x?encsessionid=1-2-3-4"))


class Reading(unittest.TestCase):
    def setUp(self):
        self.name, self.shows = bms.parse_showtimes(answer(), PAGE, "2026-11-20")

    def test_the_cinema_and_its_shows_that_day(self):
        self.assertEqual(self.name, "PVR Sathyam, Royapettah (BookMyShow)")
        self.assertEqual(len(self.shows), 4)  # the next day's show it also sent is left out
        self.assertTrue(all(s["start"].strftime("%Y-%m-%d") == "2026-11-20" for s in self.shows))
        first = self.shows[0]
        self.assertEqual((first["title"], first["format"], first["language"], first["audi"]),
                         ("O.M Operation Mole", "2D", "Tamil", "SERENE"))
        self.assertEqual(first["start"], datetime(2026, 11, 20, 7, 15, tzinfo=IST))
        self.assertEqual(first["url"], "https://in.bookmyshow.com/movies/chen/seat-layout/ET00504901/PVSR/29813/20261120")
        self.assertEqual(first["cinema_url"], LINK)
        self.assertEqual(first["price"], 54.35)

    def test_how_full_not_how_many(self):
        levels = [(s["level"], s["avail"]) for s in self.shows]
        self.assertEqual(levels, [("sold out", 0), ("almost full", None), ("seats available", None), ("filling fast", None)])
        self.assertEqual([engine.seats_open(s) for s in self.shows], [False, True, True, True])
        self.assertEqual(seats_text(self.shows[3]), "filling fast")
        self.assertEqual(engine.seat_counts(self.shows)[self.shows[0]["url"]], 0)

    def test_a_cinema_bookmyshow_says_is_down(self):
        data = answer()
        data["ShowDetails"][0]["Venues"]["isVenueDown"] = True  # "bookings aren't going through"
        _, shows = bms.parse_showtimes(data, PAGE, "2026-11-20")
        self.assertEqual(len(shows), 4)
        self.assertFalse(any(engine.seats_open(s) for s in shows))

    def test_a_day_with_nothing_yet(self):
        data = answer()
        data["ShowDetails"] = []
        self.assertEqual(bms.parse_showtimes(data, PAGE, "2026-11-20"), ("Pvr Sathyam Royapettah (BookMyShow)", []))


class Fetching(unittest.TestCase):
    def setUp(self):
        self.res = mock.Mock(status_code=200, headers={"content-type": "application/json; charset=utf-8"})
        self.res.json.return_value = answer()
        self.session = mock.Mock()
        self.session.get.return_value = self.res
        mock.patch("booktick.bms.http", return_value=self.session).start()

    def tearDown(self):
        mock.patch.stopall()

    def test_asks_for_the_cinema_and_day(self):
        name, shows = district.fetch_shows("cinema", PAGE)
        params = self.session.get.call_args.kwargs["params"]
        self.assertEqual(params, {"venueCode": "PVSR", "dateCode": "20261120"})
        self.assertEqual((name, len(shows)), ("PVR Sathyam, Royapettah (BookMyShow)", 4))

    def test_says_it_is_booktick(self):
        self.assertTrue(bms.HEADERS["User-Agent"].startswith("BookTick/"))
        self.assertNotIn("Mozilla", bms.HEADERS["User-Agent"])

    def test_a_block_page_is_an_error(self):
        self.res.headers = {"content-type": "text/html"}
        with self.assertRaises(ValueError):
            district.fetch_shows("cinema", PAGE)

    def test_turned_away(self):
        self.res.raise_for_status.side_effect = requests.HTTPError(response=mock.Mock(status_code=403))
        with self.assertRaises(requests.HTTPError) as caught:
            district.fetch_shows("cinema", PAGE)
        self.assertEqual(engine.blocked_status(caught.exception), 403)


class Watching(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "w_bms", "owner": "o", "user_label": "T", "status": "Active", "scope": "cinema",
                     "cinemas": [{"url": LINK, "name": "Pvr Sathyam Royapettah (BookMyShow)"}],
                     "movie_filter": "Ramayana", "formats": [], "time_slots": [], "target_date": "2026-11-20",
                     "max_alerts": 1, "alert_count": 0, "seats_min": 5}
        state["tasks"] = {"w_bms": self.task}
        self.send = mock.patch("booktick.engine.send_task_notification", return_value=[("ntfy", True, "sent")]).start()
        mock.patch("booktick.engine.save_tasks").start()
        mock.patch("booktick.engine.DAY_END_MARGIN", -10 ** 9).start()
        self.maps = mock.patch("booktick.engine.seatmap.request").start()
        self.asked = mock.patch("booktick.onsale.seatmap.check_on_sale").start()
        mock.patch("booktick.onsale.seatmap.enabled", return_value=True).start()
        onsale.reset()

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}
        onsale.reset()

    def check(self, data):
        place, shows = bms.parse_showtimes(data, PAGE, "2026-11-20")
        self.task["last_alert_ts"] = 0
        engine.check_task(self.task, PAGE, "2026-11-20", place, shows)

    def test_bookings_open_at_a_bookmyshow_cinema(self):
        empty = answer()
        empty["ShowDetails"][0]["Event"] = [e for e in empty["ShowDetails"][0]["Event"] if e["EventTitle"] != "Ramayana"]
        self.check(empty)
        self.send.assert_not_called()
        self.check(answer())  # Ramayana's shows appear, with seats
        self.assertEqual([c.kwargs["kind"] for c in self.send.call_args_list], ["opened"])
        shows = self.send.call_args.args[1]
        self.assertEqual(len(shows), 2)
        self.assertEqual(self.task["cinemas"][0]["name"], "PVR Sathyam, Royapettah (BookMyShow)")
        self.assertEqual(self.task["alert_log"][-1]["link"], f"{LINK}/20261120")
        self.asked.assert_not_called()  # no District seat page to check
        self.maps.assert_not_called()   # and no seat maps

    def test_closed_when_bookmyshow_says_the_cinema_is_down(self):
        self.check(answer())
        down = answer()
        down["ShowDetails"][0]["Venues"]["isVenueDown"] = True
        self.check(down)
        self.assertEqual([c.kwargs["kind"] for c in self.send.call_args_list], ["opened", "closed"])
        self.check(answer())
        self.assertEqual([c.kwargs["kind"] for c in self.send.call_args_list], ["opened", "closed", "reopened"])

    def test_the_alert_opens_bookmyshow(self):
        _, shows = bms.parse_showtimes(answer(), PAGE, "2026-11-20")
        links = alerts.alert_links(shows[2])
        self.assertEqual(links[0], ("Open on BookMyShow", f"{LINK}/20261120"))
        self.assertEqual(links[1][0], "Pick seats (web)")


class OnDistrictToo(unittest.TestCase):
    """A cinema on both sites is watched on District, which BookTick reads more reliably."""
    SATHYAM = {"name": "PVR: Sathyam, Royapettah", "lat": 13.0554, "lon": 80.258}

    def setUp(self):
        for key in [k for k in district._cache if k.startswith(("city:", "bms-"))]:
            district._cache.pop(key)
        district._cache["city:testtwin"] = {"value": {"movies": [], "cinemas": [
            {"id": "1022244", "name": "PVR Sathyam, Royapettah, Chennai", "lat": 13.05545, "lon": 80.25810,
             "url": "https://www.district.in/movies/pvr-sathyam-royapettah-chennai-CD1022244", "movies": []},
            {"id": "2", "name": "HDFC Millennia PVR Escape-Express Avenue Mall, Chennai", "lat": 13.0587, "lon": 80.2642,
             "url": "https://www.district.in/movies/pvr-escape-CD2", "movies": []},
            {"id": "3", "name": "INOX The Marina Mall, Chennai", "lat": 12.9000, "lon": 80.2300,
             "url": "https://www.district.in/movies/inox-marina-CD3", "movies": []},
        ]}, "expires": time.time() + 600, "loaded": time.time()}
        self.venue = mock.patch("booktick.bms.venue", return_value=dict(self.SATHYAM)).start()

    def tearDown(self):
        mock.patch.stopall()
        for key in [k for k in district._cache if k.startswith(("city:", "bms-"))]:
            district._cache.pop(key)

    def test_close_names_close_by_are_still_two_cinemas(self):
        kasi = {"id": "9", "name": "Kasi RGB Laser Dolby Atmos, Ashok Nagar, Chennai", "lat": 13.0360, "lon": 80.2120}
        self.assertIsNone(district.twin_in({"name": "Kasi Talkies Dolby Atmos: Ashok Nagar", "lat": 13.0365,
                                            "lon": 80.2135}, [kasi]))  # 170 m apart, but Kasi Talkies is another cinema

    def test_every_word_of_the_brand(self):
        ags = {"id": "5", "name": "AGS Cinemas OMR, Navlur, Chennai", "lat": 12.8460, "lon": 80.2260}
        self.assertIs(district.twin_in({"name": "AGS Cinemas OMR: Navlur", "lat": 12.8462, "lon": 80.2262}, [ags]), ags)
        escape = {"id": "2", "name": "HDFC Millennia PVR Escape-Express Avenue Mall, Chennai", "lat": 13.0587, "lon": 80.2642}
        self.assertIs(district.twin_in({"name": "HDFC Millennia PVR: Escape-Express Avenue Mall", "lat": 13.0590,
                                        "lon": 80.2640}, [escape]), escape)
        vr = {"id": "6", "name": "PVR VR Mall, Anna Nagar, Chennai", "lat": 13.0800, "lon": 80.1965}
        self.assertIs(district.twin_in({"name": "PVR: VR Chennai, Anna Nagar", "lat": 13.0830, "lon": 80.1970}, [vr]),
                      vr)  # 339 m: a big mall

    def test_two_of_a_chain_in_one_mall(self):
        icon = {"id": "1", "name": "PVR ICON Phoenix Palladium, Lower Parel, Mumbai", "lat": 18.9940, "lon": 72.8240}
        plain = {"id": "2", "name": "PVR Phoenix, Lower Parel, Mumbai", "lat": 18.99405, "lon": 72.8240}
        self.assertIs(district.twin_in({"name": "PVR: ICON Phoenix Palladium", "lat": 18.99406, "lon": 72.8240},
                                       [plain, icon]), icon)  # the nearer one isn't it

    def test_bookmyshow_only_cinemas_in_the_list(self):
        from booktick import handlers
        venues = [dict(self.SATHYAM, code="PVSR", down=False, url=LINK),
                  {"code": "RSSC", "name": "Rohini Silver Screens: Koyambedu", "lat": 13.0735, "lon": 80.1950, "down": False,
                   "url": "https://in.bookmyshow.com/cinemas/chennai/rohini-silver-screens-koyambedu/buytickets/RSSC"}]
        mock.patch("booktick.bms.regions", return_value=[{"code": "CHEN", "slug": "chennai", "names": {"testtwin"}}]).start()
        mock.patch("booktick.bms.city_venues", return_value=venues).start()
        directory = district._cache["city:testtwin"]["value"]
        catalog = json.loads(handlers.cinema_catalog("testtwin", directory))
        bms_rows = [c for c in catalog["cinemas"] if c.get("s") == "bms"]
        self.assertEqual([(c["n"], c["a"]) for c in bms_rows], [("Rohini Silver Screens", "Koyambedu, BookMyShow")])
        self.assertEqual(len(catalog["cinemas"]), 4)  # District's 3, and Rohini; Sathyam is District's
        self.assertEqual(district.known_cinema_name(bms_rows[0]["u"]), "Rohini Silver Screens, Koyambedu (BookMyShow)")
        self.venue.assert_not_called()  # the city's list had its position

    def test_bookmyshow_not_answering_leaves_districts_list(self):
        from booktick import handlers
        mock.patch("booktick.bms.regions", side_effect=requests.ConnectionError("down")).start()
        catalog = json.loads(handlers.cinema_catalog("testtwin", district._cache["city:testtwin"]["value"]))
        self.assertEqual(len(catalog["cinemas"]), 3)

    def test_bookmyshow_cities(self):
        known = [{"code": "MUMBAI", "slug": "mumbai", "names": {"mumbai", "bombay", "navi mumbai"}},
                 {"code": "NCR", "slug": "national-capital-region-ncr", "names": {"delhi", "noida", "gurugram (gurgaon)"}},
                 {"code": "TRIV", "slug": "thiruvananthapuram-trivandrum", "names": {"thiruvananthapuram-trivandrum"}}]
        found = {c: (bms.region_for(c, known) or {}).get("code") for c in
                 ["mumbai", "navi-mumbai", "new-delhi", "gurgaon", "noida", "trivandrum", "vizag"]}
        self.assertEqual(found, {"mumbai": "MUMBAI", "navi-mumbai": "MUMBAI", "new-delhi": "NCR", "gurgaon": "NCR",
                                 "noida": "NCR", "trivandrum": "TRIV", "vizag": None})

    def test_bookmyshow_names(self):
        self.assertEqual(bms.cinema_name({"VenueName": "PVR: Sathyam, Royapettah"}), "PVR Sathyam, Royapettah (BookMyShow)")
        self.assertEqual(bms.cinema_name({"VenueName": "Rohini Silver Screens: Koyambedu"}),
                         "Rohini Silver Screens, Koyambedu (BookMyShow)")

    def test_the_same_cinema_is_found_on_district(self):
        twin = district.district_twin(LINK)
        self.assertEqual(twin["id"], "1022244")  # 16 m away, and PVR; not PVR Escape, 726 m away
        district.district_twin(LINK)
        self.assertEqual(self.venue.call_count, 1)  # BookMyShow is asked once a day

    def test_another_chain_in_the_same_spot_isnt(self):
        self.venue.return_value = {"name": "INOX: Sathyam Mall", "lat": 13.0554, "lon": 80.258}
        self.assertIsNone(district.district_twin(LINK))

    def test_a_bookmyshow_only_cinema_stays_on_bookmyshow(self):
        self.venue.return_value = {"name": "Rohini Silver Screens: Koyambedu", "lat": 13.0735, "lon": 80.1950}
        self.assertIsNone(district.district_twin(LINK))
        self.venue.return_value = None  # no shows listed, so no position to go by
        district._cache.pop("bms-venue:PVSR")
        self.assertIsNone(district.district_twin(LINK))

    def test_bookmyshow_not_answering_keeps_it_on_bookmyshow(self):
        self.venue.side_effect = requests.ConnectionError("down")
        self.assertIsNone(district.district_twin(LINK))

    def test_pasting_it_adds_the_district_cinema(self):
        from booktick import handlers
        add, note, _ = handlers.on_link_input(f"{LINK}/20261119", "testtwin")
        self.assertEqual(json.loads(add)["u"], "https://www.district.in/movies/pvr-sathyam-royapettah-chennai-CD1022244")
        self.assertIn("is on District too", note)
        self.assertEqual(handlers.watch_targets(json.dumps([LINK]))[1],
                         ["https://www.district.in/movies/pvr-sathyam-royapettah-chennai-CD1022244"])
        self.venue.return_value = {"name": "Rohini Silver Screens: Koyambedu", "lat": 13.0735, "lon": 80.1950}
        district._cache.pop("bms-venue:PVSR")
        add, note, _ = handlers.on_link_input(f"{LINK}/20261119", "testtwin")
        self.assertEqual(json.loads(add)["u"], LINK)


class Pace(unittest.TestCase):
    def setUp(self):
        mock.patch("booktick.engine.save_tasks").start()
        engine._pages.clear()
        for b in engine._blocks.values():
            b.update(until=0.0, strikes=0)
        self.interval = state.get("interval_sec")

    def tearDown(self):
        mock.patch.stopall()
        for b in engine._blocks.values():
            b.update(until=0.0, strikes=0)
        state["interval_sec"] = self.interval

    def test_bookmyshow_turning_us_away_only_pauses_bookmyshow(self):
        err = requests.HTTPError(response=mock.Mock(status_code=403))
        with mock.patch("booktick.engine.add_log") as log:
            engine.page_failed(PAGE, {"kind": "cinema", "tasks": []}, err)
        self.assertGreater(engine._blocks["bms"]["until"], time.time() + 30)
        self.assertEqual(engine._block["until"], 0.0)  # District carries on
        self.assertIn("BookMyShow turned a request away", log.call_args_list[0].args[0])

    def test_never_faster_than_bookmyshow_asked(self):
        state["interval_sec"] = 5
        engine.page_succeeded(PAGE, "cinema", {"kind": "cinema", "tasks": []})
        self.assertGreaterEqual(engine._pages[PAGE]["next"] - time.time(), engine.BMS_MIN_INTERVAL * 0.9 - 1)
        engine.page_succeeded("https://www.district.in/movies/x-CD1?fromdate=2026-11-20", "cinema",
                              {"kind": "cinema", "tasks": []})
        self.assertLess(engine._pages["https://www.district.in/movies/x-CD1?fromdate=2026-11-20"]["next"] - time.time(), 6)


if __name__ == "__main__":
    unittest.main()
