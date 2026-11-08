"""The database writer, against a fake connection: it should write only what changed."""
import sys
import types
import unittest
from unittest import mock

from booktick import storage
from booktick.core import state


class FakeDB:
    def __init__(self):
        self.upserts, self.deletes = [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        self.upserts.append(sorted(tid for tid, _ in rows))

    def execute(self, sql, params=None):
        self.deletes.append(sorted(params[0]))


class Writer(unittest.TestCase):
    def setUp(self):
        # db_flush imports Jsonb from psycopg; a stand-in keeps this test free of the driver.
        fake = types.ModuleType("psycopg.types.json")
        fake.Jsonb = lambda data: data
        self.modules = mock.patch.dict(sys.modules, {"psycopg": types.ModuleType("psycopg"),
                                                      "psycopg.types": types.ModuleType("psycopg.types"),
                                                      "psycopg.types.json": fake})
        self.modules.start()
        self.db = FakeDB()
        mock.patch.object(storage, "db_connect", return_value=self.db).start()
        storage._db.update(ready=True, stored={})
        state["tasks"] = {"a": {"id": "a", "status": "Active"}, "b": {"id": "b", "status": "Active"}}

    def tearDown(self):
        mock.patch.stopall()
        self.modules.stop()
        state["tasks"] = {}
        storage._db.update(ready=False, stored={})

    def test_writes_only_changes(self):
        storage.db_flush()
        self.assertEqual(self.db.upserts, [["a", "b"]])
        storage.db_flush()  # nothing changed: no database call at all
        self.assertEqual(self.db.upserts, [["a", "b"]])
        state["tasks"]["a"]["status"] = "Paused"
        storage.db_flush()
        self.assertEqual(self.db.upserts[-1], ["a"])

    def test_removes_deleted_watchers(self):
        storage.db_flush()
        del state["tasks"]["b"]
        storage.db_flush()
        self.assertEqual(self.db.deletes, [["b"]])

    def test_per_check_details_are_not_saved_or_counted_as_changes(self):
        storage.db_flush()
        state["tasks"]["a"].update(last_checked="10:00:00", last_checked_ts=1.0, last_result="No match", last_alert_ts=2.0)
        storage.db_flush()
        self.assertEqual(len(self.db.upserts), 1)
        self.assertNotIn("last_result", storage._db["stored"]["a"])


if __name__ == "__main__":
    unittest.main()
