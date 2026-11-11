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


if __name__ == "__main__":
    unittest.main()
