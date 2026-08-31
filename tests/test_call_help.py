"""Answers a small model can act on — at the call boundary and at the valves.

Both halves of Posten 9 share one rule: at each of these places the caller used
to get something true it could do nothing with, and a model that gets no usable
answer invents a cause. Same rule as for tool output, one layer down.

The sharp edge is telling a wrong *call* from a failure *inside* the tool. A
TypeError raised deeper down must reach the caller unchanged — rewriting it
into a parameter complaint would send whoever reads it hunting for a problem
that is not there.
"""
import os
import unittest
from unittest.mock import patch

import app.tool_loader as tool_loader
from app.mcp_runner import _argument_help, _is_argument_error

SCHEMA = {
    "type": "object",
    "properties": {
        "search": {"type": "string"},
        "limit": {"type": "integer"},
        "category": {"type": "string"},
    },
    "required": ["search"],
}


class Tools:
    def find_tools(self, search: str, limit: int = 5, category: str = ""):
        return "ok"

    def takes_anything(self, **kwargs):
        return "ok"

    def fails_inside(self, x: int):
        raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")


class BoundaryTests(unittest.TestCase):
    """Was it the call, or the tool?"""

    def setUp(self):
        self.tools = Tools()

    def test_a_wrong_parameter_name_is_a_call_error(self):
        self.assertTrue(_is_argument_error(self.tools.find_tools, {"query": "x"}))

    def test_a_missing_required_parameter_is_a_call_error(self):
        self.assertTrue(_is_argument_error(self.tools.find_tools, {}))

    def test_a_correct_call_is_not(self):
        self.assertFalse(_is_argument_error(self.tools.find_tools, {"search": "x"}))

    def test_a_tool_taking_kwargs_legitimately_accepts_more_than_its_schema(self):
        # The case the plan warned about: refusing against the schema would
        # turn a working call into an error. Binding is Python's own matching,
        # so it knows about **kwargs and dunder parameters.
        self.assertFalse(_is_argument_error(self.tools.takes_anything, {"__user__": {"id": 1}}))

    def test_a_type_error_raised_inside_the_tool_is_left_alone(self):
        # The trap. The arguments are right; the failure is deeper. Answering
        # "'x' is not a parameter" here would be a lie with a helpful tone.
        self.assertFalse(_is_argument_error(self.tools.fails_inside, {"x": 1}))

    def test_a_method_without_an_introspectable_signature_is_not_guessed_about(self):
        class Opaque:
            def __call__(self, *a, **kw):
                raise TypeError("nope")
        with patch("inspect.signature", side_effect=ValueError("no signature")):
            self.assertFalse(_is_argument_error(Opaque(), {"x": 1}))


class ArgumentHelpTests(unittest.TestCase):
    """The message itself: the right name has to be in it."""

    def test_a_typo_gets_the_parameter_it_was_probably_meant_to_be(self):
        for typo in ("serch", "searh"):
            with self.subTest(typo=typo):
                self.assertIn("did you mean 'search'",
                              _argument_help("find_tools", {typo: "x"}, SCHEMA))

    def test_a_wholly_different_word_gets_named_and_the_right_one_too(self):
        # `query` for `search` is the case from the plan, and difflib rightly
        # sees no resemblance — the two words share one letter. The answer is
        # carried by the other half of the message instead: the required
        # parameter that is missing *is* the one that was meant, and it is
        # named. A cutoff loose enough to pair these two would pair anything.
        message = _argument_help("find_tools", {"query": "x"}, SCHEMA)
        self.assertIn("'query' is not a parameter of 'find_tools'", message)
        self.assertIn("missing required 'search'", message)
        self.assertNotIn("did you mean", message)

    def test_every_parameter_is_listed_with_the_required_ones_marked(self):
        message = _argument_help("find_tools", {"query": "x"}, SCHEMA)
        self.assertIn("search (required)", message)
        self.assertIn("limit", message)
        self.assertIn("category", message)

    def test_a_name_close_to_nothing_gets_the_list_but_no_guess(self):
        message = _argument_help("find_tools", {"zzzzzz": 1}, SCHEMA)
        self.assertIn("'zzzzzz' is not a parameter", message)
        self.assertNotIn("did you mean", message)

    def test_missing_required_parameters_are_named(self):
        message = _argument_help("find_tools", {"limit": 3}, SCHEMA)
        self.assertIn("missing required 'search'", message)

    def test_without_a_schema_there_is_nothing_better_to_say(self):
        # Then the original exception text stands, rather than a made-up
        # message with no names in it.
        self.assertEqual("", _argument_help("find_tools", {"query": "x"}, None))
        self.assertEqual("", _argument_help("find_tools", {"query": "x"}, {"properties": {}}))

    def test_a_correct_call_produces_no_message(self):
        self.assertEqual("", _argument_help("find_tools", {"search": "x"}, SCHEMA))


class ManagerUrlTests(unittest.TestCase):
    """A tool that talks back to the spawner should not have to be told where it is."""

    def url(self, **env):
        with patch.dict(os.environ, env, clear=False):
            for key in ("MCP_RUNNER_HOST", "MCP_MANAGER_PORT"):
                if key not in env:
                    os.environ.pop(key, None)
            return tool_loader.manager_url()

    def test_the_bind_address_is_turned_into_one_you_can_connect_to(self):
        # 0.0.0.0 is where the manager listens, not somewhere you can dial —
        # the same substitution the health check's probe URL makes.
        self.assertEqual("http://127.0.0.1:7860",
                         self.url(MCP_RUNNER_HOST="0.0.0.0", MCP_MANAGER_PORT="7860"))

    def test_a_manager_on_another_port_is_found_without_being_told(self):
        self.assertEqual("http://127.0.0.1:7871",
                         self.url(MCP_RUNNER_HOST="127.0.0.1", MCP_MANAGER_PORT="7871"))

    def test_an_unset_environment_falls_back_to_the_documented_default(self):
        self.assertEqual("http://127.0.0.1:7860", self.url())


TOOL_WITH_VALVES = """
from pydantic import BaseModel, Field

class Tools:
    class Valves(BaseModel):
        manager_url: str = Field(default="http://127.0.0.1:7860")
        output_dir: str = Field(default="/nonexistent")

    def __init__(self):
        self.valves = self.Valves()

    def hi(self) -> str:
        \"\"\"Hi.\"\"\"
        return "hi"
"""


class ValveAutofillTests(unittest.TestCase):
    def tool(self):
        return tool_loader.OpenWebUITool({"id": "demo", "name": "demo",
                                          "content": TOOL_WITH_VALVES})

    def test_the_manager_url_valve_is_filled_in(self):
        with patch.dict(os.environ, {"MCP_RUNNER_HOST": "0.0.0.0", "MCP_MANAGER_PORT": "7871"}):
            instance = tool_loader.create_tools_instance(self.tool(), {})
        self.assertEqual("http://127.0.0.1:7871", instance.valves.manager_url)

    def test_a_value_the_user_set_always_wins(self):
        # The rule the content valves already follow. Inventing a second one
        # here would be the thing that surprises somebody later.
        with patch.dict(os.environ, {"MCP_RUNNER_HOST": "0.0.0.0", "MCP_MANAGER_PORT": "7871"}):
            instance = tool_loader.create_tools_instance(
                self.tool(), {"manager_url": "http://elsewhere:9000"})
        self.assertEqual("http://elsewhere:9000", instance.valves.manager_url)

    def test_a_tool_without_that_valve_is_untouched(self):
        tool = tool_loader.OpenWebUITool({
            "id": "demo", "name": "demo",
            "content": "class Tools:\n    def hi(self) -> str:\n        \"\"\"Hi.\"\"\"\n        return 'hi'\n"})
        self.assertIsNotNone(tool_loader.create_tools_instance(tool, {}))


if __name__ == "__main__":
    unittest.main()
