"""Saving is not activating — what the two write routes may claim.

Writing a config or a tool's code puts it on disk. Whether the running runner
picked it up is a second question, and `restart_instance()` answers it with a
reason when it fails. Both routes used to throw that answer away and set
`restarted = True` unconditionally, so a restart that failed — a port taken
meanwhile, a venv that no longer builds — reached the dashboard and the control
tool as a change that was live. It was not: the old runner kept serving the old
code, and nothing said so.

The rule these tests pin: a failed restart never reports `restarted: true`, and
the reason travels with it. The save itself still succeeded, so `ok` stays true
— that separation is the point.
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
import app.routes.instances as instances_route
import app.routes.tools as tools_route
from app.admin_server import app
from app.schema import MCPInstance, MCPStatus

CODE = ('"""\nversion: 1.0.0\n"""\n\n\nclass Tools:\n'
        '    def hi(self) -> str:\n        """Say hi."""\n        return "hi"\n')

PASSWORD = "admin-password"


def config(port=8397):
    return {
        "id": "demo", "name": "demo", "description": "", "category": "Tests",
        "locked": False,
        "server": {"host": "127.0.0.1", "port": port, "endpoint": "/mcp"},
        "tool_source": {"type": "openwebui_json", "path": "tools/demo.json"},
        "values": {}, "venv": "default",
    }


class SaveRouteTestCase(unittest.TestCase):
    """Every path the two write routes touch, pointed at a temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.configs = root / "configs"
        self.tools = root / "tools"
        self.configs.mkdir()
        self.tools.mkdir()

        (self.configs / "demo.json").write_text(json.dumps(config()))
        self.tool_file = self.tools / "demo.json"
        self.tool_file.write_text(json.dumps([{
            "id": "demo", "name": "demo", "content": CODE, "specs": [],
            "meta": {"description": "", "manifest": {}},
        }]))

        # The routes resolve the tool path through the config, so it has to
        # point at the temp copy rather than at the installed one.
        cfg = config()
        cfg["tool_source"]["path"] = str(self.tool_file)
        (self.configs / "demo.json").write_text(json.dumps(cfg))

        config_store._state["demo"] = MCPInstance(
            id="demo", name="demo", status=MCPStatus.running, pid=4242,
            port=8397, host="127.0.0.1", endpoint="/mcp")
        self.addCleanup(lambda: config_store._state.pop("demo", None))

        for target in (
            patch.object(config_store, "CONFIGS_DIR", self.configs),
            # Saving code snapshots the previous tool JSON. Without this the
            # snapshots land in the real runtime/history/demo/ — this suite
            # runs on the server, where that directory belongs to an instance.
            patch.object(api_helpers, "HISTORY_DIR", root / "history"),
            patch.object(tools_route, "install_dependencies", lambda *a, **kw: (True, "")),
        ):
            target.start()
            self.addCleanup(target.stop)

        self.client = TestClient(app)
        lockout.clear_all()
        self.original_hash = auth._password_hash
        auth._password_hash = hashlib.sha256(PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", self.original_hash))

    def headers(self):
        return {"Authorization": f"Bearer {PASSWORD}"}

    def save_code(self):
        return self.client.put("/api/instances/demo/tool-code",
                               json={"code": CODE}, headers=self.headers())

    def save_config(self, port=8398):
        body = config(port=port)
        body["tool_source"]["path"] = str(self.tool_file)
        return self.client.put("/api/instances/demo", json=body, headers=self.headers())


class RestartReportingTests(SaveRouteTestCase):
    """Saved is not the same as live, and the answer must say which."""

    def test_a_failed_restart_after_saving_code_is_not_reported_as_restarted(self):
        with patch.object(tools_route, "restart_instance",
                          lambda _id: (False, "runner crashed")):
            response = self.save_code()
        body = response.json()
        self.assertEqual(200, response.status_code, body)
        self.assertTrue(body["ok"])          # the file *was* written
        self.assertFalse(body["restarted"])
        self.assertEqual("runner crashed", body["restart_error"])

    def test_a_successful_restart_after_saving_code_still_says_so(self):
        with patch.object(tools_route, "restart_instance", lambda _id: (True, "")):
            body = self.save_code().json()
        self.assertTrue(body["restarted"])
        self.assertEqual("", body["restart_error"])

    def test_a_failed_restart_after_saving_config_is_not_reported_as_restarted(self):
        with patch.object(instances_route, "restart_instance",
                          lambda _id: (False, "port 8398 is already in use")):
            body = self.save_config().json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["restarted"])
        self.assertEqual("port 8398 is already in use", body["restart_error"])

    def test_a_successful_restart_after_saving_config_still_says_so(self):
        with patch.object(instances_route, "restart_instance", lambda _id: (True, "")):
            body = self.save_config().json()
        self.assertTrue(body["restarted"])
        self.assertEqual("", body["restart_error"])

    def test_a_config_change_that_needs_no_restart_reports_neither(self):
        with patch.object(instances_route, "restart_instance",
                          lambda _id: (False, "should not have been called")):
            body = self.save_config(port=8397).json()   # same port: nothing to apply
        self.assertFalse(body["restarted"])
        self.assertEqual("", body["restart_error"])
