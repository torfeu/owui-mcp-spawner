"""The test call from the dashboard — the client, and the door in front of it.

Three things carry this. The call can be made *as* somebody, or the panel is
useless on exactly the instances that require an identity; the identity it
sends is one this server put together, never one the browser claimed, or the
access rules would be tested against a fiction. And the route is closed to
everything but the password: it runs the instance's own code with the
instance's own credentials, which no read-only or agent token may ask for.

Every test that touches the identity roster or the agent store points them at
temp files. The suite runs on the server as well, where both exist.
"""
import asyncio
import contextlib
import hashlib
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.auth as auth
import app.tool_call as tool_call
from app import agent_identity, identity_registry
from app.admin_server import app
from app.identity import IdentityError, verify_user_jwt
from app.schema import MCPInstance, MCPStatus

SECRET = "shared-secret-for-tests"


def instance(instance_id="demo", status=MCPStatus.running, port=8101):
    return MCPInstance(id=instance_id, name=instance_id, status=status, pid=4242,
                       host="127.0.0.1", port=port, endpoint="/mcp")


def run(coro):
    return asyncio.run(coro)


class IdentityHeaderTests(unittest.TestCase):
    """What the runner on the other end gets to see."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("MCP_USER_TRUST_HEADERS", None)

    def test_no_identity_asked_for_means_no_identity_headers(self):
        # The health check's behaviour, kept as the default: a call carries the
        # Bearer token and nothing else unless somebody was chosen.
        self.assertEqual({}, tool_call._identity_headers(None))
        self.assertEqual({}, tool_call._identity_headers({"sub": "  "}))

    def test_a_chosen_user_arrives_as_a_verifiable_token(self):
        headers = tool_call._identity_headers(
            {"sub": "u-1", "email": "ann@example.org", "name": "Ann", "role": "user"})
        who = verify_user_jwt(headers["X-OpenWebUI-User-Jwt"], SECRET)
        self.assertEqual("u-1", who.sub)
        self.assertEqual("ann@example.org", who.email)
        self.assertEqual("user", who.role)
        # Short-lived on purpose: nothing is meant to reuse this token.
        self.assertLessEqual(who.expires_at - who.issued_at, tool_call.CALL_TIMEOUT + 1)

    def test_the_unsigned_fallback_is_only_used_where_it_is_trusted(self):
        with patch.dict(os.environ, {"MCP_USER_TRUST_HEADERS": "1"}):
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            headers = tool_call._identity_headers({"sub": "u-1", "name": "Ann"})
        self.assertEqual("u-1", headers["X-OpenWebUI-User-Id"])
        self.assertEqual("Ann", headers["X-OpenWebUI-User-Name"])

    def test_without_a_secret_or_trusted_headers_the_reason_is_named(self):
        # Not a silent "access denied" from the far end — the panel has to be
        # able to say why calling as someone is impossible here.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            os.environ.pop("MCP_USER_TRUST_HEADERS", None)
            self.assertFalse(tool_call.identity_possible())
            with self.assertRaises(IdentityError):
                tool_call._identity_headers({"sub": "u-1"})


class UnpackTests(unittest.TestCase):
    """The answer as the chat would have received it, plus its true length."""

    @staticmethod
    def result(blocks, is_error=False, structured=None):
        text_block = lambda t: type("Block", (), {"type": "text", "text": t})()
        return type("Result", (), {
            "content": [text_block(b) if isinstance(b, str) else b for b in blocks],
            "is_error": is_error, "structured_content": structured,
        })()

    def test_text_blocks_are_joined_and_counted(self):
        out = tool_call._unpack(self.result(["one", "two"]))
        self.assertEqual("one\ntwo", out["text"])
        self.assertEqual(7, out["chars"])
        self.assertFalse(out["truncated"])
        self.assertFalse(out["is_error"])

    def test_an_oversized_answer_is_cut_but_still_counted_honestly(self):
        # The length is the finding here — a tool whose default answer is half a
        # megabyte is why the local model keeps getting truncated input.
        out = tool_call._unpack(self.result(["x" * (tool_call.MAX_TEXT + 500)]))
        self.assertEqual(tool_call.MAX_TEXT, len(out["text"]))
        self.assertEqual(tool_call.MAX_TEXT + 500, out["chars"])
        self.assertTrue(out["truncated"])

    def test_a_non_text_block_is_named_rather_than_rendered(self):
        image = type("Block", (), {"type": "image", "data": "…"})()
        self.assertEqual("<image>", tool_call._unpack(self.result([image]))["text"])

    def test_a_tool_that_reported_a_failure_is_marked_as_one(self):
        out = tool_call._unpack(self.result(["boom"], is_error=True))
        self.assertTrue(out["is_error"])

    def test_structured_output_is_passed_through_only_when_there_is_some(self):
        self.assertIsNone(tool_call._unpack(self.result(["x"]))["structured"])
        self.assertEqual({"a": 1},
                         tool_call._unpack(self.result(["x"], structured={"a": 1}))["structured"])


def fake_transport(*, init_name="demo", on_call=None, raises=None):
    """Stand in for the MCP client the way health.probe() uses it.

    Patched at `mcp`, not inside tool_call: the imports there are local, so the
    module attributes are what the call actually reaches for.
    """
    calls = []

    class Session:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return type("Init", (), {"server_info": type("I", (), {"name": init_name})()})()

        async def call_tool(self, name, arguments, read_timeout_seconds=None):
            calls.append((name, arguments, read_timeout_seconds))
            if raises:
                raise raises
            return on_call(name, arguments) if on_call else UnpackTests.result(["ok"])

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs, None))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    @contextlib.asynccontextmanager
    async def streams(url, http_client=None):
        calls.append(("url", url, None))
        yield (None, None, None)

    import mcp
    import mcp.client.streamable_http as transport
    return calls, patch.multiple(mcp, ClientSession=Session), patch.multiple(
        transport, streamable_http_client=streams), patch.object(
        transport.httpx2, "AsyncClient", Client)


class CallTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_plain_call_reaches_the_tool_and_reports_the_answer(self):
        calls, *patches = fake_transport()
        with patches[0], patches[1], patches[2]:
            out = run(tool_call.call(instance(), "hello", {"name": "world"}))
        self.assertTrue(out["ok"])
        self.assertEqual("ok", out["text"])
        self.assertIn(("hello", {"name": "world"}, tool_call.CALL_TIMEOUT), calls)

    def test_the_chosen_identity_travels_with_the_request(self):
        calls, *patches = fake_transport()
        with patches[0], patches[1], patches[2]:
            run(tool_call.call(instance(), "hello", {}, {"sub": "u-1", "role": "admin"}))
        headers = next(c[1]["headers"] for c in calls if c[0] == "client")
        who = verify_user_jwt(headers["X-OpenWebUI-User-Jwt"], SECRET)
        self.assertEqual("u-1", who.sub)
        self.assertEqual("admin", who.role)

    def test_an_instance_answering_under_another_name_is_refused(self):
        # Ports get recycled. Without the guard the most confusing possible
        # answer would come back: a neighbour's tool, reported as this one's.
        calls, *patches = fake_transport(init_name="somebody-else")
        with patches[0], patches[1], patches[2]:
            out = run(tool_call.call(instance(), "hello", {}))
        self.assertFalse(out["ok"])
        self.assertIn("somebody-else", out["error"])
        self.assertNotIn(("hello", {}, tool_call.CALL_TIMEOUT), calls)

    def test_a_transport_failure_names_the_cause_not_the_task_group(self):
        group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                               [OSError("All connection attempts failed")])
        calls, *patches = fake_transport(raises=group)
        with patches[0], patches[1], patches[2]:
            out = run(tool_call.call(instance(), "hello", {}))
        self.assertFalse(out["ok"])
        self.assertEqual("All connection attempts failed", out["error"])

    def test_an_impossible_identity_fails_before_anything_is_dialled(self):
        os.environ.pop("MCP_USER_JWT_SECRET", None)
        os.environ.pop("MCP_USER_TRUST_HEADERS", None)
        calls, *patches = fake_transport()
        with patches[0], patches[1], patches[2]:
            out = run(tool_call.call(instance(), "hello", {}, {"sub": "u-1"}))
        self.assertFalse(out["ok"])
        self.assertEqual([], calls)


class CallRouteTests(unittest.TestCase):
    PASSWORD = "admin-password"

    def setUp(self):
        self.client = TestClient(app)
        # Set explicitly: a route test that assumes no password is set is green
        # here and red on the server, where one is.
        original = auth._password_hash
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "MCP_AGENT_IDENTITIES_FILE": str(root / "agent_identities.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(agent_identity.ENV_IDENTITIES, None)
        agent_identity._file_cache.clear()
        self.addCleanup(agent_identity._file_cache.clear)
        # The roster is read to fill in a caller's claims; on the server it has
        # rows, here it must not matter what the machine happens to hold.
        registry = patch.object(identity_registry, "DB_PATH", root / "identities.db")
        registry.start()
        self.addCleanup(registry.stop)

    @property
    def admin(self):
        return {"Authorization": f"Bearer {self.PASSWORD}"}

    def post(self, body, instance_id="demo", **kwargs):
        return self.client.post(f"/api/instances/{instance_id}/call", json=body,
                                headers=self.admin, **kwargs)

    def running(self, **kwargs):
        return patch("app.routes.instances.get_instance_state",
                     return_value=instance(**kwargs))

    def test_an_unknown_instance_is_a_404_and_calls_nothing(self):
        with (patch("app.routes.instances.get_instance_state", return_value=None),
              patch("app.tool_call.call") as called):
            self.assertEqual(404, self.post({"tool": "hello"}).status_code)
        called.assert_not_called()

    def test_a_stopped_instance_is_refused_with_the_reason(self):
        with self.running(status=MCPStatus.stopped):
            response = self.post({"tool": "hello"})
        self.assertEqual(409, response.status_code)
        self.assertIn("not running", response.json()["detail"])

    def test_a_locked_instance_is_left_alone(self):
        # The lock means "do not touch this one"; a call runs its real code.
        with (self.running(),
              patch("app.api_helpers.load_config",
                    return_value=type("Cfg", (), {"locked": True})())):
            self.assertEqual(403, self.post({"tool": "hello"}).status_code)

    def test_a_call_without_a_tool_or_with_junk_arguments_is_refused(self):
        with self.running():
            self.assertEqual(422, self.post({"tool": " "}).status_code)
            self.assertEqual(422, self.post({"tool": "hello",
                                             "arguments": ["nope"]}).status_code)

    def test_the_answer_is_handed_back_with_what_was_asked(self):
        async def fake_call(inst, tool, arguments, who=None):
            return {"ok": True, "is_error": False, "text": "hi", "chars": 2,
                    "duration_ms": 12, "error": ""}

        with self.running(), patch("app.tool_call.call", fake_call):
            body = self.post({"tool": "hello", "arguments": {"a": 1}}).json()
        self.assertEqual("demo", body["instance"])
        self.assertEqual("hello", body["tool"])
        self.assertEqual("hi", body["text"])

    def test_the_claims_come_from_the_server_not_from_the_request(self):
        # The browser says *who* to call as. What that identity consists of is
        # looked up here — otherwise the dialog could hand a tool a role its
        # owner does not have, and the rules would be tested against a fiction.
        seen = {}

        async def fake_call(inst, tool, arguments, who=None):
            seen.update(who or {})
            return {"ok": True, "duration_ms": 1, "error": ""}

        agent_identity.create("codex", name="Codex", role="KI")
        with self.running(), patch("app.tool_call.call", fake_call):
            self.post({"tool": "hello", "as_user": "codex", "role": "admin"})
        self.assertEqual({"sub": "codex", "email": "", "name": "Codex", "role": "KI"}, seen)

    def test_an_unknown_user_may_still_be_called_as(self):
        # "What would somebody with no rules see?" is a question worth asking,
        # and the rights dialog already lets a rule name a user who never came.
        seen = {}

        async def fake_call(inst, tool, arguments, who=None):
            seen.update(who or {})
            return {"ok": True, "duration_ms": 1, "error": ""}

        with self.running(), patch("app.tool_call.call", fake_call):
            self.post({"tool": "hello", "as_user": "never-seen"})
        self.assertEqual("never-seen", seen["sub"])

    def test_the_route_is_closed_without_the_password(self):
        with (self.running(), patch("app.tool_call.call") as called):
            response = self.client.post("/api/instances/demo/call", json={"tool": "hello"})
        self.assertEqual(401, response.status_code)
        called.assert_not_called()


class SpecsHintTests(unittest.TestCase):
    """The info dialog has to know why a button is missing before it is clicked."""

    PASSWORD = "admin-password"

    def setUp(self):
        self.client = TestClient(app)
        original = auth._password_hash
        auth._password_hash = hashlib.sha256(self.PASSWORD.encode()).hexdigest()
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    def test_the_specs_payload_says_what_a_test_call_could_do(self):
        from app.schema import ContentConfig, IdentityMode
        cfg = type("Cfg", (), {"id": "demo", "name": "Demo", "description": "",
                               "locked": True, "identity_mode": IdentityMode.required,
                               "content": ContentConfig()})()
        with (patch("app.routes.instances.load_config", return_value=cfg),
              patch("app.routes.instances._specs_from_tool_file",
                    return_value={"description": "", "specs": []}),
              patch("app.routes.instances._bundled_version_info", return_value=None),
              patch("app.routes.instances.read_usage", return_value=None),
              patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET})):
            body = self.client.get("/api/instances/demo/specs",
                                   headers={"Authorization": f"Bearer {self.PASSWORD}"}).json()
        self.assertEqual({"identity_mode": "required", "can_identify": True, "locked": True},
                         body["test_call"])


if __name__ == "__main__":
    unittest.main()
