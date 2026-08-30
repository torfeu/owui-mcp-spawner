"""Named agent tokens: the store, the lookup, and the two delivery paths.

The line these tests exist for is the one in the module docstring: a token is
shown once and stored hashed. Everything else follows from it — there is no
route that can hand it out again, a backup of the store gives nothing away, and
a lost token is replaced rather than recovered.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from app import agent_identity
from app.agent_identity import AgentIdentityError
from app.identity import SOURCE_AGENT


class StoreTestCase(unittest.TestCase):
    """Every test gets its own store file. The env var points the module at it,
    which is the same switch an installation uses — no monkey-patching of the
    path, so the redirection itself is covered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "agent_identities.json"
        self.env = patch.dict(os.environ, {"MCP_AGENT_IDENTITIES_FILE": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("MCP_AGENT_IDENTITIES", None)
        agent_identity._file_cache.clear()
        self.addCleanup(agent_identity._file_cache.clear)


class CreateTests(StoreTestCase):
    def test_a_new_identity_returns_its_token_once_and_stores_only_a_hash(self):
        record, token = agent_identity.create("claude-code", "Claude Code", "agent")
        self.assertTrue(token.startswith(agent_identity.TOKEN_PREFIX))
        self.assertEqual(record["sub"], "claude-code")
        self.assertNotIn("token_hash", record)

        # The one thing a backup must not contain.
        raw = self.path.read_text()
        self.assertNotIn(token, raw)
        self.assertIn(agent_identity.hash_token(token), raw)

    def test_the_token_identifies_its_agent(self):
        _, token = agent_identity.create("claude-code", "Claude Code", "agent")
        who = agent_identity.identify(token)
        self.assertEqual(who.sub, "claude-code")
        self.assertEqual(who.name, "Claude Code")
        self.assertEqual(who.role, "agent")
        self.assertEqual(who.source, SOURCE_AGENT)

    def test_an_unknown_or_empty_token_identifies_nobody(self):
        agent_identity.create("claude-code")
        for label, token in (("unknown", "mcpa_nope"), ("empty", ""), ("none", None)):
            with self.subTest(token=label):
                self.assertIsNone(agent_identity.identify(token))

    def test_two_agents_get_two_tokens(self):
        """The reason for 1:1 rather than one token per role — they have to be
        revocable one at a time."""
        _, first = agent_identity.create("claude-code")
        _, second = agent_identity.create("codex")
        self.assertNotEqual(first, second)
        self.assertEqual(agent_identity.identify(first).sub, "claude-code")
        self.assertEqual(agent_identity.identify(second).sub, "codex")

    def test_a_duplicate_id_is_refused(self):
        agent_identity.create("claude-code")
        with self.assertRaises(AgentIdentityError):
            agent_identity.create("claude-code")

    def test_the_id_has_to_stay_url_and_policy_safe(self):
        for bad in ("", "   ", "has space", "sl/ash", "ü" * 3, "x" * 65):
            with self.subTest(sub=bad):
                with self.assertRaises(AgentIdentityError):
                    agent_identity.create(bad)

    def test_the_token_is_not_guessable_from_the_stored_record(self):
        """A plain SHA-256 is only enough because the token is 256 bits of
        `secrets` output — this pins the entropy, not the algorithm."""
        _, token = agent_identity.create("claude-code")
        body = token[len(agent_identity.TOKEN_PREFIX):]
        self.assertGreaterEqual(len(body), 40)
        self.assertNotEqual(token, agent_identity.new_token())


class LifecycleTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        _, self.token = agent_identity.create("claude-code", "Claude Code", "agent")

    def test_regenerating_kills_the_old_token(self):
        fresh = agent_identity.regenerate("claude-code")
        self.assertNotEqual(fresh, self.token)
        self.assertIsNone(agent_identity.identify(self.token))
        self.assertEqual(agent_identity.identify(fresh).sub, "claude-code")

    def test_regenerating_an_unknown_agent_is_an_error(self):
        with self.assertRaises(AgentIdentityError):
            agent_identity.regenerate("nobody")

    def test_revoking_takes_the_token_out(self):
        self.assertTrue(agent_identity.delete("claude-code"))
        self.assertIsNone(agent_identity.identify(self.token))
        self.assertEqual(agent_identity.public_list(), [])

    def test_revoking_an_unknown_agent_reports_nothing_removed(self):
        self.assertFalse(agent_identity.delete("nobody"))

    def test_renaming_leaves_the_token_alone(self):
        """The id is the policy key, so it never changes; the label may."""
        record = agent_identity.update("claude-code", name="Claude Code (laptop)")
        self.assertEqual(record["name"], "Claude Code (laptop)")
        self.assertEqual(agent_identity.identify(self.token).sub, "claude-code")
        self.assertEqual(agent_identity.identify(self.token).name, "Claude Code (laptop)")

    def test_the_public_list_never_carries_a_hash(self):
        for entry in agent_identity.public_list():
            self.assertNotIn("token_hash", entry)


class DeliveryPathTests(StoreTestCase):
    """Env or file, and a set env var wins — the same order
    auth.configure_api_tokens() uses for the API tokens."""

    def test_the_file_is_the_default_and_is_readable_by_nobody_else(self):
        agent_identity.create("claude-code")
        self.assertEqual(agent_identity.source(), agent_identity.SOURCE_FILE)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_change_on_disk_is_picked_up_without_a_restart(self):
        """The whole reason the file path exists next to the env one.

        Written here the way the manager writes it *from another process*, and
        read back without touching the cache — otherwise this would only prove
        that `_save()` clears its own cache, which is not the case that matters:
        the reader is a runner that never called `_save()` at all.
        """
        old_token, new_token = "mcpa_the-old-one", "mcpa_the-new-one"
        self.path.write_text(json.dumps({"agent_identities": [
            {"sub": "claude-code", "token_hash": agent_identity.hash_token(old_token)}]}))
        self.assertEqual(agent_identity.identify(old_token).sub, "claude-code")

        self.path.write_text(json.dumps({"agent_identities": [
            {"sub": "claude-code", "token_hash": agent_identity.hash_token(new_token)}]}))
        self.assertIsNone(agent_identity.identify(old_token))
        self.assertEqual(agent_identity.identify(new_token).sub, "claude-code")

    def test_the_environment_wins_over_the_file(self):
        _, file_token = agent_identity.create("from-file")
        env_token = "mcpa_env-token-value"
        with patch.dict(os.environ, {agent_identity.ENV_IDENTITIES: json.dumps(
                [{"sub": "from-env", "token": env_token}])}):
            self.assertEqual(agent_identity.source(), agent_identity.SOURCE_ENV)
            self.assertEqual(agent_identity.identify(env_token).sub, "from-env")
            self.assertIsNone(agent_identity.identify(file_token))

    def test_the_environment_accepts_a_hash_as_well_as_a_plain_token(self):
        token = "mcpa_hashed-in-the-env"
        with patch.dict(os.environ, {agent_identity.ENV_IDENTITIES: json.dumps(
                [{"sub": "from-env", "token_hash": agent_identity.hash_token(token)}])}):
            self.assertEqual(agent_identity.identify(token).sub, "from-env")

    def test_a_broken_environment_variable_grants_nobody_and_stays_the_source(self):
        """Falling back to the file here would re-open a door the (broken)
        variable was meant to define."""
        _, file_token = agent_identity.create("from-file")
        with patch.dict(os.environ, {agent_identity.ENV_IDENTITIES: "{not json"}):
            self.assertEqual(agent_identity.source(), agent_identity.SOURCE_ENV)
            self.assertEqual(agent_identity.load(), [])
            self.assertIsNone(agent_identity.identify(file_token))

    def test_writes_are_refused_while_the_environment_is_in_charge(self):
        """A write that cannot take effect must not answer ok — the lesson from
        the invented `lifecycle` valve."""
        agent_identity.create("from-file")
        with patch.dict(os.environ, {agent_identity.ENV_IDENTITIES: json.dumps(
                [{"sub": "from-env", "token": "mcpa_x"}])}):
            self.assertTrue(agent_identity.env_active())
            for label, call in (
                ("create", lambda: agent_identity.create("new-one")),
                ("regenerate", lambda: agent_identity.regenerate("from-file")),
                ("update", lambda: agent_identity.update("from-file", name="x")),
                ("delete", lambda: agent_identity.delete("from-file")),
            ):
                with self.subTest(call=label):
                    with self.assertRaises(AgentIdentityError):
                        call()


class BadInputTests(StoreTestCase):
    def test_one_broken_entry_does_not_take_the_others_down(self):
        token = "mcpa_the-good-one"
        self.path.write_text(json.dumps({"agent_identities": [
            "not a dict",
            {"name": "no sub at all"},
            {"sub": "no-token"},
            {"sub": "good", "token_hash": agent_identity.hash_token(token)},
        ]}))
        self.assertEqual([r["sub"] for r in agent_identity.load()], ["good"])
        self.assertEqual(agent_identity.identify(token).sub, "good")

    def test_an_unreadable_file_grants_nobody(self):
        self.path.write_text("{ this is not json")
        self.assertEqual(agent_identity.load(), [])
        self.assertFalse(agent_identity.configured())

    def test_a_missing_file_is_simply_no_identities(self):
        self.assertEqual(agent_identity.load(), [])
        self.assertFalse(agent_identity.configured())
        self.assertIsNone(agent_identity.identify("mcpa_anything"))

    def test_a_duplicate_id_in_the_file_is_taken_once(self):
        self.path.write_text(json.dumps({"agent_identities": [
            {"sub": "twice", "token": "mcpa_first"},
            {"sub": "twice", "token": "mcpa_second"},
        ]}))
        self.assertEqual(len(agent_identity.load()), 1)
        self.assertEqual(agent_identity.identify("mcpa_first").sub, "twice")
        self.assertIsNone(agent_identity.identify("mcpa_second"))


class BearerTests(unittest.TestCase):
    """Reading the credential off a request. RFC 7235 makes the scheme
    case-insensitive, and ASGI, Starlette and httpx each capitalise the header
    their own way — getting this wrong would look like a wrong token."""

    def test_the_scheme_and_the_header_name_are_both_case_insensitive(self):
        for headers in (
            {"Authorization": "Bearer abc"},
            {"authorization": "bearer abc"},
            {"AUTHORIZATION": "BEARER abc"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(agent_identity.bearer_token(headers), "abc")

    def test_anything_that_is_not_a_bearer_credential_is_empty(self):
        for headers in ({}, None, {"Authorization": "Basic abc"}, {"X-Other": "Bearer abc"}):
            with self.subTest(headers=headers):
                self.assertEqual(agent_identity.bearer_token(headers), "")


if __name__ == "__main__":
    unittest.main()
