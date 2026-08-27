"""The roster of users the runners have seen.

Exists so rights can be assigned by picking a person instead of copying a UUID
out of a log. A roster, not an audit trail: one row per user, overwritten as
they return, no per-call history and nothing that could not be shown in a
dropdown.
"""
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import app.identity_registry as registry
from app.identity import SOURCE_HEADERS, Identity


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        registry.close()
        self.patch = patch.object(registry, "DB_PATH", pathlib.Path(self.tmp.name) / "identities.db")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(registry.close)

    def test_a_caller_is_remembered_with_their_claims(self):
        registry.record(Identity(sub="sub-anna", email="anna@example.org", name="Anna", role="user"), "inst")
        entry = registry.known()[0]
        self.assertEqual(entry["sub"], "sub-anna")
        self.assertEqual(entry["email"], "anna@example.org")
        self.assertEqual(entry["role"], "user")
        self.assertEqual(entry["last_instance"], "inst")

    def test_returning_costs_one_row_not_one_per_call(self):
        for _ in range(20):
            registry.record(Identity(sub="sub-anna", email="anna@example.org"), "inst")
        self.assertEqual(1, len(registry.known()))

    def test_changed_claims_are_written_through_immediately(self):
        """A renamed or re-roled user must not sit in the list under their old
        details until the refresh window happens to expire."""
        registry.record(Identity(sub="sub-anna", name="Anna", role="user"), "inst")
        registry.record(Identity(sub="sub-anna", name="Anna B", role="admin"), "inst")
        entry = registry.known()[0]
        self.assertEqual(entry["name"], "Anna B")
        self.assertEqual(entry["role"], "admin")

    def test_first_seen_survives_later_visits(self):
        registry.record(Identity(sub="sub-anna"), "inst")
        first = registry.known()[0]["first_seen"]
        registry._recent.clear()
        time.sleep(0.01)
        registry.record(Identity(sub="sub-anna", name="Anna"), "other")
        entry = registry.known()[0]
        self.assertEqual(entry["first_seen"], first)
        self.assertEqual(entry["last_instance"], "other")

    def test_the_source_is_kept_so_unsigned_users_stay_recognisable(self):
        registry.record(Identity(sub="sub-anna", source=SOURCE_HEADERS), "inst")
        self.assertEqual(registry.known()[0]["source"], SOURCE_HEADERS)

    def test_the_most_recent_caller_comes_first(self):
        registry.record(Identity(sub="sub-old"), "inst")
        registry._recent.clear()
        time.sleep(1.01)          # the column has second resolution
        registry.record(Identity(sub="sub-new"), "inst")
        self.assertEqual([e["sub"] for e in registry.known()], ["sub-new", "sub-old"])

    def test_forgetting_removes_one_person(self):
        registry.record(Identity(sub="sub-anna"), "inst")
        registry.record(Identity(sub="sub-ben"), "inst")
        self.assertTrue(registry.forget("sub-anna"))
        self.assertEqual([e["sub"] for e in registry.known()], ["sub-ben"])
        self.assertFalse(registry.forget("sub-anna"))

    def test_a_forgotten_person_can_come_back(self):
        """Forgetting is not revoking — the rules live in the policy, and a
        roster that could not refill itself would be a trap."""
        registry.record(Identity(sub="sub-anna"), "inst")
        registry.forget("sub-anna")
        registry.record(Identity(sub="sub-anna"), "inst")
        self.assertEqual(["sub-anna"], [e["sub"] for e in registry.known()])

    def test_an_identity_without_a_sub_is_not_recorded(self):
        registry.record(Identity(sub=""), "inst")
        registry.record(None, "inst")
        self.assertEqual([], registry.known())

    def test_no_token_is_stored(self):
        registry.record(Identity(sub="sub-anna", raw_token="a.b.c"), "inst")
        self.assertNotIn("a.b.c", str(registry.known()))

    def test_reading_an_empty_installation_creates_no_database(self):
        """Nobody has called yet, so there is no roster. The manager asking
        about it must not leave an empty file behind — in `runtime/`, in a
        backup, and in every test run that happens to sweep the route."""
        self.assertEqual([], registry.known())
        self.assertFalse(registry.forget("nobody"))
        self.assertFalse(registry.DB_PATH.exists())

    def test_an_unwritable_database_never_breaks_a_call(self):
        """Bookkeeping must not be able to fail a tool call: the user is
        verified either way, and the only loss is a dropdown entry."""
        registry.close()
        with patch.object(registry, "DB_PATH", pathlib.Path("/nonexistent-dir/x/identities.db")):
            registry.record(Identity(sub="sub-anna"), "inst")   # must not raise
            self.assertEqual([], registry.known())


if __name__ == "__main__":
    unittest.main()
