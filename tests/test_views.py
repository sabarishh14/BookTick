"""What a watcher card shows to its owner and to an admin."""
import unittest

from booktick import views

TASK = {"id": "w_abc", "owner": "alice", "user_label": "Alice", "status": "Active", "phase": "waiting",
        "movie_filter": "Ramayana", "format_filter": "IMAX", "target_date": "2026-11-14", "created_at": "11-Nov 09:40",
        "ntfy_topic": "alice_tickets_982", "telegram_token": "123456:SECRET-TOKEN", "telegram_chat_id": "555123",
        "discord_webhook": "https://discord.com/api/webhooks/1/secretpartXYZ9"}


class Cards(unittest.TestCase):
    def test_owner_sees_their_own_topics_but_no_secrets(self):
        card = views.watcher_card("w_abc", TASK, {"owner": "alice"})
        self.assertIn("alice_tickets_982", card)
        self.assertIn("chat 555123", card)
        self.assertNotIn("SECRET-TOKEN", card)
        self.assertNotIn("secretpart", card)
        self.assertNotIn("bt-admin-meta", card)  # the id line is for admins

    def test_without_ownership_only_channel_names(self):
        card = views.watcher_card("w_abc", TASK, {"owner": "someone-else"})
        self.assertIn(">ntfy<", card)
        self.assertNotIn("alice_tickets_982", card)

    def test_admin_sees_where_alerts_go_but_no_secrets(self):
        card = views.watcher_card("w_abc", TASK, {"owner": "bob", "admin": True})
        self.assertIn("alice_tickets_982", card)        # ntfy topic
        self.assertIn("chat 555123", card)              # Telegram chat
        self.assertIn("webhook …XYZ9", card)            # only the end of the Discord webhook
        self.assertNotIn("SECRET-TOKEN", card)
        self.assertNotIn("secretpart", card)
        self.assertIn("Watcher <code>w_abc</code>", card)
        self.assertIn('data-bt-action="delete"', card)  # admins can delete anyone's watcher
        self.assertIn("not yours", card)


class AdminView(unittest.TestCase):
    def setUp(self):
        from booktick.core import state
        self.state = state
        state["tasks"] = {
            "w_abc": dict(TASK, created_ts=1),
            "w_def": dict(TASK, id="w_def", movie_filter="Jailer 2", status="Paused", created_ts=3),
            "w_mine": dict(TASK, id="w_mine", owner="admin-owner", user_label="Brave Otter 27", ntfy_topic="otter_alerts",
                           created_ts=2),
        }

    def tearDown(self):
        self.state["tasks"] = {}

    def test_admin_sees_everyone_grouped_by_person_with_their_own_first(self):
        page = views.watchers_html({"owner": "admin-owner", "admin": True})
        self.assertEqual(page.count('class="bt-person"'), 2)
        self.assertLess(page.index("Brave Otter 27"), page.index("Alice"))       # your own group first
        self.assertIn('class="bt-you"', page)
        self.assertEqual(page.count('<details class="bt-wrow"'), 3)              # one compact row per watcher…
        self.assertEqual(page.count('<article class="bt-card"'), 3)             # …with its full card inside
        self.assertIn('data-groups="Active Paused"', page)                      # so the filters can hide whole people
        self.assertEqual(page.count("alice_tickets_982"), 3)                   # header chips, once per person, plus each card

    def test_everyone_else_sees_their_own_cards(self):
        page = views.watchers_html({"owner": "alice"})
        self.assertNotIn("bt-person", page)
        self.assertEqual(page.count('<article class="bt-card"'), 2)


class Names(unittest.TestCase):
    def test_each_browser_gets_its_own_steady_name(self):
        from booktick.names import name_for
        a, b = name_for("3f9a0c1d2e4b5a6c7d8e9f01"), name_for("0123456789abcdef01234567")
        self.assertEqual(a, name_for("3f9a0c1d2e4b5a6c7d8e9f01"))
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^[A-Z][a-z]+ [A-Z][a-z]+ \d\d$")


class Page(unittest.TestCase):
    def test_header_links_the_guide_and_only_the_header_credits_the_author(self):
        self.assertIn('data-bt-goto="guide"', views.header_html())
        self.assertIn(views.AUTHOR_NAME, views.header_html())
        self.assertNotIn(views.AUTHOR_NAME, views.footer_html())
        self.assertIn("isn't affiliated with District", views.footer_html())
        self.assertNotIn(views.AUTHOR_NAME, views.about_html())

    def test_guide_covers_setup_alerts_and_management(self):
        from booktick.guide import GUIDE_HTML
        for heading in ("1. Set up a watcher", "2. Choose where alerts go", "3. What you'll get", "4. Manage your watchers"):
            self.assertIn(heading, GUIDE_HTML)


if __name__ == "__main__":
    unittest.main()
