import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app.tool_editor import validate_tool_code


CONTROL_TOOL_PATH = Path(__file__).resolve().parents[1] / "examples" / "mcp-manager-control.json"


def load_control_tools():
    exported = json.loads(CONTROL_TOOL_PATH.read_text())[0]
    namespace = {}
    exec(exported["content"], namespace)
    return namespace["Tools"]()


class ControlToolSpecTests(unittest.TestCase):
    """The MCP runner serves the JSON 'specs' descriptions (tool_loader), while the
    schema comes from the code — so a hand-edited spec silently drifts from its
    docstring. Regenerate the specs instead of editing them."""

    def test_specs_match_the_docstrings_of_the_shipped_code(self):
        exported = json.loads(CONTROL_TOOL_PATH.read_text())[0]
        result = validate_tool_code(exported["content"])

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(
            {t["name"]: t for t in result["tools"]},
            {s["name"]: s for s in exported["specs"]},
        )

    def test_write_actions_document_the_lock(self):
        # These endpoints call require_not_locked() and answer 403 on a locked
        # instance; start/stop deliberately keep working.
        tools = load_control_tools()
        blocked_by_lock = (
            "save_tool_code", "reinstall_instance", "restart_instance", "delete_instance",
            "update_instance_category", "update_instance_values",
            "update_instance_dependencies", "update_instance_venv",
        )

        for name in blocked_by_lock:
            with self.subTest(tool=name):
                self.assertIn("locked", getattr(tools, name).__doc__)


class ControlToolRegressionTests(unittest.TestCase):
    def test_export_requires_code_read_permission(self):
        tools = load_control_tools()
        tools.valves.allow_export_tool = True
        tools.valves.allow_get_code = False
        tools._get = Mock()

        result = tools.export_tool("demo", "Demo", "Description")

        self.assertIn("allow_get_code", result)
        tools._get.assert_not_called()

    def test_value_update_reports_automatic_restart(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": True}))

        result = tools.update_instance_values("demo", {"api_url": "new"})

        self.assertIn("already restarted", result)
        self.assertNotIn("did not restart", result)

    def test_value_update_only_suggests_restart_when_manager_did_not_restart(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": False}))

        result = tools.update_instance_values("demo", {"api_url": "new"})

        self.assertIn("did not restart", result)
        self.assertIn("If it is currently running", result)

    def test_dependency_update_does_not_reinstall_twice(self):
        tools = load_control_tools()
        tools._put = Mock(return_value=json.dumps({"ok": True, "restarted": True}))
        tools._post = Mock()

        result = tools.update_instance_dependencies("demo", ["httpx>=0.27"])

        self.assertIn('"ok": true', result)
        tools._put.assert_called_once()
        tools._post.assert_not_called()

    def test_upload_omits_empty_category_override(self):
        tools = load_control_tools()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "id": "demo"}

        with patch("httpx.post", return_value=response) as post:
            tools.upload_tool("[]")

        self.assertEqual({}, post.call_args.kwargs["data"])

    def test_upload_sends_non_empty_category_override(self):
        tools = load_control_tools()
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "id": "demo"}

        with patch("httpx.post", return_value=response) as post:
            tools.upload_tool("[]", "  Smart Home  ")

        self.assertEqual({"category": "Smart Home"}, post.call_args.kwargs["data"])


if __name__ == "__main__":
    unittest.main()
