"""The instance note, as the models see it.

The note says what an instance is *for* when its name does not — several
`external_mcp_bridge` copies, each pointing at a different external MCP. A
model choosing between them through the router, or looking at the server
through the control tool, needs that as much as the person hovering the name.

Kept short where a small model reads a whole list (one line), complete where it
asked about one instance.
"""
import asyncio
import time
import unittest
from unittest.mock import Mock

from tests.test_control_tool import load_control_tools
from tests.test_tool_router import load_router

SPEC = {"name": "call_tool", "description": "Call a tool of the external MCP.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}


def bridge(instance_id, note=""):
    return {"id": instance_id, "name": "external_mcp_bridge", "category": "System",
            "description": "Proxy to one external MCP server", "status": "running",
            "note": note, "specs": [dict(SPEC)]}


class RouterNoteTests(unittest.TestCase):

    def router(self, *catalog):
        tools = load_router()
        # A fresh cache: _catalog() answers from it without asking the manager.
        tools._cache, tools._cache_time = list(catalog), time.time()
        return tools

    def test_the_listing_carries_the_first_line_of_the_note(self):
        tools = self.router(bridge("bridge_a", "Wikidata — wd-mcp.wmcloud.org\nno auth"),
                            bridge("bridge_b"))
        listing = asyncio.run(tools.find_tools())
        self.assertIn("  note: Wikidata — wd-mcp.wmcloud.org", listing)
        self.assertNotIn("no auth", listing)            # one line, not all of it
        self.assertEqual(1, listing.count("note: "))    # no empty note line for bridge_b

    def test_the_note_is_what_a_search_can_find(self):
        tools = self.router(bridge("bridge_a", "Wikidata"), bridge("bridge_b", "Nextcloud"))
        listing = asyncio.run(tools.find_tools(query_words=["wikidata"]))
        self.assertIn("1 match(es)", listing)
        self.assertIn("bridge_a.call_tool", listing)
        self.assertNotIn("bridge_b.call_tool", listing)

    def test_describe_carries_the_whole_note_capped(self):
        tools = self.router(bridge("bridge_a", "Wikidata\nno auth, SPARQL only"))
        described = asyncio.run(tools.describe_tools(["bridge_a.call_tool"]))
        self.assertIn("Instance note: Wikidata no auth, SPARQL only", described)

        long_one = self.router(bridge("bridge_a", "x" * 5000))
        described = asyncio.run(long_one.describe_tools(["bridge_a.call_tool"]))
        note_line = next(l for l in described.splitlines() if l.startswith("Instance note:"))
        self.assertLessEqual(len(note_line), len("Instance note: ") + long_one.NOTE_LIMIT)

    def test_an_older_manager_without_notes_changes_nothing(self):
        old = bridge("bridge_a")
        del old["note"]
        tools = self.router(old)
        self.assertNotIn("note", asyncio.run(tools.find_tools()))
        self.assertNotIn("Instance note", asyncio.run(tools.describe_tools(["bridge_a.call_tool"])))


class ControlToolNoteTests(unittest.TestCase):

    def test_the_overview_line_carries_the_first_line_of_the_note(self):
        tools = load_control_tools()
        tools._json_get = Mock(return_value=[
            {"id": "bridge_a", "status": "running", "category": "System", "port": 8601,
             "note": "Wikidata — wd-mcp.wmcloud.org\nsecond line"},
            {"id": "bridge_b", "status": "stopped", "category": "System", "port": 8602},
        ])
        overview = tools.list_instances()
        line_a = next(l for l in overview.splitlines() if "bridge_a" in l)
        line_b = next(l for l in overview.splitlines() if "bridge_b" in l)
        self.assertIn("note: Wikidata — wd-mcp.wmcloud.org", line_a)
        self.assertNotIn("second line", overview)
        self.assertNotIn("note", line_b)


if __name__ == "__main__":
    unittest.main()
