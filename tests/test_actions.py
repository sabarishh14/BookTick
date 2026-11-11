"""The buttons in alerts: what they change, and the page Telegram and Discord link to."""
import unittest
from unittest import mock

from booktick import actions
from booktick.core import state

TOKEN = "abcdefghijklmnopqrstuvwx"


class Buttons(unittest.TestCase):
    def setUp(self):
        mock.patch("booktick.actions.save_tasks").start()
        self.task = {"id": "w1", "owner": "o", "user_label": "Me", "movie_filter": "Ramayana", "status": "Active",
                     "phase": "open", "reminders_left": 3, "action_token": TOKEN}
        state["tasks"] = {"w1": self.task}

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def test_booked_stops_the_watcher(self):
        ok, message = actions.apply(TOKEN, "stop")
        self.assertTrue(ok)
        self.assertEqual(self.task["status"], "Done")
        self.assertTrue(self.task["booked"])

    def test_keep_watching_only_stops_the_reminders(self):
        ok, _ = actions.apply(TOKEN, "keep")
        self.assertTrue(ok)
        self.assertEqual(self.task["status"], "Active")
        self.assertEqual(self.task["reminders_left"], 0)

    def test_unknown_tokens_and_choices_do_nothing(self):
        self.assertFalse(actions.apply("x" * 24, "stop")[0])
        self.assertFalse(actions.apply("", "stop")[0])
        self.assertFalse(actions.apply(TOKEN, "delete")[0])
        self.assertEqual(self.task["status"], "Active")

    def test_the_page_asks_first(self):
        html = actions.page_html(TOKEN)
        self.assertIn("Did you get tickets?", html)
        self.assertIn(f'action="/watch/{TOKEN}/stop"', html)
        self.assertEqual(self.task["status"], "Active")  # opening the page changes nothing

    def test_urls_need_a_public_address(self):
        with mock.patch.object(actions, "PUBLIC_URL", ""):
            self.assertIsNone(actions.action_url(self.task, "stop"))
        with mock.patch.object(actions, "PUBLIC_URL", "https://booktick.example"):
            self.assertEqual(actions.action_url(self.task, "stop"), f"https://booktick.example/watch/{TOKEN}/stop")


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # the CI unit job installs only requests
    FastAPI = None


@unittest.skipIf(FastAPI is None, "needs fastapi")
class Endpoints(unittest.TestCase):
    def setUp(self):
        mock.patch("booktick.actions.save_tasks").start()
        self.task = {"id": "w1", "movie_filter": "Ramayana", "status": "Active", "action_token": TOKEN}
        state["tasks"] = {"w1": self.task}
        app = FastAPI()
        actions.register(app)
        self.client = TestClient(app)

    def tearDown(self):
        mock.patch.stopall()
        state["tasks"] = {}

    def test_get_shows_the_page_and_post_acts(self):
        self.assertEqual(self.client.get(f"/watch/{TOKEN}").status_code, 200)
        self.assertEqual(self.task["status"], "Active")
        res = self.client.post(f"/watch/{TOKEN}/stop")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.task["status"], "Done")
        self.assertEqual(self.client.post("/watch/wrong-token-wrong-token/stop").status_code, 404)


if __name__ == "__main__":
    unittest.main()
