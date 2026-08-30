"""The API behind the *Agent Identities* section.

Two promises the routes have to keep. The token appears in exactly one
response, the one that created it — there is no route that could produce it a
second time, because only the hash is kept. And a write that cannot take
effect is refused rather than answered with `ok`: with the environment variable
in charge, the file this API writes is never read.

Every test points the store at a temp file. The suite runs on the server too,
and a stray identity in the live store would be a working credential nobody
issued on purpose.
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
from app import agent_identity
from app.admin_server import app


class AgentIdentityApiTestCase(unittest.TestCase):
    PASSWORD = "admin-password"
    AGENT = "agent-token-abcdef"

    def setUp(self):
        self.client = TestClient(app)
        # Set explicitly: a route test that assumes no password is set is green
        # here and red on the server, where one is.
        self.original_hash = auth._password_hash
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", self.original_hash))

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.store = root / "agent_identities.json"

        self.env = patch.dict(os.environ, {
            "MCP_AGENT_IDENTITIES_FILE": str(self.store),
            "MCP_MANAGER_AGENT_TOKEN": self.AGENT,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(agent_identity.ENV_IDENTITIES, None)
        agent_identity._file_cache.clear()
        self.addCleanup(agent_identity._file_cache.clear)

    @property
    def admin(self):
        return {"Authorization": f"Bearer {self.PASSWORD}"}

    @property
    def agent(self):
        return {"Authorization": f"Bearer {self.AGENT}"}

    def create(self, sub="claude-code", **body):
        response = self.client.post(
            "/api/agent-identities", json={"sub": sub, **body}, headers=self.admin)
        self.assertEqual(200, response.status_code, response.text)
        return response.json()


class IssueTests(AgentIdentityApiTestCase):
    def test_the_token_comes_back_once_and_never_again(self):
        created = self.create("claude-code", name="Claude Code", role="agent")
        token = created["token"]
        self.assertTrue(token.startswith(agent_identity.TOKEN_PREFIX))
        self.assertIn("cannot be shown again", created["note"])

        listed = self.client.get("/api/agent-identities", headers=self.admin).json()
        self.assertEqual([e["sub"] for e in listed["identities"]], ["claude-code"])
        # Not the token, and not the hash either.
        self.assertNotIn(token, json.dumps(listed))
        self.assertNotIn("token_hash", json.dumps(listed))

    def test_the_issued_token_actually_works(self):
        """The routes and the lookup have to agree about the hash — a store
        the runner cannot read would fail silently, as a 401 nobody explains."""
        token = self.create("claude-code")["token"]
        self.assertEqual(agent_identity.identify(token).sub, "claude-code")

    def test_a_duplicate_or_malformed_id_is_refused(self):
        self.create("claude-code")
        for label, sub in (("duplicate", "claude-code"), ("spaces", "has space"),
                           ("slash", "sl/ash"), ("empty", "")):
            with self.subTest(sub=label):
                response = self.client.post(
                    "/api/agent-identities", json={"sub": sub}, headers=self.admin)
                self.assertEqual(422, response.status_code, response.text)

    def test_regenerating_replaces_the_token(self):
        first = self.create("claude-code")["token"]
        response = self.client.post(
            "/api/agent-identities/claude-code/token", headers=self.admin)
        self.assertEqual(200, response.status_code, response.text)
        second = response.json()["token"]
        self.assertNotEqual(first, second)
        self.assertIsNone(agent_identity.identify(first))
        self.assertEqual(agent_identity.identify(second).sub, "claude-code")

    def test_renaming_keeps_the_id_and_the_token(self):
        token = self.create("claude-code", name="Claude Code")["token"]
        response = self.client.put(
            "/api/agent-identities/claude-code", json={"name": "Claude Code (laptop)"},
            headers=self.admin)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(response.json()["identity"]["name"], "Claude Code (laptop)")
        self.assertEqual(agent_identity.identify(token).sub, "claude-code")

    def test_revoking_kills_the_token_and_an_unknown_one_is_a_404(self):
        token = self.create("claude-code")["token"]
        self.assertEqual(
            200, self.client.delete("/api/agent-identities/claude-code",
                                    headers=self.admin).status_code)
        self.assertIsNone(agent_identity.identify(token))
        self.assertEqual(
            404, self.client.delete("/api/agent-identities/claude-code",
                                    headers=self.admin).status_code)


class DoorTests(AgentIdentityApiTestCase):
    """Who may issue a credential. Only the password — an agent that could mint
    an identity for itself would be an admin with extra steps."""

    def test_the_agent_token_may_look_but_not_issue(self):
        self.create("claude-code")
        self.assertEqual(
            200, self.client.get("/api/agent-identities", headers=self.agent).status_code)
        for method, path, body in (
            ("post", "/api/agent-identities", {"sub": "sneaky"}),
            ("post", "/api/agent-identities/claude-code/token", None),
            ("put", "/api/agent-identities/claude-code", {"name": "x"}),
            ("delete", "/api/agent-identities/claude-code", None),
        ):
            with self.subTest(route=f"{method.upper()} {path}"):
                call = getattr(self.client, method)
                response = call(path, headers=self.agent, **({"json": body} if body else {}))
                self.assertEqual(403, response.status_code, response.text)
        # …and nothing moved.
        self.assertEqual([e["sub"] for e in agent_identity.public_list()], ["claude-code"])

    def test_no_credential_at_all_is_a_401(self):
        self.assertEqual(401, self.client.get("/api/agent-identities").status_code)
        self.assertEqual(
            401, self.client.post("/api/agent-identities", json={"sub": "x"}).status_code)

    def test_no_token_edit_closes_this_door_like_the_others(self):
        """Driven through the real flag rather than a patched dependency: the
        dependency list is captured when the module is imported, so a patch
        there passes while the running server ignores it."""
        with patch.dict(os.environ, {"MCP_NO_TOKEN_EDIT": "1"}):
            response = self.client.post(
                "/api/agent-identities", json={"sub": "x"}, headers=self.admin)
            self.assertEqual(403, response.status_code, response.text)
            self.assertEqual(
                403, self.client.delete("/api/agent-identities/x",
                                        headers=self.admin).status_code)
            # Reading the roster is not editing a token.
            self.assertEqual(
                200, self.client.get("/api/agent-identities", headers=self.admin).status_code)
        self.assertEqual(agent_identity.public_list(), [])


class DeliveryPathTests(AgentIdentityApiTestCase):
    def test_the_file_is_the_source_by_default_and_is_editable(self):
        body = self.client.get("/api/agent-identities", headers=self.admin).json()
        self.assertEqual(body["source"], agent_identity.SOURCE_FILE)
        self.assertTrue(body["editable"])
        self.assertEqual(body["path"], str(self.store))

    def test_with_the_environment_in_charge_the_dialog_says_so_and_writes_are_refused(self):
        """Otherwise "I changed the token and nothing happened" is the first
        support question — and the write would have answered ok."""
        self.create("from-file")
        with patch.dict(os.environ, {agent_identity.ENV_IDENTITIES: json.dumps(
                [{"sub": "from-env", "token": "mcpa_env-token"}])}):
            body = self.client.get("/api/agent-identities", headers=self.admin).json()
            self.assertEqual(body["source"], agent_identity.SOURCE_ENV)
            self.assertFalse(body["editable"])
            self.assertEqual([e["sub"] for e in body["identities"]], ["from-env"])
            self.assertEqual(body["env_var"], agent_identity.ENV_IDENTITIES)

            for method, path, sent in (
                ("post", "/api/agent-identities", {"sub": "new-one"}),
                ("post", "/api/agent-identities/from-file/token", None),
                ("put", "/api/agent-identities/from-file", {"name": "x"}),
                ("delete", "/api/agent-identities/from-file", None),
            ):
                with self.subTest(route=f"{method.upper()} {path}"):
                    call = getattr(self.client, method)
                    response = call(path, headers=self.admin,
                                    **({"json": sent} if sent else {}))
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn(agent_identity.ENV_IDENTITIES, response.text)


class RosterTests(AgentIdentityApiTestCase):
    """A fresh agent has to be assignable *before* its first call — that is the
    moment you want to give it its rules, not an hour later."""

    def setUp(self):
        super().setUp()
        root = pathlib.Path(self.tmp.name)
        registry.close()
        self.db = patch.object(registry, "DB_PATH", root / "identities.db")
        self.db.start()
        self.addCleanup(self.db.stop)
        self.addCleanup(registry.close)
        self.policy_file = root / "policy.json"
        self.policy_env = patch.dict(os.environ, {"MCP_IDENTITY_POLICY": str(self.policy_file)})
        self.policy_env.start()
        self.addCleanup(self.policy_env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))

    def _roster(self):
        response = self.client.get("/api/identities", headers=self.admin)
        self.assertEqual(200, response.status_code, response.text)
        return {row["sub"]: row for row in response.json()["identities"]}

    def test_a_new_agent_appears_in_the_rights_dialog_before_it_has_called(self):
        self.create("claude-code", name="Claude Code", role="agent")
        row = self._roster()["claude-code"]
        self.assertTrue(row["agent"])
        self.assertTrue(row["never_seen"])
        self.assertFalse(row["has_rules"])
        self.assertEqual(row["name"], "Claude Code")

    def test_an_agent_that_has_rules_is_marked_as_having_them(self):
        self.create("claude-code")
        self.policy_file.write_text(json.dumps(
            {"users": {"claude-code": {"instances": {"inst": ["read_item"]}}}}))
        policy._cache = policy._cache_key = None
        row = self._roster()["claude-code"]
        self.assertTrue(row["has_rules"])
        self.assertTrue(row["agent"])
        # Listed once, not once per source.
        self.assertEqual(
            1, sum(1 for r in self.client.get("/api/identities", headers=self.admin)
                   .json()["identities"] if r["sub"] == "claude-code"))

    def test_a_caller_that_is_not_an_agent_is_not_marked_as_one(self):
        from app.identity import Identity
        registry.record(Identity(sub="sub-anna", name="Anna"), "inst")
        self.assertFalse(self._roster()["sub-anna"]["agent"])

    def test_agents_alone_count_as_a_configured_identity(self):
        """An installation with agents but no OpenWebUI is a real setup — the
        dialog must not tell it that identity is switched off."""
        before = self.client.get("/api/identities", headers=self.admin).json()
        self.assertFalse(before["identity_configured"])
        self.create("claude-code")
        after = self.client.get("/api/identities", headers=self.admin).json()
        self.assertTrue(after["identity_configured"])


if __name__ == "__main__":
    unittest.main()
