"""The District parsers, run on trimmed copies of real pages (see make_fixtures.py)."""
import io
import json
import os
import unittest

from booktick import district
from booktick.core import IST

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with io.open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


META = json.loads(fixture("meta.json"))


class CinemaPage(unittest.TestCase):
    def setUp(self):
        self.name, self.shows = district.parse_cinema_page(fixture("cinema_page.html"), META["cinema_url"])

    def test_uses_districts_own_cinema_name(self):
        self.assertIn("INOX Phoenix Market City", self.name)
        self.assertIn("(", self.name)  # the real name, not one rebuilt from the link

    def test_finds_shows_with_times_titles_and_links(self):
        self.assertGreater(len(self.shows), 0)
        for show in self.shows:
            self.assertTrue(show["title"])
            self.assertEqual(show["start"].utcoffset(), IST.utcoffset(None))
            self.assertTrue(show["url"].startswith("https://www.district.in/"))
            self.assertEqual(show["cinema"], self.name)

    def test_has_seat_counts(self):
        self.assertTrue(any(isinstance(s["avail"], int) for s in self.shows))

    def test_shows_are_sorted_by_time(self):
        starts = [s["start"] for s in self.shows if s["start"]]
        self.assertEqual(starts, sorted(starts))


class SameTimeTwoScreens(unittest.TestCase):
    """Two screens showing the same movie at the same time must each keep their own seat link."""

    def test_each_show_keeps_its_own_link(self):
        sessions = [{"sid": sid, "cid": 1020779, "mid": "OBCQR6", "fid": "42vo6x7qxpo", "showTime": "2026-11-14T17:00",
                     "total": 280, "avail": avail, "audi": audi, "scrnFmt": "2D"}
                    for sid, avail, audi in (("69353", 43, "SCREEN 8"), ("69286", 64, "SCREEN 4"))]
        data = {"props": {"pageProps": {"data": {"pageData": {"sessions": sessions},
                                                 "groupedMovies": [{"id": "OBCQR6", "name": "Scene", "contentId": 233181}]}}}}
        events = [{"@type": "ScreeningEvent", "name": "Scene (2D)", "startDate": "2026-11-14T22:30:00+05:30",
                   "workPresented": {"name": "Scene"},
                   "offers": {"url": f"https://www.district.in/movies/seat-layout/42vo6x7qxpo?encsessionid=1020779-{sid}-"
                                     f"obcqr6-42vo6x7qxpo&fromdate=2026-11-14"}}
                  for sid in ("69353", "69286")]
        page = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
                + "".join(f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events))
        _, shows = district.parse_cinema_page(page, "https://www.district.in/movies/inox-CD1020779")
        links = {s["audi"]: s["url"] for s in shows}
        self.assertEqual(len(shows), 2)
        self.assertIn("-69353-", links["SCREEN 8"])
        self.assertIn("-69286-", links["SCREEN 4"])


class UpcomingPage(unittest.TestCase):
    """District's upcoming-movies page, cut down to the shape it has."""

    def page(self, movies):
        rails = [{"items": [{"ItemDetails": {"MovieData": m}} for m in movies]}]
        data = {"props": {"pageProps": {"data": {"serverState": {"EDSResponse": {"rails": rails}}}}}}
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

    def test_soonest_first_with_posters_and_languages(self):
        movies = [
            {"movie_id": "2", "name": "Avengers: Doomsday", "release_date": "1797465600", "languages": ["English", "Hindi"],
             "image": "https://cdn.district.in/a.jpg", "genres": ["Action"], "censor": "TBA",
             "web_url": "https://www.district.in/movies/avengers-doomsday-movie-tickets-MV176735"},
            {"movie_id": "1", "name": "Superhero", "release_date": "1794960000", "movie_variants": [{"language": "Tamil"}],
             "image": "https://cdn.district.in/s.jpg"},
            {"movie_id": "3", "name": "No Date Yet", "release_date": "", "image": "javascript:alert(1)"},
        ]
        out = district.parse_upcoming_page(self.page(movies))
        self.assertEqual([m["name"] for m in out], ["Superhero", "Avengers: Doomsday", "No Date Yet"])
        self.assertEqual(out[0]["released"], "2026-11-18")
        self.assertEqual(out[0]["langs"], ["Tamil"])          # from the variants when there's no list
        self.assertEqual(out[1]["langs"], ["English", "Hindi"])
        self.assertIsNone(out[1]["censor"])                  # "TBA" isn't a rating
        self.assertIsNone(out[2]["released"])
        self.assertIsNone(out[2]["poster"])                  # only https images

    def test_an_empty_page_is_an_error(self):
        with self.assertRaises(ValueError):
            district.parse_upcoming_page(self.page([]))

    def test_a_far_off_placeholder_date_means_not_announced(self):
        out = district.parse_upcoming_page(self.page([{"movie_id": "9", "name": "Wife", "release_date": "3786825600"}]))
        self.assertIsNone(out[0]["released"])  # District's 2090-01-01

    def test_movies_home_page_stream(self):
        movie = {"movie_id": "176735", "name": "Avengers: Doomsday", "release_date": "1797465600",
                 "languages": ["English"], "image": "https://cdn.district.in/a.jpg"}
        stream = json.dumps({"rails": [{"items": [{"ItemDetails": {"MovieData": movie}}]}]})
        half = len(stream) // 2  # the page splits its data across several chunks
        page = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>" for part in (stream[:half], stream[half:]))
        out = district.parse_movies_home(page)
        self.assertEqual([(m["name"], m["released"]) for m in out], [("Avengers: Doomsday", "2026-12-17")])


class CityPage(unittest.TestCase):
    def setUp(self):
        self.directory = district.parse_city_page(fixture("city_page.html"))

    def test_lists_cinemas_with_links_and_movies(self):
        cinemas = self.directory["cinemas"]
        self.assertGreater(len(cinemas), 0)
        for c in cinemas:
            self.assertRegex(c["url"], r"^https://www\.district\.in/movies/.+-CD\d+$")
            self.assertTrue(c["name"])

    def test_lists_movies_with_ids_and_pages(self):
        movies = self.directory["movies"]
        self.assertGreater(len(movies), 0)
        for m in movies:
            self.assertTrue(m["id"].isdigit())
            self.assertIsNotNone(district.city_movie_url(m["url"], "chennai"))

    def test_movies_carry_what_the_picker_shows(self):
        for m in self.directory["movies"]:
            self.assertEqual(set(m), {"id", "name", "url", "poster", "langs", "genres", "censor", "released"})
            self.assertTrue(m["poster"] is None or m["poster"].startswith("https://"))

    def test_matching_movies(self):
        name = self.directory["movies"][0]["name"]
        found = district.matching_movies(self.directory["movies"], [name.lower()[:5]])
        self.assertIn(name, [m["name"] for m in found])
        self.assertEqual(district.matching_movies(self.directory["movies"], ["no such movie zzz"]), [])


class CityMoviePage(unittest.TestCase):
    def test_finds_shows_across_cinemas(self):
        movie, shows = district.parse_city_movie_page(fixture("city_movie_page.html"), "https://www.district.in/x")
        self.assertEqual(movie, META["movie"]["name"])
        self.assertGreater(len(shows), 0)
        for show in shows:
            self.assertTrue(show["cinema"])
            self.assertRegex(show["url"], r"/movies/seat-layout/([^?]+)\?encsessionid=[^&]+-\1&fromdate=" + META["date"])
            self.assertEqual(show["start"].strftime("%Y-%m-%d"), META["date"])


class Links(unittest.TestCase):
    def test_city_movie_url(self):
        self.assertEqual(
            district.city_movie_url("https://www.district.in/movies/scene-movie-tickets-MV233181", "chennai", "2026-11-09"),
            "https://www.district.in/movies/scene-movie-tickets-in-chennai-MV233181?fromdate=2026-11-09")
        self.assertIsNone(district.city_movie_url("https://www.district.in/movies/something-else", "chennai"))

    def test_city_page_booking_link(self):
        # City pages have "mcd" instead of "fid", and an encSessionId ending in the cinema's id.
        # That id in the link makes District say "Booking is now closed".
        session = {"cid": 1037033, "sid": "35388__1__1", "mid": "OBCQR6", "mcd": "42vo6x7qxpo",
                   "encSessionId": "1037033-35388__1__1-obcqr6-1037033", "showTime": "2026-11-10T06:00"}
        self.assertEqual(
            district.session_booking_url(session, 233181, "x"),
            "https://www.district.in/movies/seat-layout/42vo6x7qxpo?encsessionid=1037033-35388__1__1-obcqr6-42vo6x7qxpo"
            "&fromdate=2026-11-10&freeseating=false&fromsessions=true&type=CINEMAS&contentid=233181")

    def test_booking_link_needs_the_format_code(self):
        self.assertEqual(district.session_booking_url({"encSessionId": "1-2-3-4"}, 1, "fallback"), "fallback")

    def test_clean_cinema_url(self):
        self.assertEqual(district.clean_cinema_url("www.district.in/movies/x-CD1"), "https://www.district.in/movies/x-CD1")
        self.assertIsNone(district.clean_cinema_url("https://evil.example/district.in"))
        self.assertEqual(district.cinema_id_from_url("https://www.district.in/movies/pvr-x-CD1020779?fromdate=1"), "1020779")


if __name__ == "__main__":
    unittest.main()
