"""The shipped diagnostic tool (examples/identity-probe.json).

It exists because setting up per-user identity spans two systems and every
mistake in it is silent: a wrong secret, an instance that was not restarted,
a mode left on `off` all look identical from a chat window. The probe answers
"who arrived here, and what may they do" — and nothing else.

Same house rule as the router and the control tool: the shipped `specs` are
generated from the code, never hand-written. What a model reads must be what
the code does.
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from app.identity import Identity, identity_scope
from app.tool_editor import validate_tool_code

import app.policy as policy

PROBE_PATH = pathlib.Path(__file__).resolve().parents[1] / "examples" / "identity-probe.json"
SECRET = "a-shared-secret-long-enough-to-be-real"


def load_probe():
    namespace = {}
    exec(json.loads(PROBE_PATH.read_text())[0]["content"], namespace)
    return namespace["Tools"]()


class ProbeSpecTests(unittest.TestCase):
    def test_specs_match_the_docstrings_of_the_shipped_code(self):
        exported = json.loads(PROBE_PATH.read_text())[0]
        result = validate_tool_code(exported["content"])

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(
            {t["name"]: t for t in result["tools"]},
            {s["name"]: s for s in exported["specs"]},
        )

    def test_it_offers_exactly_one_tool(self):
        # A diagnostic that grows features is no longer a diagnostic.
        exported = json.loads(PROBE_PATH.read_text())[0]
        self.assertEqual({"whoami"}, {s["name"] for s in exported["specs"]})

    def test_whoami_takes_no_arguments(self):
        exported = json.loads(PROBE_PATH.read_text())[0]
        self.assertEqual({}, exported["specs"][0]["parameters"].get("properties", {}))


class ProbeOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps({"users": {"sub-anna": {
            "account": "anna",
            "credentials_file": str(pathlib.Path(self.tmp.name) / "anna"),
            "instances": {"inst": ["read_item"]},
        }}}))
        (pathlib.Path(self.tmp.name) / "anna").write_text("anna-secret")
        self.env = patch.dict(os.environ, {
            "MCP_USER_JWT_SECRET": SECRET,
            "MCP_IDENTITY_POLICY": str(path),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))
        self.probe = load_probe()

    async def test_it_never_prints_a_secret_or_a_token(self):
        """Its whole value is that the output can be pasted into a chat.

        Covers both branches: the identified one, which touches the policy and
        therefore the account's credentials file, and the anonymous one, which
        reports the shared secret's fingerprint.
        """
        who = Identity(sub="sub-anna", email="anna@example.org", raw_token="tok.en.value")
        with identity_scope(who):
            identified = await self.probe.whoami()
        anonymous = await self.probe.whoami()

        for output in (identified, anonymous):
            self.assertNotIn(SECRET, output)
            self.assertNotIn("anna-secret", output)
            self.assertNotIn("tok.en.value", output)

    async def test_it_reports_the_user_and_what_they_may_run(self):
        with identity_scope(Identity(sub="sub-anna", email="anna@example.org")):
            output = await self.probe.whoami()
        self.assertIn("sub-anna", output)
        self.assertIn("anna", output)          # the account, not its secret
        self.assertIn("read_item", output)

    async def test_it_says_when_an_identity_is_unsigned(self):
        """The distinction the operator has to see: a signed token and a plain
        header both produce a user, but only one of them was verified."""
        from app.identity import SOURCE_HEADERS

        with identity_scope(Identity(sub="sub-anna", source=SOURCE_HEADERS)):
            output = await self.probe.whoami()
        self.assertIn("UNSIGNED", output)

        with identity_scope(Identity(sub="sub-anna")):
            output = await self.probe.whoami()
        self.assertIn("signature", output)
        self.assertNotIn("UNSIGNED", output)

    async def test_every_source_is_named_for_what_it_is(self):
        """Found live on 30.08.: an agent token came back labelled "taken from
        OpenWebUI's plain headers — UNSIGNED", which sends the reader to check
        ENABLE_FORWARD_USER_INFO_HEADERS for a setup OpenWebUI is not part of.
        The machine identity had the same problem since v0.2.0. Four sources
        reach this branch, and only one of them was ever proved."""
        from app.identity import SOURCE_AGENT, SOURCE_HEADERS, SOURCE_MACHINE, SOURCE_TOKEN

        expected = {
            SOURCE_TOKEN: "signature",
            SOURCE_HEADERS: "UNSIGNED",
            SOURCE_AGENT: "agent token",
            SOURCE_MACHINE: "machine identity",
        }
        for source, wanted in expected.items():
            with self.subTest(source=source):
                with identity_scope(Identity(sub="sub-anna", source=source)):
                    output = await self.probe.whoami()
                self.assertIn(wanted, output)
                # The two assigned sources must not borrow the header wording —
                # that is the whole point of telling them apart.
                if source in (SOURCE_AGENT, SOURCE_MACHINE):
                    self.assertIn("assigned, not proven", output)
                    self.assertNotIn("OpenWebUI's plain headers", output)

    async def test_an_unknown_user_is_told_so_plainly(self):
        with identity_scope(Identity(sub="nobody-here")):
            output = await self.probe.whoami()
        self.assertIn("No entry in the access rules", output)


if __name__ == "__main__":
    unittest.main()
