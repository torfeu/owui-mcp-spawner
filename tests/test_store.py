"""The store — what ships in examples/, and installing it with one click.

Everything the routes touch points at temp directories: the examples the store
lists, the configs and tools an install writes, and the install itself (pip
and the validation subprocess are patched). This suite runs on the server,
and a store test that reached the real configs/ would put instances on the
running installation.
"""
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.api_helpers as api_helpers
import app.auth as auth
import app.config_store as config_store
import app.lockout as lockout
from app.admin_server import app

PASSWORD = "admin-password"


def tool(tool_id, version="1.0.0", description="A shipped tool", category="System"):
    code = (f'"""\ntitle: {tool_id}\ndescription: {description}\nversion: {version}\n'
            f'category: {category}\n"""\n\nclass Tools:\n'
            '    def hi(self) -> str:\n        """Say hi."""\n        return "hi"\n')
    return [{"id": tool_id, "name": tool_id.replace("_", " ").title(), "content": code,
             "specs": [{"name": "hi", "description": "Say hi.",
                        "parameters": {"type": "object", "properties": {}}}],
             "meta": {"description": "", "manifest": {}}}]


VALID = {"valid": True, "errors": [], "warnings": [], "valves": {},
         "tools": [{"name": "hi", "description": "Say hi.",
                    "parameters": {"type": "object", "properties": {}}}]}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.examples, self.configs, self.tools = root / "examples", root / "configs", root / "tools"
        for d in (self.examples, self.configs, self.tools):
            d.mkdir()
        (self.examples / "router.json").write_text(json.dumps(tool("demo_router", "0.0.10")))
        (self.examples / "probe.json").write_text(json.dumps(tool("demo_probe", "0.0.4")))
        # An MCP-connection sample: an id, but no code — not a tool.
        (self.examples / "sample.json").write_text(json.dumps({"id": "sample", "url": "x"}))

        for target in (
            patch.object(api_helpers, "EXAMPLES_DIR", self.examples),
            patch.object(api_helpers, "_examples_index", (-1.0, {})),
            patch.object(api_helpers, "TOOLS_DIR", self.tools),
            patch.object(config_store, "CONFIGS_DIR", self.configs),
            # An install records its tool as "./tools/<id>.json", resolved
            # against this — it has to be the temp root, not the project.
            patch.object(config_store, "BASE_DIR", root),
            patch.object(api_helpers, "install_dependencies", lambda *a, **kw: (True, "")),
            patch.object(api_helpers, "validate_tool_code", lambda *a, **kw: dict(VALID)),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(lambda: [config_store._state.pop(i, None)
                                 for i in ("demo_router", "demo_probe")])

        lockout.clear_all()
        original = auth._password_hash
        auth._password_hash = hashlib.sha256(PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))
        self.client = TestClient(app)

    @property
    def admin(self):
        return {"Authorization": f"Bearer {PASSWORD}"}

    def listing(self):
        response = self.client.get("/api/store", headers=self.admin)
        self.assertEqual(200, response.status_code, response.text)
        return {entry["id"]: entry for entry in response.json()}


class StoreListingTests(StoreTestCase):

    def test_it_lists_the_shipped_tools_and_only_those(self):
        entries = self.listing()
        self.assertEqual({"demo_router", "demo_probe"}, set(entries))   # not the sample
        router = entries["demo_router"]
        self.assertEqual("0.0.10", router["version"])
        self.assertEqual("A shipped tool", router["description"])
        self.assertEqual("System", router["category"])
        self.assertFalse(router["installed"])

    def test_it_needs_a_login(self):
        self.assertEqual(401, self.client.get("/api/store").status_code)


class StoreInstallTests(StoreTestCase):

    def install(self, tool_id, **body):
        return self.client.post(f"/api/store/{tool_id}/install", json=body, headers=self.admin)

    def test_install_creates_the_instance_like_an_upload(self):
        response = self.install("demo_router")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("demo_router", response.json()["id"])
        cfg = json.loads((self.configs / "demo_router.json").read_text())
        self.assertEqual("System", cfg["category"])            # from the tool's header
        self.assertTrue((self.tools / "demo_router.json").exists())
        entry = self.listing()["demo_router"]
        self.assertTrue(entry["installed"])
        self.assertEqual("0.0.10", entry["installed_version"])
        self.assertFalse(entry["update_available"])

    def test_an_installed_tool_is_not_installed_twice(self):
        self.install("demo_router")
        self.assertEqual(409, self.install("demo_router").status_code)

    def test_a_newer_shipped_version_shows_as_an_update(self):
        self.install("demo_router")
        (self.examples / "router.json").write_text(json.dumps(tool("demo_router", "0.0.11")))
        entry = self.listing()["demo_router"]
        self.assertTrue(entry["update_available"])
        self.assertEqual("0.0.11", entry["version"])

    def test_only_what_ships_can_be_installed(self):
        # The id is looked up in the index of examples/, never made into a path.
        for bad in ("nope", "sample"):
            with self.subTest(tool_id=bad):
                self.assertEqual(404, self.install(bad).status_code)
        # An encoded slash does not even reach the route (405) — either way
        # nothing is installed.
        self.assertNotEqual(200, self.install("..%2Fconfigs%2Fdemo").status_code)
        self.assertEqual([], list(self.configs.iterdir()))

    def test_a_category_and_venv_can_be_chosen(self):
        response = self.install("demo_probe", category="Tests", venv="default")
        self.assertEqual(200, response.status_code, response.text)
        cfg = json.loads((self.configs / "demo_probe.json").read_text())
        self.assertEqual("Tests", cfg["category"])

    def test_a_further_copy_goes_in_under_its_own_id(self):
        """One bridge per external MCP server: the shipped tool installed
        again, next to the first, under an id of the caller's choosing."""
        self.install("demo_router")
        response = self.install("demo_router", id="demo_router_2")
        self.assertEqual(200, response.status_code, response.text)
        copy = json.loads((self.configs / "demo_router_2.json").read_text())
        self.assertEqual("demo_router_2", copy["id"])
        self.assertTrue((self.tools / "demo_router_2.json").exists())
        # The original is untouched, and the shipped file still describes itself.
        self.assertTrue((self.configs / "demo_router.json").exists())
        self.assertEqual("demo_router", json.loads((self.examples / "router.json").read_text())[0]["id"])
        self.addCleanup(lambda: config_store._state.pop("demo_router_2", None))

    def test_a_copy_needs_a_usable_free_id(self):
        self.install("demo_router")
        self.assertEqual(409, self.install("demo_probe", id="demo_router").status_code)
        self.assertEqual(400, self.install("demo_router", id="../x").status_code)
        self.assertEqual(400, self.install("demo_router", id="example").status_code)  # reserved

    def test_a_bad_venv_name_is_refused(self):
        self.assertEqual(400, self.install("demo_probe", venv="../x").status_code)

    def test_readonly_mode_refuses_the_install(self):
        with patch.dict("os.environ", {"MCP_EDIT_MODE": "readonly"}):
            self.assertEqual(403, self.install("demo_router").status_code)
        self.assertFalse((self.configs / "demo_router.json").exists())


if __name__ == "__main__":
    unittest.main()
