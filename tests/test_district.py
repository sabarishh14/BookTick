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
            self.assertIn("/movies/seat-layout/", show["url"])
            self.assertEqual(show["start"].strftime("%Y-%m-%d"), META["date"])


class Links(unittest.TestCase):
    def test_city_movie_url(self):
        self.assertEqual(
            district.city_movie_url("https://www.district.in/movies/scene-movie-tickets-MV233181", "chennai", "2026-11-09"),
            "https://www.district.in/movies/scene-movie-tickets-in-chennai-MV233181?fromdate=2026-11-09")
        self.assertIsNone(district.city_movie_url("https://www.district.in/movies/something-else", "chennai"))

    def test_booking_link_without_fid(self):
        # City pages leave out "fid"; it's the last part of the encoded session id.
        url = district.session_booking_url({"encSessionId": "1037033-35388__1__1-obcqr6-1037033"}, 233181, "x")
        self.assertTrue(url.startswith("https://www.district.in/movies/seat-layout/1037033?encsessionid=1037033-35388"))
        self.assertTrue(url.endswith("&contentid=233181"))

    def test_clean_cinema_url(self):
        self.assertEqual(district.clean_cinema_url("www.district.in/movies/x-CD1"), "https://www.district.in/movies/x-CD1")
        self.assertIsNone(district.clean_cinema_url("https://evil.example/district.in"))
        self.assertEqual(district.cinema_id_from_url("https://www.district.in/movies/pvr-x-CD1020779?fromdate=1"), "1020779")


if __name__ == "__main__":
    unittest.main()
