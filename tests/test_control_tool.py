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


class ContentToolTests(unittest.TestCase):
    """What the model gets to see of the file storage.

    The rule that matters here is the house rule: little by default, detail on
    request. The overview must not be a JSON dump, and a .docx must never end
    up in the chat as bytes.
    """

    OVERVIEW = {
        "instances": [
            {"instance": "docs", "files": 2, "bytes": 3 * 1024 * 1024,
             "oldest": None, "limit_bytes": 10 * 1024 * 1024, "percent": 30},
        ],
        "total_bytes": 3 * 1024 * 1024, "total_files": 2,
    }
    FILES = {
        "instance": "docs", "bytes": 3 * 1024 * 1024, "files": 2,
        "items": [
            {"name": "report.docx", "size": 2 * 1024 * 1024, "modified": None,
             "url": "/content/docs/report.docx?t=abc"},
            {"name": "notes.md", "size": 1024, "modified": None,
             "url": "/content/docs/notes.md?t=def"},
        ],
    }

    def test_the_overview_is_one_line_per_instance_and_not_a_json_dump(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.OVERVIEW))

        result = tools.list_content()

        self.assertIn("docs: 2 file(s), 3.0 MB", result)
        self.assertIn("30 % of quota", result)
        self.assertNotIn("{", result)

    def test_naming_an_instance_gives_the_files_with_their_links(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))

        result = tools.list_content("docs")

        self.assertIn("report.docx", result)
        # Relative links from the manager are made absolute here — the model
        # hands this to a user, who has no manager URL to resolve it against.
        self.assertIn("http://127.0.0.1:7860/content/docs/report.docx?t=abc", result)

    def test_a_binary_file_is_described_and_never_dumped(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))
        response = Mock(status_code=200, content=b"PK\x03\x04\x00binary")

        with patch("httpx.get", return_value=response):
            result = tools.read_content("docs", "report.docx")

        self.assertIn("binary docx file", result)
        self.assertIn("/content/docs/report.docx?t=abc", result)

    def test_a_text_file_comes_back_and_is_capped(self):
        tools = load_control_tools()
        tools._get = Mock(return_value=json.dumps(self.FILES))
        response = Mock(status_code=200, content=("x" * 50).encode())

        with patch("httpx.get", return_value=response):
            full = tools.read_content("docs", "notes.md")
            cut = tools.read_content("docs", "notes.md", max_chars=10)

        self.assertEqual("x" * 50, full)
        self.assertTrue(cut.startswith("x" * 10))
        self.assertIn("cut off after 10 characters of 50", cut)

    def test_deleting_is_off_until_it_is_switched_on(self):
        tools = load_control_tools()
        tools._delete = Mock()

        result = tools.delete_content("docs", "report.docx")

        self.assertIn("allow_content_delete", result)
        tools._delete.assert_not_called()

    def test_an_empty_filename_empties_the_whole_folder(self):
        tools = load_control_tools()
        tools.valves.allow_content_delete = True
        tools._delete = Mock(return_value="{}")

        tools.delete_content("docs")

        tools._delete.assert_called_once_with("/api/content/docs")


if __name__ == "__main__":
    unittest.main()
