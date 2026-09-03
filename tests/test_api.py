import hashlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app.api_helpers as api_helpers
import app.auth as auth
import app.lockout as lockout
from app.admin_server import app
from app.schema import ContentConfig, IdentityMode


EXPECTED_API_ROUTES = {
    ("GET", "/api/agent-identities"),
    ("POST", "/api/agent-identities"),
    ("PUT", "/api/agent-identities/{sub}"),
    ("DELETE", "/api/agent-identities/{sub}"),
    ("POST", "/api/agent-identities/{sub}/token"),
    ("GET", "/api/identities"),
    ("DELETE", "/api/identities/{sub}"),
    ("GET", "/api/policy"),
    ("PUT", "/api/policy"),
    ("POST", "/api/policy/preview"),
    ("GET", "/api/auth-status"),
    ("GET", "/api/auth-check"),
    ("GET", "/api/instances"),
    ("GET", "/api/instances/{instance_id}"),
    ("GET", "/api/instances/{instance_id}/config"),
    ("GET", "/api/instances/{instance_id}/specs"),
    ("POST", "/api/instances/upload"),
    ("PUT", "/api/instances/{instance_id}"),
    ("POST", "/api/instances/{instance_id}/start"),
    ("POST", "/api/instances/{instance_id}/stop"),
    ("POST", "/api/instances/{instance_id}/restart"),
    ("GET", "/api/instances/{instance_id}/tool-code"),
    ("PUT", "/api/instances/{instance_id}/tool-code"),
    ("POST", "/api/instances/{instance_id}/reinstall"),
    ("POST", "/api/instances/{instance_id}/update-from-example"),
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
    ("POST", "/api/instances/{instance_id}/call"),
    ("GET", "/api/backup"),
    ("POST", "/api/backup/restore"),
    ("GET", "/api/categories"),
    ("GET", "/api/categories/{name}/export"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/settings/mcp-token"),
    ("GET", "/api/settings/read-token"),
    ("GET", "/api/settings/agent-token"),
    ("POST", "/api/settings/update-check"),
    ("GET", "/api/usage"),
    ("POST", "/api/server/restart"),
    ("DELETE", "/api/instances/{instance_id}"),
    ("GET", "/api/content"),
    ("GET", "/api/content/{instance_id}"),
    ("DELETE", "/api/content"),
    ("DELETE", "/api/content/{instance_id}"),
    ("DELETE", "/api/content/{instance_id}/{filename}"),
    ("GET", "/api/system/stats"),
}


class ValveNameTests(unittest.TestCase):
    """A write that cannot take effect must not answer "ok".

    `lifecycle` was sent inside `values`, where it belongs one level up. It was
    stored as a valve of that name, the loader skipped it (it is not on the
    Valves class), auto_start never moved — and the answer was `{"ok": true}`.
    """

    def setUp(self):
        # The suite also runs on the server, where a password *is* set. These
        # tests are about what the route does with the body, not about its auth —
        # same guard as ReadTokenTests. Second time this trap has been sprung.
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    def _config(self):
        from app.schema import MCPConfig, ServerConfig, ToolSourceConfig

        return MCPConfig(
            id="demo", name="Demo",
            server=ServerConfig(host="127.0.0.1", port=8199, endpoint="/mcp"),
            tool_source=ToolSourceConfig(path="tools/demo.json"),
            values={"api_url": "https://example.invalid", "timeout_seconds": 10},
        )

    VALVES = {"api_url", "timeout_seconds"}

    def _put(self, body, config=None, declared=VALVES):
        # Nothing reaches disk: the config is handed in and the writes are
        # patched away. The suite runs against the live installation.
        with patch("app.routes.instances.load_config", return_value=config or self._config()), \
             patch("app.routes.instances._valve_names_from_tool_file", return_value=declared), \
             patch("app.routes.instances.require_not_locked"), \
             patch("app.routes.instances.save_config"), \
             patch("app.routes.instances.set_instance_state"), \
             patch("app.routes.instances.get_instance_state", return_value=None):
            return TestClient(app).put("/api/instances/demo", json=body)

    def test_a_name_that_is_not_a_valve_is_refused(self):
        response = self._put({"values": {"lifecycle": {"auto_start": False}}})

        self.assertEqual(422, response.status_code)
        detail = response.json()["detail"]
        self.assertIn("'lifecycle'", detail)
        self.assertIn("api_url", detail)          # says what it *does* have

    def test_the_refusal_names_the_field_that_was_meant(self):
        detail = self._put({"values": {"auto_start": False}}).json()["detail"]

        self.assertIn("auto_start and restart_on_change are not valves", detail)

    def test_a_real_valve_still_goes_through(self):
        response = self._put({"values": {"timeout_seconds": 30}})

        self.assertEqual(200, response.status_code)

    def test_lifecycle_at_the_top_level_is_the_way_to_do_it(self):
        response = self._put({"lifecycle": {"auto_start": False}})

        self.assertEqual(200, response.status_code)

    def test_a_key_the_bug_already_wrote_is_still_refused(self):
        # The first version of this guard asked `cfg.values`, and the very key
        # it existed to catch was already sitting there — vouching for itself.
        # The valves come from the tool code now, which the bug never touched.
        polluted = self._config()
        polluted.values["lifecycle"] = {"auto_start": False}

        response = self._put({"values": {"lifecycle": {"auto_start": True}}}, config=polluted)

        self.assertEqual(422, response.status_code)

    def test_valves_that_cannot_be_determined_are_not_second_guessed(self):
        # Tool file missing, no Valves class, code that does not parse: that is
        # "cannot tell", not "has none", and must not turn into a refusal.
        response = self._put({"values": {"anything": 1}}, declared=None)

        self.assertEqual(200, response.status_code)


class ValveDiscoveryTests(unittest.TestCase):
    """Reading the valve names out of a tool — parsed, never executed."""

    def test_the_declared_valves_are_found(self):
        from app.tool_editor import valve_names

        code = (
            "from pydantic import BaseModel, Field\n"
            "class Tools:\n"
            "    class Valves(BaseModel):\n"
            "        api_url: str = Field(default='')\n"
            "        timeout_seconds: int = 10\n"
            "        plain = 'x'\n"
            "    def do(self): pass\n"
        )
        self.assertEqual({"api_url", "timeout_seconds", "plain"}, valve_names(code))

    def test_the_tool_code_is_never_executed(self):
        # A tool's module level runs on install and on start — not on a config
        # write. Parsing keeps a PUT from being an execution primitive.
        from app.tool_editor import valve_names

        code = (
            "raise SystemExit('module level ran')\n"
            "class Tools:\n"
            "    class Valves:\n"
            "        api_url: str = ''\n"
        )
        self.assertEqual({"api_url"}, valve_names(code))

    def test_what_cannot_be_read_comes_back_as_none(self):
        from app.tool_editor import valve_names

        self.assertIsNone(valve_names("def ("))                       # unparsable
        self.assertIsNone(valve_names("x = 1"))                       # no Tools
        self.assertIsNone(valve_names("class Tools:\n    pass\n"))    # no Valves


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
        import app as app_package

        self.assertEqual(app_package.__version__, app.version)
        self.assertEqual(app_package.__version__, app.openapi()["info"]["version"])

    def test_pyproject_derives_the_version_from_the_package(self):
        # One number, one place. Two would mean a build that reports itself
        # up to date while the update check compares against the other value.
        pyproject = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('version = {attr = "app.__version__"}', pyproject)
        self.assertNotRegex(pyproject, r'(?m)^version\s*=\s*"')

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
        fake_config = type("Config", (), {"locked": False, "venv": "default", "identity_mode": IdentityMode.off,
                            "forward_agent_token": False, "content": ContentConfig()})()
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
        fake_config = type("Config", (), {"locked": False, "venv": "default", "identity_mode": IdentityMode.off,
                            "forward_agent_token": False, "content": ContentConfig()})()
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


class ReadTokenTests(unittest.TestCase):
    """The GET-only API token: what it opens, and everything it must not."""

    # Every GET route under /api, split by what the read-only token may do with
    # it. The classification is asserted to be complete below, so a new GET
    # route fails the suite until someone decides which group it belongs to.
    READABLE = {
        "/api/auth-check",
        "/api/settings",
        "/api/instances",
        "/api/instances/{instance_id}",
        "/api/instances/{instance_id}/config",
        "/api/instances/{instance_id}/specs",
        "/api/instances/{instance_id}/tool-code",
        "/api/instances/{instance_id}/logs/install",
        "/api/instances/{instance_id}/logs/runtime",
        "/api/venvs",
        "/api/usage",
        "/api/tools/template",
        "/api/auth-status",
        # Who may do what is not a credential: the policy names accounts and
        # points at files, it never carries their content.
        "/api/identities",
        "/api/policy",
        # The agents and when they were issued a token — never the token, and
        # not even its hash. Issuing one is admin-only; reading the roster is
        # what a monitoring client wants.
        "/api/agent-identities",
        # File names and sizes, no file contents — the download route that does
        # serve content is not under /api and has its own per-file token.
        "/api/content",
        "/api/content/{instance_id}",
        # Load figures, not secrets — and monitoring is precisely what a
        # read-only token is for.
        "/api/system/stats",
        # Which categories exist and what is in them. The URL it names is
        # public knowledge; the token that opens it is not in the payload.
        "/api/categories",
    }
    # Hand out a credential verbatim — password only, even though they are GETs.
    ADMIN_ONLY = {
        # Carries every credential this server holds when asked for secrets —
        # the read token must not be able to fetch itself.
        "/api/backup",
        "/api/settings/mcp-token",
        "/api/settings/read-token",
        "/api/settings/agent-token",
        "/api/instances/{instance_id}/export",
        # Same payload shape as the per-instance export, same reason: it
        # carries the MCP Bearer token so the import works on arrival.
        "/api/categories/{name}/export",
    }

    PASSWORD = "admin-password"
    TOKEN = "read-only-token-xyz"

    def setUp(self):
        self.client = TestClient(app)
        # Rejected credentials are counted per address now, and every test
        # here shares one — start each with a clean slate.
        lockout.clear_all()
        self.original_hash = auth._password_hash
        self.original_token = os.environ.get("MCP_MANAGER_READ_TOKEN")
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        os.environ["MCP_MANAGER_READ_TOKEN"] = self.TOKEN

    def tearDown(self):
        auth._password_hash = self.original_hash
        if self.original_token is None:
            os.environ.pop("MCP_MANAGER_READ_TOKEN", None)
        else:
            os.environ["MCP_MANAGER_READ_TOKEN"] = self.original_token

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.TOKEN}"}

    @staticmethod
    def _fill(path):
        return (path.replace("{instance_id}", "no-such-instance")
                    .replace("{name}", "no-such-venv")
                    .replace("{sub}", "no-such-user"))

    def test_every_get_route_is_classified(self):
        actual = {
            route.path for route in app.routes
            if route.path.startswith("/api") and "GET" in route.methods
        }
        self.assertEqual(self.READABLE | self.ADMIN_ONLY, actual)

    def test_read_token_opens_get_routes(self):
        for path in sorted(self.READABLE):
            with self.subTest(path=path):
                status = self.client.get(self._fill(path), headers=self._headers()).status_code
                # 404 is fine — the point is that auth let the request through.
                self.assertNotIn(status, (401, 403), path)

    def test_read_token_is_refused_on_credential_routes(self):
        for path in sorted(self.ADMIN_ONLY):
            with self.subTest(path=path):
                response = self.client.get(self._fill(path), headers=self._headers())
                self.assertEqual(403, response.status_code, path)
                # …and the password still gets in (404 for the unknown instance).
                admin = self.client.get(self._fill(path), headers=self._headers(self.PASSWORD))
                self.assertNotIn(admin.status_code, (401, 403), path)

    def test_read_token_is_refused_on_every_mutating_route(self):
        # The whole design rests on "GET only". One route that accepts it with
        # another method would hand write access to a token that lives in clear
        # text inside instance configs.
        checked = 0
        for route in app.routes:
            if not route.path.startswith("/api"):
                continue
            for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
                with self.subTest(route=f"{method} {route.path}"):
                    response = self.client.request(
                        method, self._fill(route.path), headers=self._headers()
                    )
                    self.assertEqual(401, response.status_code, f"{method} {route.path}")
                checked += 1
        self.assertGreater(checked, 10)  # guard against an empty sweep

    def test_read_token_gets_the_full_instance_view_not_the_guest_one(self):
        # is_request_authenticated and require_auth share one parser; if they
        # drifted, the router would silently receive guest records without specs.
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "Example",
            "category": "Tests", "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp", "pid": 123, "error": None,
        })()
        fake_config = type("Config", (), {"locked": False, "venv": "default", "identity_mode": IdentityMode.off,
                            "forward_agent_token": False, "content": ContentConfig()})()
        with (
            patch("app.routes.instances.get_all_states", return_value=[fake_state]),
            patch("app.routes.instances.load_all_configs", return_value={"demo": fake_config}),
            patch("app.routes.instances._version_from_tool_file", return_value="1.0.0"),
            patch("app.routes.instances._specs_from_tool_file", return_value={"specs": [{"name": "x"}]}),
        ):
            listed = self.client.get(
                "/api/instances?include=specs", headers=self._headers()
            ).json()[0]
        self.assertIn("port", listed)
        self.assertEqual([{"name": "x"}], listed["specs"])

    def test_unset_read_token_authenticates_nobody(self):
        os.environ.pop("MCP_MANAGER_READ_TOKEN", None)
        for token in (self.TOKEN, "", " "):
            with self.subTest(token=repr(token)):
                self.assertEqual(
                    401,
                    self.client.get("/api/auth-check", headers=self._headers(token or "x")).status_code,
                )
        self.assertFalse(auth.verify_read_token(""))

    def test_settings_reports_whether_a_read_token_is_set(self):
        data = self.client.get("/api/settings", headers=self._headers()).json()
        self.assertTrue(data["read_token_set"])
        os.environ.pop("MCP_MANAGER_READ_TOKEN", None)
        data = self.client.get("/api/settings", headers=self._headers(self.PASSWORD)).json()
        self.assertFalse(data["read_token_set"])

    def test_read_token_cannot_be_set_to_the_password(self):
        new_pw = "fresh-password"
        for body in (
            # …neither the password in force
            {"read_token": self.PASSWORD},
            # …nor the one being set in the very same request
            {"read_token": new_pw, "password": new_pw, "password_confirm": new_pw,
             "current_password": self.PASSWORD},
        ):
            with self.subTest(body=body):
                with patch("app.auth.set_read_token") as setter:
                    response = self.client.put(
                        "/api/settings", json=body, headers=self._headers(self.PASSWORD)
                    )
                self.assertEqual(400, response.status_code, response.text)
                setter.assert_not_called()

    def test_read_token_is_rejected_when_too_short(self):
        with patch("app.auth.set_read_token") as setter:
            response = self.client.put(
                "/api/settings", json={"read_token": "short"}, headers=self._headers(self.PASSWORD)
            )
        self.assertEqual(400, response.status_code)
        setter.assert_not_called()

    def test_read_token_can_be_set_and_cleared(self):
        with patch("app.settings_store.save_settings"):
            response = self.client.put(
                "/api/settings", json={"read_token": "brand-new-token"},
                headers=self._headers(self.PASSWORD),
            )
            self.assertIn("read_token", response.json()["changed"])
            self.assertEqual("brand-new-token", auth.read_token())

            response = self.client.put(
                "/api/settings", json={"read_token_clear": True},
                headers=self._headers(self.PASSWORD),
            )
            self.assertIn("read_token_cleared", response.json()["changed"])
            self.assertIsNone(auth.read_token())


class AgentTokenTests(unittest.TestCase):
    """The read/write API token: it may work, it may not promote itself."""

    # Password-only despite a valid agent token. The GETs hand out a credential
    # verbatim; PUT /api/settings sets the password and both API tokens, so an
    # agent token reaching it could make itself admin.
    ADMIN_ONLY = {
        ("GET", "/api/settings/mcp-token"),
        ("GET", "/api/settings/read-token"),
        ("GET", "/api/settings/agent-token"),
        ("GET", "/api/instances/{instance_id}/export"),
        ("GET", "/api/categories/{name}/export"),
        ("PUT", "/api/settings"),
        # Assigning an account to a person hands them that account's data.
        ("PUT", "/api/policy"),
        # Issuing an agent identity mints a credential, and POST
        # /api/agent-identities takes no id — without this line the sweep
        # would write a junk identity into the running installation's store,
        # the same way DELETE /api/content once emptied its file storage.
        ("POST", "/api/agent-identities"),
        ("POST", "/api/agent-identities/{sub}/token"),
        ("PUT", "/api/agent-identities/{sub}"),
        ("DELETE", "/api/agent-identities/{sub}"),
        # Runs the instance's real code with the instance's real credentials —
        # the one route here that does something outside this process. The
        # mandatory instance id makes the sweep hit a 404 first, but the door
        # is the point: a read-only or agent token must not be able to make the
        # server call a tool on its behalf.
        ("POST", "/api/instances/{instance_id}/call"),
        ("GET", "/api/backup"),
        # The dangerous one. It takes no id, so nothing upstream turns it into
        # a harmless 404 — the same shape as DELETE /api/content, which once
        # emptied the live file storage. Admin-only means the sweep stops at
        # the door; the payload check behind it is the second lock.
        ("POST", "/api/backup/restore"),
    }

    PASSWORD = "admin-password"
    AGENT = "agent-token-abcdef"
    READ = "read-token-abcdef"

    def setUp(self):
        self.client = TestClient(app)
        lockout.clear_all()
        self.original_hash = auth._password_hash
        self.original = {k: os.environ.get(k) for k in
                         ("MCP_MANAGER_AGENT_TOKEN", "MCP_MANAGER_READ_TOKEN")}
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        os.environ["MCP_MANAGER_AGENT_TOKEN"] = self.AGENT
        os.environ["MCP_MANAGER_READ_TOKEN"] = self.READ

    def tearDown(self):
        auth._password_hash = self.original_hash
        for key, value in self.original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.AGENT}"}

    @staticmethod
    def _fill(path):
        return path.replace("{instance_id}", "no-such-instance").replace("{name}", "no-such-venv")

    def test_agent_token_opens_every_route_except_the_admin_ones(self):
        # Restart, update-check and the content deletions are patched: they
        # would act on the real world, and this sweep is about the door, not
        # the room behind it. DELETE /api/content is the one route here that
        # takes no instance id, so nothing upstream turns it into a harmless
        # 404 — unpatched, this sweep empties the running installation's file
        # storage, and the suite also runs on the server.
        with (
            patch("app.routes.settings._restart_after_delay"),
            patch("app.routes.settings.check_manually", AsyncMock(return_value={"ok": True})),
            patch("app.routes.content.clear_all", return_value=0),
            patch("app.routes.content.clear_instance", return_value=0),
            patch("app.routes.content.delete_file", return_value=True),
        ):
            checked = 0
            for route in app.routes:
                if not route.path.startswith("/api"):
                    continue
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    admin_only = (method, route.path) in self.ADMIN_ONLY
                    with self.subTest(route=f"{method} {route.path}"):
                        response = self.client.request(
                            method, self._fill(route.path), headers=self._headers()
                        )
                        if admin_only:
                            self.assertEqual(403, response.status_code, f"{method} {route.path}")
                        else:
                            # 404/422 is fine — the point is that auth passed.
                            self.assertNotIn(response.status_code, (401, 403),
                                             f"{method} {route.path}")
                    checked += 1
            self.assertGreater(checked, 25)  # guard against an empty sweep

    def test_every_admin_only_route_is_registered(self):
        # If a route in the list above is renamed away, the sweep would still
        # pass while silently testing nothing.
        registered = {(m, r.path) for r in app.routes
                      if r.path.startswith("/api") for m in r.methods}
        self.assertTrue(self.ADMIN_ONLY <= registered, self.ADMIN_ONLY - registered)

    def test_agent_token_cannot_change_the_password(self):
        # The escalation this whole split stands or falls on.
        with patch("app.auth.set_password") as set_pw:
            response = self.client.put(
                "/api/settings",
                json={"password": "hijacked", "password_confirm": "hijacked"},
                headers=self._headers(),
            )
        self.assertEqual(403, response.status_code)
        set_pw.assert_not_called()
        self.assertTrue(auth.verify_password(self.PASSWORD))

    def test_agent_token_cannot_rewrite_the_api_tokens(self):
        with patch("app.auth.set_agent_token") as setter:
            response = self.client.put(
                "/api/settings", json={"agent_token": "a-token-of-my-own"},
                headers=self._headers(),
            )
        self.assertEqual(403, response.status_code)
        setter.assert_not_called()

    def test_unset_agent_token_authenticates_nobody(self):
        os.environ.pop("MCP_MANAGER_AGENT_TOKEN", None)
        self.assertEqual(
            401,
            self.client.post("/api/instances/x/start", headers=self._headers()).status_code,
        )
        self.assertFalse(auth.verify_agent_token(""))

    def test_the_two_api_tokens_must_differ(self):
        for body in (
            {"agent_token": self.READ},                       # agent := existing read
            {"read_token": self.AGENT},                       # read := existing agent
            {"agent_token": "same-token-value", "read_token": "same-token-value"},
        ):
            with self.subTest(body=body):
                with (
                    patch("app.auth.set_agent_token") as set_agent,
                    patch("app.auth.set_read_token") as set_read,
                ):
                    response = self.client.put(
                        "/api/settings", json=body, headers=self._headers(self.PASSWORD)
                    )
                self.assertEqual(400, response.status_code, response.text)
                set_agent.assert_not_called()
                set_read.assert_not_called()

    def test_agent_token_can_be_set_and_cleared(self):
        with patch("app.settings_store.save_settings"):
            response = self.client.put(
                "/api/settings", json={"agent_token": "brand-new-agent"},
                headers=self._headers(self.PASSWORD),
            )
            self.assertIn("agent_token", response.json()["changed"])
            self.assertEqual("brand-new-agent", auth.agent_token())

            response = self.client.put(
                "/api/settings", json={"agent_token_clear": True},
                headers=self._headers(self.PASSWORD),
            )
            self.assertIn("agent_token_cleared", response.json()["changed"])
            self.assertIsNone(auth.agent_token())

    def test_settings_reports_whether_an_agent_token_is_set(self):
        data = self.client.get("/api/settings", headers=self._headers()).json()
        self.assertTrue(data["agent_token_set"])


class BundledVersionTests(unittest.TestCase):
    """Installed tool against the copy shipped in examples/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.examples = pathlib.Path(self.tmp.name)
        self.real_examples = pathlib.Path(__file__).parent.parent / "examples"
        self.patcher = patch.object(api_helpers, "EXAMPLES_DIR", self.examples)
        self.patcher.start()
        self._reset_caches()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()
        self._reset_caches()

    @staticmethod
    def _reset_caches():
        api_helpers._examples_index = (-1.0, {})
        api_helpers._version_cache.clear()

    def _ship(self, tool_id, version, name=None, with_code=True):
        entry = {"id": tool_id, "specs": []}
        if with_code:
            entry["content"] = f'"""\ntitle: T\nversion: {version}\n"""\n\nclass Tools: pass\n'
        (self.examples / (name or f"{tool_id}.json")).write_text(json.dumps([entry]))
        self._reset_caches()  # a fresh temp dir can share an mtime second

    def _info(self, instance_id, installed):
        cfg = type("Config", (), {"id": instance_id})()
        with patch.object(api_helpers, "_version_from_tool_file", return_value=installed):
            return api_helpers._bundled_version_info(cfg)

    def test_nothing_shipped_under_that_id_reports_nothing(self):
        self._ship("other_tool", "1.0.0")
        self.assertIsNone(self._info("my_tool", "0.1.0"))

    def test_newer_shipped_version_is_reported_with_its_path(self):
        self._ship("my_tool", "0.0.4")
        info = self._info("my_tool", "0.0.3")
        self.assertTrue(info["update_available"])
        self.assertEqual("0.0.4", info["version"])
        self.assertTrue(info["path"].endswith("my_tool.json"), info["path"])

    def test_equal_or_older_shipped_version_is_not_an_update(self):
        for shipped, installed in (("0.0.3", "0.0.3"), ("0.0.3", "0.0.4")):
            with self.subTest(shipped=shipped, installed=installed):
                self._ship("my_tool", shipped)
                info = self._info("my_tool", installed)
                # Still reported, so the dialog can stay silent by itself.
                self.assertFalse(info["update_available"])

    def test_versions_are_compared_numerically_not_as_strings(self):
        # "0.0.10" sorts before "0.0.9" lexicographically — the whole reason
        # this goes through packaging.version.
        self._ship("my_tool", "0.0.10")
        self.assertTrue(self._info("my_tool", "0.0.9")["update_available"])
        self._ship("my_tool", "0.0.9")
        self.assertFalse(self._info("my_tool", "0.0.10")["update_available"])

    def test_mcp_connection_samples_do_not_claim_the_id(self):
        # examples/ also ships OWUI *connection* JSONs; they carry an id but no
        # code, and must not shadow a real tool of the same name.
        self._ship("my_tool", "", with_code=False)
        self.assertIsNone(self._info("my_tool", "0.0.3"))

    def test_a_broken_sample_does_not_take_the_index_with_it(self):
        (self.examples / "broken.json").write_text("{not json")
        self._ship("my_tool", "0.0.4")
        self.assertTrue(self._info("my_tool", "0.0.3")["update_available"])

    def test_the_polled_list_carries_the_marker_but_guests_do_not_get_it(self):
        self._ship("demo", "0.0.4")
        client = TestClient(app)
        original_hash = auth._password_hash
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "", "category": "",
            "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp", "pid": 1, "error": None,
        })()
        fake_config = type("Config", (), {"id": "demo", "locked": False, "venv": "default",
                                          "identity_mode": IdentityMode.off,
                                          "forward_agent_token": False,
                                          "content": ContentConfig()})()
        try:
            with (
                patch("app.routes.instances.get_all_states", return_value=[fake_state]),
                patch("app.routes.instances.load_all_configs", return_value={"demo": fake_config}),
                patch("app.routes.instances._version_from_tool_file", return_value="0.0.3"),
                patch.object(api_helpers, "_version_from_tool_file", return_value="0.0.3"),
            ):
                auth._password_hash = None
                row = client.get("/api/instances").json()[0]
                self.assertEqual("0.0.4", row["bundled_update"])

                # Guest view is an allowlist, so the marker stays behind the login
                # together with every other detail.
                auth._password_hash = hashlib.sha256(b"pw").hexdigest()
                guest = client.get("/api/instances").json()[0]
                self.assertNotIn("bundled_update", guest)
        finally:
            auth._password_hash = original_hash

    def _update_call(self, instance_id="demo", locked=False):
        cfg = type("Config", (), {"id": instance_id, "locked": locked})()
        client = TestClient(app)
        original_hash = auth._password_hash
        auth._password_hash = None
        try:
            with (
                patch("app.routes.tools.load_config", return_value=cfg),
                patch("app.api_helpers.load_config", return_value=cfg),
                patch("app.api_helpers._version_from_tool_file", return_value="0.0.3"),
                patch("app.routes.tools.save_tool_code",
                      AsyncMock(return_value={"ok": True, "restarted": True, "warnings": []})) as saver,
            ):
                response = client.post(f"/api/instances/{instance_id}/update-from-example")
            return response, saver
        finally:
            auth._password_hash = original_hash

    def test_update_from_example_applies_the_shipped_code(self):
        self._ship("demo", "0.0.4")
        response, saver = self._update_call()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("0.0.4", response.json()["version"])
        # Delegated, not reimplemented: the save path does backup, validation,
        # dependency install, valve sync and restart.
        self.assertEqual(1, saver.await_count)
        self.assertIn("version: 0.0.4", saver.await_args.args[1]["code"])

    def test_update_from_example_refuses_to_downgrade(self):
        # The badge is the only way in, so this can only be hit by a stale page
        # or a hand-made request — either way it must not roll the tool back.
        self._ship("demo", "0.0.2")
        response, saver = self._update_call()
        self.assertEqual(409, response.status_code)
        saver.assert_not_awaited()

    def test_update_from_example_needs_a_shipped_tool(self):
        self._ship("something_else", "9.9.9")
        response, saver = self._update_call()
        self.assertEqual(404, response.status_code)
        saver.assert_not_awaited()

    def test_update_from_example_respects_the_instance_lock(self):
        self._ship("demo", "0.0.4")
        response, saver = self._update_call(locked=True)
        self.assertEqual(403, response.status_code)
        saver.assert_not_awaited()

    def test_the_shipped_examples_are_matched_by_their_real_ids(self):
        # Guards the actual repo files: a renamed id here would silently switch
        # the hint off for everyone who installed the tool.
        with patch.object(api_helpers, "EXAMPLES_DIR", self.real_examples):
            self._reset_caches()
            index = api_helpers._examples_by_id()
        self.assertIn("mcp_tool_router", index)
        self.assertIn("mcp_manager_control", index)
        self.assertIn("identity_probe", index)


class SpecsEndpointTests(unittest.TestCase):
    """The function catalog read out of the tool JSON (no exec, no running instance)."""

    TOOL_JSON = {
        "content": "class Tools: pass",
        "specs": [
            {
                "name": "get_items",
                "description": "List items.",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
            {"name": "no_params"},
            {"description": "nameless entries are dropped"},
        ],
        "meta": {"description": "A demo tool."},
    }

    def setUp(self):
        self.client = TestClient(app)
        lockout.clear_all()
        self.original_hash = auth._password_hash
        auth._password_hash = None
        self.tmp = tempfile.TemporaryDirectory()
        self.tool_path = pathlib.Path(self.tmp.name) / "demo.json"
        self.tool_path.write_text(json.dumps(self.TOOL_JSON))
        api_helpers._specs_cache.clear()

    def tearDown(self):
        auth._password_hash = self.original_hash
        api_helpers._specs_cache.clear()
        self.tmp.cleanup()

    def _config(self):
        from app.schema import MCPConfig, ServerConfig, ToolSourceConfig

        return MCPConfig(
            id="demo", name="Demo", description="config fallback",
            server=ServerConfig(port=8123),
            tool_source=ToolSourceConfig(path=str(self.tool_path)),
        )

    def test_specs_endpoint_carries_the_bundled_block(self):
        # The info dialog reads the comparison from here; None is the normal
        # answer for a tool that does not ship with the spawner.
        with patch("app.routes.instances.load_config", return_value=self._config()):
            payload = self.client.get("/api/instances/demo/specs").json()
        self.assertIn("bundled", payload)
        self.assertIsNone(payload["bundled"])

        with (
            patch("app.routes.instances.load_config", return_value=self._config()),
            patch("app.routes.instances._bundled_version_info",
                  return_value={"version": "9.9.9", "path": "examples/demo.json",
                                "update_available": True}),
        ):
            payload = self.client.get("/api/instances/demo/specs").json()
        self.assertTrue(payload["bundled"]["update_available"])
        self.assertEqual("9.9.9", payload["bundled"]["version"])

    def test_specs_endpoint_serves_names_descriptions_and_schemas(self):
        with (
            patch("app.routes.instances.load_config", return_value=self._config()),
            patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
        ):
            data = self.client.get("/api/instances/demo/specs").json()
        self.assertEqual("demo", data["id"])
        self.assertEqual("A demo tool.", data["description"])
        self.assertEqual(["get_items", "no_params"], [s["name"] for s in data["specs"]])
        self.assertEqual("List items.", data["specs"][0]["description"])
        self.assertEqual("object", data["specs"][0]["parameters"]["type"])
        # Entries without parameters still carry the key, so the UI needn't guess
        self.assertEqual({}, data["specs"][1]["parameters"])

    def test_missing_or_broken_tool_file_yields_an_empty_catalog(self):
        for content in (None, "{ not json", json.dumps({"content": "x"})):
            with self.subTest(content=content):
                api_helpers._specs_cache.clear()
                if content is None:
                    self.tool_path.unlink(missing_ok=True)
                else:
                    self.tool_path.write_text(content)
                with (
                    patch("app.routes.instances.load_config", return_value=self._config()),
                    patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
                ):
                    response = self.client.get("/api/instances/demo/specs")
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual([], response.json()["specs"])
                # Falls back to the config description instead of going blank
                self.assertEqual("config fallback", response.json()["description"])

    def test_specs_endpoint_is_404_for_unknown_instance(self):
        with patch("app.routes.instances.load_config", return_value=None):
            self.assertEqual(404, self.client.get("/api/instances/nope/specs").status_code)

    def test_specs_endpoint_requires_authentication(self):
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        self.assertEqual(401, self.client.get("/api/instances/demo/specs").status_code)

    def test_specs_endpoint_stays_open_in_readonly_and_no_code_edit_modes(self):
        # Metadata, not source: the info dialog must survive both edit locks.
        original = os.environ.get("MCP_EDIT_MODE")
        try:
            for mode in ("readonly", "upload"):
                os.environ["MCP_EDIT_MODE"] = mode
                with (
                    patch("app.routes.instances.load_config", return_value=self._config()),
                    patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
                ):
                    response = self.client.get("/api/instances/demo/specs")
                self.assertEqual(200, response.status_code, mode)
        finally:
            if original is None:
                os.environ.pop("MCP_EDIT_MODE", None)
            else:
                os.environ["MCP_EDIT_MODE"] = original

    def _list(self, url, headers=None):
        fake_state = type("State", (), {
            "id": "demo", "name": "Demo", "description": "Example",
            "category": "Tests", "status": type("Status", (), {"value": "running"})(),
            "port": 8123, "host": "127.0.0.1", "endpoint": "/mcp",
            "pid": 123, "error": None,
        })()
        cfg = self._config()
        with (
            patch("app.routes.instances.get_all_states", return_value=[fake_state]),
            patch("app.routes.instances.load_all_configs", return_value={"demo": cfg}),
            patch("app.api_helpers.resolve_tool_path", return_value=self.tool_path),
        ):
            return self.client.get(url, headers=headers or {}).json()[0]

    def test_list_payload_is_unchanged_without_include_specs(self):
        # The hot path: the UI polls /api/instances every few seconds per tab.
        # Specs are fat and must never sneak into the default payload.
        plain = self._list("/api/instances")
        self.assertNotIn("specs", plain)
        self.assertEqual(plain, self._list("/api/instances?include=version"))
        with_specs = self._list("/api/instances?include=specs")
        self.assertEqual(plain, {k: v for k, v in with_specs.items() if k != "specs"})
        self.assertEqual(["get_items", "no_params"], [s["name"] for s in with_specs["specs"]])

    def test_guests_never_get_specs_even_when_asking_for_them(self):
        auth._password_hash = hashlib.sha256(b"pw").hexdigest()
        guest = self._list("/api/instances?include=specs")
        self.assertNotIn("specs", guest)
        admin = self._list("/api/instances?include=specs", {"Authorization": "Bearer pw"})
        self.assertIn("specs", admin)


if __name__ == "__main__":
    unittest.main()
