"""The movie picker's data and the value it sends back. No network: District is stubbed out."""
import json
import unittest
from unittest import mock

from booktick import core, views


def movie(name, langs, released=None, poster="https://cdn.district.in/p.jpg"):
    return {"id": name, "name": name, "url": None, "poster": poster, "langs": langs, "genres": [], "censor": "U",
            "released": released}


class Catalog(unittest.TestCase):
    def catalog(self, now, soon, here=()):
        with mock.patch.object(views.district, "upcoming_movies", return_value=soon):
            return json.loads(views.movie_catalog(now, here))

    def test_showing_newest_first_and_coming_soon_without_repeats(self):
        now = [movie("Scene", ["Tamil"], "2026-11-07"), movie("Ramayana", ["Hindi", "Tamil"], "2026-11-06")]
        soon = [movie("Superhero", ["Tamil"], "2099-11-18"), movie("Scene", ["Tamil"], "2026-11-07"),
                movie("Untitled", ["Marathi"])]
        cat = self.catalog(now, soon, here=["Ramayana"])
        self.assertEqual(cat["here"], ["Ramayana"])
        self.assertEqual([m["n"] for m in cat["now"]], ["Scene", "Ramayana"])
        self.assertEqual([m["n"] for m in cat["soon"]], ["Superhero", "Untitled"])  # Scene is already showing
        self.assertEqual(cat["soon"][1]["w"], "Date TBA")
        self.assertNotIn("w", cat["now"][0])

    def test_coming_soon_puts_real_releases_first_and_ones_out_elsewhere_last(self):
        soon = [movie("Out Elsewhere", ["English"], "2026-01-01"), movie("No Date", ["Tamil"]),
                movie("Later", ["Tamil"], "2099-12-01"), movie("Sooner", ["Tamil"], "2099-11-01")]
        cat = self.catalog([], soon)
        self.assertEqual([m["n"] for m in cat["soon"]], ["Sooner", "Later", "No Date", "Out Elsewhere"])
        self.assertEqual(cat["soon"][-1]["w"], "Out now")

    def test_the_citys_languages_come_first(self):
        now = [movie("A", ["Tamil"]), movie("B", ["Tamil"]), movie("C", ["English"])]
        soon = [movie("D", ["Hindi"]), movie("E", ["Hindi"]), movie("F", ["Tamil"])]
        self.assertEqual(self.catalog(now, soon)["langs"], ["Tamil", "English", "Hindi"])

    def test_coming_soon_still_works_when_district_is_down(self):
        with mock.patch.object(views.district, "upcoming_movies", side_effect=RuntimeError("down")):
            cat = json.loads(views.movie_catalog([movie("A", ["Tamil"])]))
        self.assertEqual(cat["soon"], [])


class PickerValue(unittest.TestCase):
    def test_reads_the_pickers_json_list(self):
        self.assertEqual(core.movie_text('["Avengers: Doomsday", "Superhero"]'), "Avengers: Doomsday, Superhero")
        self.assertEqual(core.movie_text("[]"), "")
        self.assertEqual(core.movie_text("Ramayana"), "Ramayana")       # plain text still works
        self.assertEqual(core.movie_text(["Ramayana", " "]), "Ramayana")

    def test_release_labels(self):
        self.assertEqual(views.release_label(None), "Date TBA")
        self.assertEqual(views.release_label("2000-01-01"), "Out now")
        self.assertEqual(views.release_label("2099-12-18"), "Fri 18 Dec")


if __name__ == "__main__":
    unittest.main()
