"""Who can see and change what, and the limits."""
import json
import unittest
from unittest import mock

from booktick import access
from booktick.config import MAX_WATCHERS_PER_PERSON
from booktick.core import state

KEY = "0123456789abcdef0123456789abcdef"


class Identity(unittest.TestCase):
    def test_valid_key_becomes_an_owner_hash(self):
        s = access.session_from_identity(json.dumps({"key": KEY}))
        self.assertTrue(s["owner"])
        self.assertNotIn(KEY, s["owner"])  # the key itself is never kept
        self.assertEqual(s["owner"], access.session_from_identity(json.dumps({"key": KEY}))["owner"])

    def test_bad_keys_get_no_owner(self):
        for raw in ("", "not json", json.dumps({"key": "short"}), json.dumps({"key": "Z" * 32})):
            self.assertIsNone(access.session_from_identity(raw)["owner"])

    def test_admin_needs_the_right_key(self):
        with mock.patch.object(access, "ADMIN_KEY", "s3cret"):
            self.assertTrue(access.session_from_identity(json.dumps({"key": KEY, "admin": "s3cret"}))["admin"])
            s = access.session_from_identity(json.dumps({"key": KEY, "admin": "guess"}))
            self.assertFalse(s["admin"])
            self.assertTrue(s["admin_rejected"])
        with mock.patch.object(access, "ADMIN_KEY", ""):  # no ADMIN_KEY: nobody is admin
            self.assertFalse(access.check_admin(""))


class Ownership(unittest.TestCase):
    def setUp(self):
        state["tasks"] = {"a": {"id": "a", "owner": "alice", "status": "Active"},
                          "b": {"id": "b", "owner": "bob", "status": "Active"},
                          "legacy": {"id": "legacy", "status": "Active"}}

    def tearDown(self):
        state["tasks"] = {}

    def test_people_see_only_their_own(self):
        self.assertEqual([tid for tid, _ in access.visible_tasks({"owner": "alice"})], ["a"])
        self.assertEqual(len(access.visible_tasks({"owner": "x", "admin": True})), 3)
        self.assertEqual(access.visible_tasks({}), [])

    def test_only_owners_and_admins_can_manage(self):
        self.assertTrue(access.can_manage({"owner": "alice"}, state["tasks"]["a"]))
        self.assertFalse(access.can_manage({"owner": "bob"}, state["tasks"]["a"]))
        self.assertFalse(access.can_manage({"owner": None}, state["tasks"]["legacy"]))
        self.assertTrue(access.can_manage({"admin": True}, state["tasks"]["legacy"]))


class Limits(unittest.TestCase):
    def tearDown(self):
        state["tasks"] = {}

    def test_per_person_limit(self):
        state["tasks"] = {str(i): {"owner": "alice", "status": "Active"} for i in range(MAX_WATCHERS_PER_PERSON)}
        self.assertIsNotNone(access.watcher_limit_error({"owner": "alice"}, None))
        self.assertIsNone(access.watcher_limit_error({"owner": "bob"}, None))
        self.assertIsNone(access.watcher_limit_error({"owner": "alice", "admin": True}, None))

    def test_finished_watchers_dont_count(self):
        state["tasks"] = {str(i): {"owner": "alice", "status": "Done"} for i in range(MAX_WATCHERS_PER_PERSON)}
        self.assertIsNone(access.watcher_limit_error({"owner": "alice"}, None))

    def test_test_alert_cooldown_follows_the_destination(self):
        channels = {"ntfy_topic": "bt_unit_test_topic"}
        self.assertIsNone(access.claim_test(channels))
        self.assertIn("Try again", access.claim_test(channels))
        self.assertIn("Try again", access.claim_test({"ntfy_topic": "BT_UNIT_TEST_TOPIC"}))  # same topic
        self.assertIsNone(access.claim_test({"ntfy_topic": "another_topic_xyz"}))


if __name__ == "__main__":
    unittest.main()
