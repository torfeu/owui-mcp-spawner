import hashlib
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.auth as auth
from app.admin_server import app


EXPECTED_API_ROUTES = {
    ("GET", "/api/auth-status"),
    ("GET", "/api/auth-check"),
    ("GET", "/api/instances"),
    ("GET", "/api/instances/{instance_id}"),
    ("GET", "/api/instances/{instance_id}/config"),
    ("POST", "/api/instances/upload"),
    ("PUT", "/api/instances/{instance_id}"),
    ("POST", "/api/instances/{instance_id}/start"),
    ("POST", "/api/instances/{instance_id}/stop"),
    ("POST", "/api/instances/{instance_id}/restart"),
    ("GET", "/api/instances/{instance_id}/tool-code"),
    ("PUT", "/api/instances/{instance_id}/tool-code"),
    ("POST", "/api/instances/{instance_id}/reinstall"),
    ("GET", "/api/instances/{instance_id}/logs/install"),
    ("GET", "/api/instances/{instance_id}/logs/runtime"),
    ("GET", "/api/tools/template"),
    ("POST", "/api/tools/validate"),
    ("POST", "/api/tools/export"),
    ("POST", "/api/tools/create"),
    ("GET", "/api/venvs"),
    ("POST", "/api/venvs"),
    ("DELETE", "/api/venvs/{name}"),
    ("POST", "/api/instances/{instance_id}/lock"),
    ("POST", "/api/instances/{instance_id}/unlock"),
    ("GET", "/api/instances/{instance_id}/export"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/settings/mcp-token"),
    ("POST", "/api/server/restart"),
    ("DELETE", "/api/instances/{instance_id}"),
}


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.original_hash = auth._password_hash
        self.original_mode = os.environ.get("MCP_EDIT_MODE")
        self.original_mode_locked = os.environ.get("MCP_EDIT_MODE_LOCKED")

    def tearDown(self):
        auth._password_hash = self.original_hash
        for key, value in (
            ("MCP_EDIT_MODE", self.original_mode),
            ("MCP_EDIT_MODE_LOCKED", self.original_mode_locked),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_all_api_routes_remain_registered(self):
        actual = {
            (method, route.path)
            for route in app.routes
            if route.path.startswith("/api")
            for method in route.methods
        }
        self.assertEqual(EXPECTED_API_ROUTES, actual)

    def test_openapi_and_runtime_version_match(self):
        self.assertEqual("0.1.2", app.version)
        self.assertEqual("0.1.2", app.openapi()["info"]["version"])

    def test_authentication_rejects_missing_and_wrong_password(self):
        auth._password_hash = hashlib.sha256(b"correct-password").hexdigest()
        self.assertEqual(401, self.client.get("/api/settings").status_code)
        response = self.client.get(
            "/api/settings",
            headers={"Authorization": "Bearer wrong-password"},
        )
        self.assertEqual(401, response.status_code)

    def test_authentication_accepts_correct_password(self):
        auth._password_hash = hashlib.sha256(b"correct-password").hexdigest()
        response = self.client.get(
            "/api/settings",
            headers={"Authorization": "Bearer correct-password"},
        )
        self.assertEqual(200, response.status_code)

    def test_guest_instance_view_does_not_expose_connection_details(self):
        auth._password_hash = hashlib.sha256(b"password").hexdigest()
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "Example",
            "category": "Tests", "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp",
            "pid": 123, "error": "secret detail",
        })()
        fake_config = type("Config", (), {"locked": False, "venv": "default"})()
        with (
            patch("app.routes.instances.get_all_states", return_value=[fake_state]),
            patch("app.routes.instances.load_all_configs", return_value={"demo": fake_config}),
            patch("app.routes.instances._version_from_tool_file", return_value="1.0.0"),
        ):
            data = self.client.get("/api/instances").json()[0]
        self.assertEqual(
            {"id", "name", "description", "category", "status", "version"},
            set(data),
        )

    def test_guest_view_and_require_auth_accept_the_same_tokens(self):
        # The guest-view check and require_auth must share one parser: any
        # Authorization header must either unlock both or neither, or an admin
        # could silently land in the reduced guest view (or a guest in the
        # full one) depending on header quirks.
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "Example",
            "category": "Tests", "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp",
            "pid": 123, "error": None,
        })()
        fake_config = type("Config", (), {"locked": False, "venv": "default"})()
        with (
            patch("app.routes.instances.get_all_states", return_value=[fake_state]),
            patch("app.routes.instances.load_all_configs", return_value={"demo": fake_config}),
            patch("app.routes.instances._version_from_tool_file", return_value="1.0.0"),
        ):
            # Sanity: the exact token unlocks the full view at all
            full = self.client.get(
                "/api/instances", headers={"Authorization": "Bearer pw"}
            ).json()[0]
            self.assertIn("port", full)
            for header in ("Bearer pw", "Bearer  pw", "Bearer pw ", "bearer pw",
                           "Bearer wrong", "Basic pw", ""):
                headers = {"Authorization": header} if header else {}
                strict_ok = (
                    self.client.get("/api/auth-check", headers=headers).status_code == 200
                )
                listed = self.client.get("/api/instances", headers=headers).json()[0]
                self.assertEqual(strict_ok, "port" in listed, repr(header))

    def test_config_endpoint_serves_secret_classification(self):
        from app.schema import MCPConfig, ServerConfig, ToolSourceConfig

        auth._password_hash = None
        cfg = MCPConfig(
            id="demo", name="Demo",
            server=ServerConfig(port=8123),
            tool_source=ToolSourceConfig(path="./tools/demo.json"),
            values={"api_key": "real-secret", "base_url": "http://x"},
        )
        with patch("app.routes.instances.load_config", return_value=cfg):
            data = self.client.get("/api/instances/demo/config").json()
        self.assertEqual("********", data["values"]["api_key"])
        self.assertEqual("http://x", data["values"]["base_url"])
        self.assertEqual(["api_key"], data["secret_fields"])

    def test_edit_modes_are_enforced_by_dependencies(self):
        auth._password_hash = None
        os.environ["MCP_EDIT_MODE"] = "readonly"
        self.assertEqual(
            403,
            self.client.post("/api/tools/create", json={"id": "x", "code": "x"}).status_code,
        )
        os.environ["MCP_EDIT_MODE"] = "upload"
        self.assertEqual(
            403,
            self.client.post("/api/tools/validate", json={"code": "x"}).status_code,
        )

    def test_reserved_id_example_is_rejected(self):
        auth._password_hash = None
        os.environ.pop("MCP_EDIT_MODE", None)
        response = self.client.post(
            "/api/tools/create", json={"id": "example", "code": "class Tools: pass"}
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("reserved", response.json()["detail"])

    def test_update_config_rejects_wrong_body_shapes(self):
        from app.schema import MCPConfig, ServerConfig, ToolSourceConfig

        auth._password_hash = None
        os.environ.pop("MCP_EDIT_MODE", None)
        cfg = MCPConfig(
            id="demo", name="Demo",
            server=ServerConfig(port=8123),
            tool_source=ToolSourceConfig(path="./tools/demo.json"),
        )
        with (
            patch("app.api_helpers.load_config", return_value=cfg),
            patch("app.routes.instances.load_config", return_value=cfg),
        ):
            for body in ({"server": "nope"}, {"values": []}, {"install": 5}, {"lifecycle": "x"}):
                response = self.client.put("/api/instances/demo", json=body)
                self.assertEqual(422, response.status_code, body)

    def test_cli_locked_edit_mode_cannot_be_changed(self):
        auth._password_hash = None
        os.environ["MCP_EDIT_MODE"] = "readonly"
        os.environ["MCP_EDIT_MODE_LOCKED"] = "1"
        response = self.client.put("/api/settings", json={"edit_mode": "full"})
        self.assertEqual(403, response.status_code)
        # Re-submitting the active mode is a no-op, not a change (and not an error)
        response = self.client.put("/api/settings", json={"edit_mode": "readonly"})
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()["changed"])
        self.assertTrue(self.client.get("/api/settings").json()["edit_mode_locked"])

    def test_rejected_settings_save_applies_nothing(self):
        # The client only adopts the new session token when the whole save
        # succeeds — a request rejected on a later field (shared_port) must
        # therefore not have persisted the new password already.
        auth._password_hash = hashlib.sha256(b"old-pw").hexdigest()
        manager_port = int(os.environ.get("MCP_MANAGER_PORT", "7860"))
        with patch("app.auth.set_password") as set_pw:
            response = self.client.put(
                "/api/settings",
                headers={"Authorization": "Bearer old-pw"},
                json={
                    "password": "new-pw-123",
                    "password_confirm": "new-pw-123",
                    "current_password": "old-pw",
                    "shared_port": manager_port,
                },
            )
        self.assertEqual(409, response.status_code)
        set_pw.assert_not_called()

    def test_reinstall_stays_available_in_readonly_mode(self):
        auth._password_hash = None
        os.environ["MCP_EDIT_MODE"] = "readonly"
        with patch("app.routes.tools.load_config", return_value=None):
            response = self.client.post("/api/instances/demo/reinstall")
        # 404 (config missing), not 403: readonly mode must not block reinstall
        self.assertEqual(404, response.status_code)

    def test_locked_instance_can_stop_but_cannot_restart(self):
        auth._password_hash = None
        locked_config = type("Config", (), {"locked": True})()
        with (
            patch("app.api_helpers.load_config", return_value=locked_config),
            patch("app.routes.instances.stop_instance", return_value=(True, "")) as stop,
            patch("app.routes.instances.restart_instance", return_value=(True, "")) as restart,
        ):
            self.assertEqual(403, self.client.post("/api/instances/demo/restart").status_code)
            self.assertEqual(200, self.client.post("/api/instances/demo/stop").status_code)
        restart.assert_not_called()
        stop.assert_called_once_with("demo")

    def test_delete_aborts_when_stop_fails(self):
        # A process that survives SIGKILL must not have its config deleted —
        # that would orphan a live process nothing can address anymore.
        from app.schema import MCPStatus

        auth._password_hash = None
        os.environ.pop("MCP_EDIT_MODE", None)
        running = type("State", (), {"status": MCPStatus.running})()
        unlocked = type("Config", (), {"locked": False})()
        with (
            patch("app.api_helpers.load_config", return_value=unlocked),
            patch("app.routes.instances.get_instance_state", return_value=running),
            patch("app.routes.instances.stop_instance", return_value=(False, "pid 42 survived SIGKILL")),
            patch("app.routes.instances.delete_config") as delete_cfg,
        ):
            response = self.client.delete("/api/instances/demo")
        self.assertEqual(500, response.status_code)
        delete_cfg.assert_not_called()

    def test_create_tool_tolerates_non_string_metadata(self):
        # Optional metadata of any JSON type must fall back to defaults with a
        # clean response instead of 500ing on .strip().
        auth._password_hash = None
        os.environ.pop("MCP_EDIT_MODE", None)
        provision = AsyncMock(return_value={"ok": True, "id": "unittest_coercion", "port": 8101})
        with patch("app.routes.tools._provision_new_tool", provision):
            response = self.client.post(
                "/api/tools/create",
                json={
                    "id": "unittest_coercion", "code": "class Tools: pass",
                    "name": 123, "description": {"x": 1}, "category": [], "venv": 0,
                },
            )
        self.assertEqual(200, response.status_code, response.text)
        kwargs = provision.await_args.kwargs
        self.assertEqual("unittest_coercion", kwargs["name"])
        self.assertEqual("", kwargs["description"])
        self.assertEqual("", kwargs["category"])
        self.assertEqual("default", kwargs["venv"])

    def test_openwebui_upload_tolerates_non_string_metadata(self):
        auth._password_hash = None
        os.environ.pop("MCP_EDIT_MODE", None)
        provision = AsyncMock(return_value={"ok": True, "id": "unittest_owui", "port": 8102})
        payload = {
            "id": "unittest_owui", "content": "class Tools: pass", "specs": [],
            "name": 123, "meta": {"description": 7},
        }
        with patch("app.api_helpers._provision_new_tool", provision):
            response = self.client.post(
                "/api/instances/upload",
                files={"file": ("tool.json", json.dumps(payload), "application/json")},
            )
        self.assertEqual(200, response.status_code, response.text)
        kwargs = provision.await_args.kwargs
        self.assertEqual("unittest_owui", kwargs["name"])
        self.assertEqual("", kwargs["description"])

    def test_empty_upload_category_does_not_override_mcp_config(self):
        auth._password_hash = None
        imported = AsyncMock(return_value={"ok": True, "id": "demo", "port": 8101})
        with patch("app.routes.tools._import_mcp_config", imported):
            response = self.client.post(
                "/api/instances/upload",
                files={"file": ("config.json", json.dumps({"id": "demo"}), "application/json")},
                data={"category": ""},
            )
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(imported.await_args.kwargs["category"])


if __name__ == "__main__":
    unittest.main()
