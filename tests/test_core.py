"""Dates and ranges."""
import unittest
from datetime import timedelta

from booktick.core import dates_label, now_ist, parse_dates, task_dates


def day(n):
    return str(now_ist().date() + timedelta(days=n))


class Dates(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(parse_dates("2026-11-13"), ("2026-11-13", "2026-11-13"))

    def test_range(self):
        self.assertEqual(parse_dates("2026-11-13..2026-11-15"), ("2026-11-13", "2026-11-15"))

    def test_range_backwards_is_swapped(self):
        self.assertEqual(parse_dates("2026-11-15..2026-11-13"), ("2026-11-13", "2026-11-15"))

    def test_range_limit(self):
        self.assertIsNotNone(parse_dates("2026-11-01..2026-11-07"))  # 7 days
        self.assertIsNone(parse_dates("2026-11-01..2026-11-08"))     # 8 days

    def test_invalid(self):
        self.assertIsNone(parse_dates("next friday"))
        self.assertIsNone(parse_dates("2026-11-01..2026-11-02..2026-11-03"))

    def test_task_dates(self):
        self.assertEqual(task_dates({"target_date": "2026-11-13", "target_date_to": "2026-11-15"}),
                         ["2026-11-13", "2026-11-14", "2026-11-15"])
        self.assertEqual(task_dates({"target_date": "2026-11-13"}), ["2026-11-13"])  # watchers saved before ranges

    def test_labels(self):
        self.assertTrue(dates_label(day(0)).startswith("Today · "))
        self.assertTrue(dates_label(day(1)).startswith("Tomorrow · "))
        self.assertEqual(dates_label("2026-11-13", "2026-11-15"), "Fri 13 – Sun 15 Nov · 3 days")
        self.assertEqual(dates_label("2026-11-29", "2026-12-02"), "Sun 29 Nov – Wed 02 Dec · 4 days")


if __name__ == "__main__":
    unittest.main()
