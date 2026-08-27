"""The API behind the *Users & permissions* dialog.

Two halves: the roster of people the runners have seen, and the rules that
name them. What the routes must get right is mostly about *not* hiding things
— a rule for a user who never called looks exactly like a typo in a user id,
and a policy file that cannot be read must say so in the dialog rather than
only in a log, because while it is broken every rule denies.
"""
import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.identity_registry as registry
import app.policy as policy
from app.admin_server import app
from app.identity import Identity

ANNA = Identity(sub="sub-anna", email="anna@example.org", name="Anna", role="user")


class PermissionsApiTests(unittest.TestCase):
    PASSWORD = "admin-password"

    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", self.original_hash))

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)

        registry.close()
        self.db = patch.object(registry, "DB_PATH", root / "identities.db")
        self.db.start()
        self.addCleanup(self.db.stop)
        self.addCleanup(registry.close)

        self.policy_file = root / "policy.json"
        self.env = patch.dict(os.environ, {"MCP_IDENTITY_POLICY": str(self.policy_file)})
        self.env.start()
        self.addCleanup(self.env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))

    @property
    def auth_header(self):
        return {"Authorization": f"Bearer {self.PASSWORD}"}

    def get(self, path):
        response = self.client.get(path, headers=self.auth_header)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_the_roster_marks_who_already_has_rules(self):
        registry.record(ANNA, "inst")
        registry.record(Identity(sub="sub-ben", name="Ben"), "inst")
        self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {"sub-anna": {"instances": {"inst": "*"}}}}})

        entries = {e["sub"]: e for e in self.get("/api/identities")["identities"]}
        self.assertTrue(entries["sub-anna"]["has_rules"])
        self.assertFalse(entries["sub-ben"]["has_rules"])

    def test_a_rule_for_someone_who_never_called_is_shown_and_marked(self):
        """The likeliest cause is a mistyped user id — hiding the entry would
        hide the mistake, and the rule would silently never apply."""
        self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {"typo-sub": {"instances": {"inst": "*"}}}}})
        entries = {e["sub"]: e for e in self.get("/api/identities")["identities"]}
        self.assertTrue(entries["typo-sub"]["never_seen"])
        self.assertTrue(entries["typo-sub"]["has_rules"])

    def test_a_broken_policy_is_reported_instead_of_swallowed(self):
        self.policy_file.write_text("{ not json")
        policy._cache = policy._cache_key = None
        self.assertIn("unusable", self.get("/api/policy")["error"])
        self.assertIn("unusable", self.get("/api/identities")["policy_error"])

    def test_saving_rules_takes_effect_without_a_restart(self):
        response = self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {"sub-anna": {"instances": {"inst": ["read_item"]}}}}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))
        self.assertFalse(policy.is_tool_allowed(ANNA, "inst", "delete_item"))

    def test_a_policy_carrying_a_secret_is_refused_with_a_reason(self):
        response = self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {"sub-anna": {"app_password": "hunter2"}}}})
        self.assertEqual(422, response.status_code)
        self.assertIn("credentials_file", response.json()["detail"])

    def test_the_preview_answers_what_a_user_would_get(self):
        """A rules file has enough moving parts — role, personal entry, deny —
        that "read it and see" is not an answer."""
        self.client.put("/api/policy", headers=self.auth_header, json={"policy": {
            "roles": {"admin": {"instances": {"laws": "*"}}},
            "users": {"sub-anna": {"account": "anna", "instances": {"inst": ["read_item"]}}},
        }})
        anna = self.client.post("/api/policy/preview", headers=self.auth_header,
                                json={"sub": "sub-anna", "role": "admin"}).json()
        self.assertTrue(anna["matched"])
        self.assertEqual("anna", anna["account"])
        self.assertEqual({"laws": "*", "inst": ["read_item"]}, anna["instances"])

        stranger = self.client.post("/api/policy/preview", headers=self.auth_header,
                                    json={"sub": "nobody", "role": "user"}).json()
        self.assertFalse(stranger["matched"])

    def test_forgetting_a_user_leaves_their_rules_alone(self):
        """Tidying the list must not silently revoke access — the two are
        different actions and only one of them is destructive."""
        registry.record(ANNA, "inst")
        self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {"sub-anna": {"instances": {"inst": "*"}}}}})

        response = self.client.delete("/api/identities/sub-anna", headers=self.auth_header)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()["forgotten"])
        self.assertTrue(policy.is_tool_allowed(ANNA, "inst", "read_item"))
        # …and the entry is still listed, now as never-seen
        entries = {e["sub"]: e for e in self.get("/api/identities")["identities"]}
        self.assertTrue(entries["sub-anna"]["never_seen"])

    def test_a_guest_reaches_neither_the_roster_nor_the_rules(self):
        """The 🔑 button is hidden in the guest view, but a hidden button is
        not a boundary. Unlike /api/instances, which deliberately answers
        anonymous callers with a reduced payload, these two say nothing at all:
        who uses this installation, and who may do what, is not public.
        """
        registry.record(ANNA, "inst")
        for path in ("/api/identities", "/api/policy"):
            with self.subTest(route=path):
                self.assertEqual(401, self.client.get(path).status_code)
        self.assertEqual(401, self.client.put("/api/policy", json={"policy": {}}).status_code)
        self.assertEqual(401, self.client.delete("/api/identities/sub-anna").status_code)

    def test_no_credential_ever_passes_through_the_policy_api(self):
        secret_file = pathlib.Path(self.tmp.name) / "anna"
        secret_file.write_text("anna-secret")
        self.client.put("/api/policy", headers=self.auth_header, json={"policy": {"users": {
            "sub-anna": {"account": "anna", "credentials_file": str(secret_file)}}}})
        for path in ("/api/policy", "/api/identities"):
            with self.subTest(route=path):
                self.assertNotIn("anna-secret", json.dumps(self.get(path)))


if __name__ == "__main__":
    unittest.main()
