"""The external MCP bridge as it ships in examples/.

It was built on the live server (29.09.) and brought into the repository so
the store can install it. Three things matter about the shipped copy: that its
specs are generated from its code like every other shipped tool, that it
carries nothing of the installation it came from (the Wikidata target lives in
that instance's values, not in the code), and the two behaviours it was built
for — the credential it attaches, and tolerating flat arguments.
"""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.tool_editor import validate_tool_code

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "examples" / "external-mcp-bridge.json"


def exported():
    return json.loads(BRIDGE_PATH.read_text())[0]


def load_bridge():
    namespace = {}
    exec(exported()["content"], namespace)
    return namespace["Tools"]()


class ShippedBridgeTests(unittest.TestCase):

    def test_specs_match_the_docstrings_of_the_shipped_code(self):
        result = validate_tool_code(exported()["content"])
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual({t["name"]: t for t in result["tools"]},
                         {s["name"]: s for s in exported()["specs"]})

    def test_the_catch_all_stays_out_of_the_schema(self):
        # **__request__ is named after an injected parameter precisely so the
        # schema builder omits it; a model must see name and arguments only.
        call = next(s for s in exported()["specs"] if s["name"] == "call_tool")
        self.assertEqual(["name", "arguments"], list(call["parameters"]["properties"]))
        self.assertEqual(["name"], call["parameters"]["required"])

    def test_it_is_not_mistaken_for_a_router(self):
        # The router skips any instance offering exactly its own three tools;
        # the bridge's names are chosen so that it is routed, not skipped.
        names = {s["name"] for s in exported()["specs"]}
        self.assertNotEqual({"find_tools", "describe_tools", "call_tool"}, names)

    def test_nothing_of_the_installation_it_came_from(self):
        entry = exported()
        text = json.dumps(entry).lower()
        for marker in ("wmcloud", "wikidata", "192.168."):
            self.assertNotIn(marker, text, marker)
        valves = load_bridge().valves
        self.assertEqual("", valves.target_url)
        self.assertEqual("", valves.auth_token)
        self.assertIn("category: System", entry["content"])   # sorts it next to the router

    def test_the_store_lists_it(self):
        import app.api_helpers as api_helpers
        self.assertIn("external_mcp_bridge", api_helpers._examples_by_id())


class BridgeBehaviourTests(unittest.TestCase):

    def test_the_credential_goes_where_the_values_say(self):
        bridge = load_bridge()
        bridge.valves.auth_token = "secret"
        self.assertEqual({"Authorization": "Bearer secret"}, bridge._headers())
        bridge.valves.auth_header, bridge.valves.auth_scheme = "X-Api-Key", ""
        bridge.valves.extra_headers = "X-Extra: 1\nnot a header"
        self.assertEqual({"X-Api-Key": "secret", "X-Extra": "1"}, bridge._headers())

    def test_flat_arguments_reach_the_remote_tool_like_nested_ones(self):
        """A model calling through the router often forgets the second
        nesting level. Both shapes must arrive the same way."""
        bridge = load_bridge()
        bridge.valves.target_url = "https://example.invalid/mcp"
        seen = []

        class Session:
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="search", description="")])

            async def call_tool(self, name, arguments):
                seen.append(arguments)
                return SimpleNamespace(content=[SimpleNamespace(text="ok")], is_error=False)

        @asynccontextmanager
        async def session():
            yield Session()

        with patch.object(bridge, "_session", session):
            nested = asyncio.run(bridge.call_tool(name="search", arguments={"query": "Einstein"}))
            flat = asyncio.run(bridge.call_tool(name="search", query="Einstein"))
        self.assertEqual(("ok", "ok"), (nested, flat))
        self.assertEqual([{"query": "Einstein"}, {"query": "Einstein"}], seen)

    def test_an_unconfigured_bridge_says_so(self):
        answer = asyncio.run(load_bridge().list_tools())
        self.assertIn("target_url is not configured", answer)


if __name__ == "__main__":
    unittest.main()
