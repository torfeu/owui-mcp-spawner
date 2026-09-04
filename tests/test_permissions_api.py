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


class ResetIdentityTests(PermissionsApiTests):
    """The stop button: out of the list, out of the rules, out of the tokens.

    Its reason for existing is somebody misbehaving right now, so it asks a
    question and not a password — and it is written so that it *cannot* do the
    thing the password guards. It deletes a key; it never writes a policy a
    caller handed it. `PUT /api/policy` keeps the password because granting
    rights hands somebody another account's credentials.
    """

    def setUp(self):
        super().setUp()
        from tests.test_agent_identity import StoreTestCase  # noqa: F401 — path only
        self.agent_file = pathlib.Path(self.tmp.name) / "agent_identities.json"
        self.agent_env = patch.dict(os.environ,
                                    {"MCP_AGENT_IDENTITIES_FILE": str(self.agent_file)})
        self.agent_env.start()
        self.addCleanup(self.agent_env.stop)
        os.environ.pop("MCP_AGENT_IDENTITIES", None)
        from app import agent_identity
        agent_identity._file_cache.clear()
        self.addCleanup(agent_identity._file_cache.clear)
        self.agent_identity = agent_identity

    def _grant(self, sub):
        response = self.client.put("/api/policy", headers=self.auth_header, json={
            "policy": {"users": {sub: {"instances": {"inst": "*"}}}}})
        self.assertEqual(200, response.status_code, response.text)

    def _policy_users(self):
        return list((policy.load_policy(force=True).get("users") or {}))

    def test_forgetting_leaves_the_rules_where_they_are(self):
        registry.record(ANNA, "inst")
        self._grant("sub-anna")
        body = self.client.delete("/api/identities/sub-anna",
                                  headers=self.auth_header).json()
        self.assertTrue(body["forgotten"])
        self.assertEqual(["sub-anna"], self._policy_users())

    def test_reset_takes_the_rules_with_it(self):
        registry.record(ANNA, "inst")
        self._grant("sub-anna")
        body = self.client.delete("/api/identities/sub-anna/reset",
                                  headers=self.auth_header).json()
        self.assertTrue(body["forgotten"])
        self.assertTrue(body["rules_removed"])
        self.assertEqual([], self._policy_users())

    def test_reset_of_an_agent_takes_its_token_too(self):
        # A record without a token is not an agent any more, and a token
        # without a record is a key without a lock.
        self.agent_identity.create("claude", "Claude", "KI")
        registry.record(Identity(sub="claude", name="Claude"), "inst")
        self._grant("claude")
        body = self.client.delete("/api/identities/claude/reset",
                                  headers=self.auth_header).json()
        self.assertTrue(body["token_removed"])
        self.assertEqual([], [r["sub"] for r in self.agent_identity.public_list()])
        self.assertEqual([], self._policy_users())

    def test_a_person_is_not_mistaken_for_an_agent(self):
        registry.record(ANNA, "inst")
        body = self.client.delete("/api/identities/sub-anna/reset",
                                  headers=self.auth_header).json()
        self.assertFalse(body["token_removed"])

    def test_the_route_cannot_grant_anything(self):
        # The whole reason it may run without the password. Every other user's
        # rules survive untouched, and no rule can appear that was not there.
        registry.record(ANNA, "inst")
        self.client.put("/api/policy", headers=self.auth_header, json={"policy": {"users": {
            "sub-anna": {"instances": {"inst": "*"}},
            "sub-ben": {"instances": {"other": ["one"]}}}}})
        self.client.delete("/api/identities/sub-anna/reset", headers=self.auth_header)
        after = policy.load_policy(force=True)
        self.assertEqual(["sub-ben"], list(after["users"]))
        self.assertEqual({"instances": {"other": ["one"]}}, after["users"]["sub-ben"])

    def test_reset_needs_the_password_like_every_other_access_change(self):
        # One line for everything that changes who may do what: no second,
        # softer door into the room that PUT /api/policy guards.
        registry.record(ANNA, "inst")
        self._grant("sub-anna")
        with patch.dict(os.environ, {"MCP_MANAGER_AGENT_TOKEN": "agent-token"}):
            refused = self.client.delete("/api/identities/sub-anna/reset",
                                         headers={"Authorization": "Bearer agent-token"})
        # 403, not 401: the agent token is a valid credential, it is simply
        # not the one an access change costs.
        self.assertEqual(403, refused.status_code)
        self.assertEqual(["sub-anna"], self._policy_users())

    def test_forgetting_needs_no_password_because_it_takes_nothing_away(self):
        # The agent token may write, but it may not change who is allowed what.
        # Tidying the list is not that, so it goes through.
        registry.record(ANNA, "inst")
        self._grant("sub-anna")
        with patch.dict(os.environ, {"MCP_MANAGER_AGENT_TOKEN": "agent-token"}):
            response = self.client.delete("/api/identities/sub-anna",
                                          headers={"Authorization": "Bearer agent-token"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["sub-anna"], self._policy_users())

    def test_resetting_somebody_who_was_never_there_is_not_an_error(self):
        body = self.client.delete("/api/identities/nobody/reset",
                                  headers=self.auth_header).json()
        self.assertFalse(body["forgotten"])
        self.assertFalse(body["rules_removed"])
        self.assertFalse(body["token_removed"])
