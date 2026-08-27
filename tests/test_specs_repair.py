"""Specs must describe the code, no matter how the tool got installed.

An OpenWebUI export often carries only the first line of each docstring — cut
off mid-sentence. Those descriptions are what the info dialog, the specs API
and a tool router hand to a model, so the framework rebuilds them from the
tool's own code: at upload time, and once for what is already installed.
"""
import asyncio
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.admin_server as admin_server
import app.auth as auth
from app.admin_server import app

# What an OpenWebUI export tends to look like: description cut after line one
TRUNCATED = [{
    "id": "demo", "name": "Demo",
    "content": "class Tools:\n    def act(self):\n        pass\n",
    "specs": [{"name": "act", "description": "Reads the state of an item — controllable or",
               "parameters": {"type": "object", "properties": {}}}],
    "meta": {"description": "Demo tool", "manifest": {"keep": "me"}},
}]

# What validate_tool_code() derives from the same code
FULL_SPECS = [{
    "name": "act",
    "description": "Reads the state of an item — controllable or\nstatus-only.\n\nUse it whenever…",
    "parameters": {"type": "object", "properties": {}},
}]


class UploadKeepsGeneratedSpecsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        auth._password_hash = self.original_hash
        self.tmp.cleanup()

    def test_uploaded_specs_are_replaced_by_the_generated_ones(self):
        written = {}

        class FakePath:
            def __init__(self, name):
                self.name = name

            def write_text(self, text):
                written["payload"] = json.loads(text)

        validation = {"valid": True, "tools": FULL_SPECS, "valves": {}, "warnings": []}
        with (
            patch("app.api_helpers.install_dependencies", return_value=(True, "")),
            patch("app.api_helpers.validate_tool_code", return_value=validation),
            patch("app.api_helpers.save_config"),
            patch("app.api_helpers.set_instance_state"),
            patch("app.api_helpers.TOOLS_DIR") as tools_dir,
        ):
            tools_dir.__truediv__ = lambda self, other: FakePath(other)
            from app.api_helpers import _provision_new_tool

            asyncio.run(_provision_new_tool(
                tool_id="demo", name="Demo", description="Demo tool", category="",
                code=TRUNCATED[0]["content"], requirements=[], venv="default",
                persist_json=json.loads(json.dumps(TRUNCATED)), port=8199,
            ))

        entry = written["payload"][0]
        self.assertEqual(FULL_SPECS, entry["specs"])
        # Everything else the upload brought must survive untouched
        self.assertEqual({"keep": "me"}, entry["meta"]["manifest"])
        self.assertEqual("Demo tool", entry["meta"]["description"])
        self.assertEqual(TRUNCATED[0]["content"], entry["content"])


class DeclaredCategoryTests(unittest.TestCase):
    """A tool may declare its own category — the shipped ones say `System`."""

    def test_the_docstring_category_is_read(self):
        from app.api_helpers import _category_from_code

        code = '"""\ntitle: T\nversion: 0.1.0\ncategory: System\n"""\nclass Tools: pass\n'
        self.assertEqual("System", _category_from_code(code))

    def test_no_declaration_means_no_category(self):
        from app.api_helpers import _category_from_code

        self.assertEqual("", _category_from_code('"""\ntitle: T\n"""\n'))
        self.assertEqual("", _category_from_code('"""\ncategory:\n"""\n'))
        self.assertEqual("", _category_from_code(None))

    def test_a_category_inside_a_description_line_is_not_mistaken_for_one(self):
        from app.api_helpers import _category_from_code

        code = '"""\ndescription: sorts by category: name\n"""\n'
        self.assertEqual("", _category_from_code(code))

    def test_both_shipped_tools_declare_system(self):
        # They record their own traffic and would otherwise top every usage
        # ranking; the statistics view sorts them out by this category.
        import json
        from pathlib import Path
        from app.api_helpers import _category_from_code

        examples = Path(__file__).resolve().parents[1] / "examples"
        for name in ("mcp-manager-control.json", "mcp-tool-router.json"):
            with self.subTest(tool=name):
                content = json.loads((examples / name).read_text())[0]["content"]
                self.assertEqual("System", _category_from_code(content))

    def test_an_explicit_category_wins_over_the_declaration(self):
        validation = {"valid": True, "tools": FULL_SPECS, "valves": {}, "warnings": []}
        saved = {}

        class FakePath:
            def __init__(self, name): self.name = name
            def write_text(self, text): saved["json"] = text

        code = '"""\ntitle: T\ncategory: System\n"""\nclass Tools: pass\n'
        with (
            patch("app.api_helpers.install_dependencies", return_value=(True, "")),
            patch("app.api_helpers.validate_tool_code", return_value=validation),
            patch("app.api_helpers.save_config") as save_config,
            patch("app.api_helpers.set_instance_state"),
            patch("app.api_helpers.TOOLS_DIR") as tools_dir,
        ):
            tools_dir.__truediv__ = lambda self, other: FakePath(other)
            from app.api_helpers import _provision_new_tool

            for passed, expected in (("", "System"), ("Recht", "Recht")):
                asyncio.run(_provision_new_tool(
                    tool_id="demo", name="Demo", description="", category=passed,
                    code=code, requirements=[], venv="default", port=8199,
                ))
                self.assertEqual(expected, save_config.call_args[0][0].category)


class SpecsMigrationTests(unittest.TestCase):
    """The one-time repair of already installed tools."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.tool_path = self.root / "demo.json"
        self.tool_path.write_text(json.dumps(TRUNCATED))
        self.marker = self.root / ".specs_migrated"
        self.cfg = type("Config", (), {"id": "demo", "venv": "default"})()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, validation, configs=None):
        with (
            patch.object(admin_server, "_SPECS_MIGRATION_MARKER", self.marker),
            patch.object(admin_server, "load_all_configs",
                         return_value=configs if configs is not None else {"demo": self.cfg}),
            patch.object(admin_server, "resolve_tool_path", return_value=self.tool_path),
            patch.object(admin_server, "python_path", return_value="/usr/bin/python3"),
            patch.object(admin_server, "validate_tool_code", return_value=validation),
        ):
            asyncio.run(admin_server._migrate_tool_specs())
        return json.loads(self.tool_path.read_text())

    def test_truncated_specs_are_rebuilt_and_the_rest_is_preserved(self):
        raw = self._run({"valid": True, "tools": FULL_SPECS})
        self.assertEqual(FULL_SPECS, raw[0]["specs"])
        self.assertEqual({"keep": "me"}, raw[0]["meta"]["manifest"])
        self.assertEqual(TRUNCATED[0]["content"], raw[0]["content"])
        self.assertTrue(self.marker.exists())

    def test_matching_specs_leave_the_file_untouched(self):
        # Rewriting identical content would bump the mtime and needlessly drop
        # the specs cache on every upgrade.
        current = json.loads(json.dumps(TRUNCATED))
        self.tool_path.write_text(json.dumps(current))
        before = self.tool_path.stat().st_mtime_ns
        self._run({"valid": True, "tools": current[0]["specs"]})
        self.assertEqual(before, self.tool_path.stat().st_mtime_ns)

    def test_a_tool_that_cannot_be_validated_is_left_alone_and_retried(self):
        raw = self._run({"valid": False, "errors": ["ModuleNotFoundError: requests"]})
        self.assertEqual(TRUNCATED[0]["specs"], raw[0]["specs"])
        # No marker: a momentarily broken tool must not be written off forever
        self.assertFalse(self.marker.exists())

    def test_the_marker_prevents_a_second_run(self):
        self.marker.write_text("done\n")
        called = []
        with (
            patch.object(admin_server, "_SPECS_MIGRATION_MARKER", self.marker),
            patch.object(admin_server, "load_all_configs", side_effect=lambda: called.append(1) or {}),
        ):
            asyncio.run(admin_server._migrate_tool_specs())
        self.assertEqual([], called)

    def test_instances_without_embedded_code_are_skipped(self):
        # An imported MCP config points at a tool file with no `content` —
        # there is nothing to derive specs from, so the file stays as it is.
        self.tool_path.write_text(json.dumps([{"id": "demo", "specs": []}]))
        before = self.tool_path.stat().st_mtime_ns
        raw = self._run({"valid": True, "tools": FULL_SPECS})
        self.assertEqual([], raw[0]["specs"])
        self.assertEqual(before, self.tool_path.stat().st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
