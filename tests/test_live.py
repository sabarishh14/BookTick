"""Checks District's real pages still parse. Skipped unless BOOKTICK_LIVE=1.

GitHub Actions runs this daily (see .github/workflows/tests.yml), so a District redesign
shows up as a failed run instead of silently missed alerts.
"""
import os
import unittest
from datetime import timedelta

from booktick import district
from booktick.core import now_ist

CINEMA_URL = "https://www.district.in/movies/inox-phoenix-market-city-formerly-jazz-cinemas-velachery-chennai-CD1020779"


@unittest.skipUnless(os.environ.get("BOOKTICK_LIVE") == "1", "set BOOKTICK_LIVE=1 to check District's live pages")
class LiveDistrict(unittest.TestCase):
    date = str((now_ist() + timedelta(days=1)).date())

    def test_city_directory(self):
        d = district.parse_city_page(district.fetch_html("https://www.district.in/movies/cinemas-in-chennai"))
        self.assertGreater(len(d["cinemas"]), 20)
        self.assertGreater(len(d["movies"]), 3)
        LiveDistrict.movie = d["movies"][0]

    def test_cinema_page(self):
        name, shows = district.fetch_shows("cinema", district.query_url(CINEMA_URL, self.date))
        self.assertIn("INOX", name)
        self.assertGreater(len(shows), 0)
        self.assertTrue(any(isinstance(s["avail"], int) for s in shows))

    def test_city_movie_page(self):
        d = district.parse_city_page(district.fetch_html("https://www.district.in/movies/cinemas-in-chennai"))
        for movie in d["movies"][:5]:
            name, shows = district.fetch_shows("city", district.city_movie_url(movie["url"], "chennai", self.date))
            if shows:
                self.assertEqual(name, movie["name"])
                self.assertTrue(all(s["cinema"] for s in shows))
                return
        self.fail("none of the first five movies in Chennai had shows tomorrow")

    def test_cities_sitemap(self):
        self.assertGreater(len(district.load_cities()), 100)


if __name__ == "__main__":
    unittest.main()
