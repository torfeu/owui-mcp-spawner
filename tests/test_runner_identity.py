"""The runner's gate: which caller gets to run which tool.

app/mcp_runner.py reads the forwarded user token off the HTTP request that
carried the current MCP message, verifies it, and decides. These tests drive
the real handlers built by build_server() with a faked request context, so what
is tested is the decision itself and not a re-implementation of it.

The case that matters most is the last one in AccessTests: a tool hidden from
the listing must also be refused when it is called by name. Filtering a listing
is presentation; this is the boundary.
"""
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from mcp import types
from mcp.server.lowlevel.server import ServerRequestContext

import app.policy as policy
from app.identity import get_current_identity
from app.mcp_runner import _resolve_identity, build_server
from app.schema import IdentityMode, MachineIdentity, MCPConfig, ServerConfig, ToolSourceConfig
from tests.test_identity import SECRET, mint

TOOL_DEFS = [
    {"name": "read_item", "description": "read", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "delete_item", "description": "delete", "inputSchema": {"type": "object", "properties": {}}},
]


class DemoTools:
    """Two tools, one async and one sync — the sync one goes through
    asyncio.to_thread, which is where a context that does not propagate would
    show up as a silent "no user"."""

    async def read_item(self):
        who = get_current_identity()
        return f"read by {who.sub if who else 'nobody'}"

    def delete_item(self):
        who = get_current_identity()
        return f"deleted by {who.sub if who else 'nobody'}"


def make_config(instance_id="inst", mode=IdentityMode.off) -> MCPConfig:
    return MCPConfig(
        id=instance_id, name=instance_id,
        server=ServerConfig(port=9999),
        tool_source=ToolSourceConfig(path="tools/demo.json"),
        identity_mode=mode,
    )


class FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def make_context(headers: dict | None, method: str = "tools/call") -> ServerRequestContext:
    """The context the SDK hands a handler, with the HTTP request we want.

    *headers* None stands for "no HTTP request at all" — the stdio case, and
    what a handler sees when nothing carried it over the wire.
    """
    return ServerRequestContext(
        session=None,
        lifespan_context=None,
        protocol_version="2025-06-18",
        method=method,
        request_id=1,
        meta=None,
        request=FakeRequest(headers) if headers is not None else None,
    )


def redirect_registry(case):
    """Send the identity roster into a temp file for the duration of a test.

    A verified caller is recorded, and these tests verify plenty of them — into
    the project's own `runtime/` if nobody stops it. The suite's promise is
    that it leaves the real installation alone, and a roster full of `sub-anna`
    would break that promise quietly, in a file nobody looks at.
    """
    import app.identity_registry as registry

    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    registry.close()
    patcher = patch.object(registry, "DB_PATH", pathlib.Path(tmp.name) / "identities.db")
    patcher.start()
    case.addCleanup(patcher.stop)
    case.addCleanup(registry.close)


class HandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs the real handlers with a request context we control."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET})
        self.env.start()
        self.addCleanup(self.env.stop)
        # record_call would open the usage database; the counter is not what
        # these tests are about.
        self.record = patch("app.mcp_runner.record_call")
        self.record.start()
        self.addCleanup(self.record.stop)
        redirect_registry(self)

    async def call(self, server, name, headers=None):
        handler = server.get_request_handler("tools/call").handler
        result = await handler(
            make_context(headers),
            types.CallToolRequestParams(name=name, arguments={}),
        )
        return "\n".join(part.text for part in result.content)

    async def list_tools(self, server, headers=None):
        handler = server.get_request_handler("tools/list").handler
        result = await handler(make_context(headers, method="tools/list"), None)
        return [tool.name for tool in result.tools]


class IdentityModeTests(HandlerTestCase):
    def _server(self, mode):
        return build_server(make_config(mode=mode), TOOL_DEFS, DemoTools())

    async def test_off_behaves_exactly_as_before(self):
        """Every existing instance runs in this mode. No token, no policy, no
        change — otherwise the upgrade breaks the Codex and agent connections."""
        server = self._server(IdentityMode.off)
        self.assertEqual(await self.list_tools(server), ["read_item", "delete_item"])
        self.assertEqual(await self.call(server, "read_item"), "read by nobody")

    async def test_required_refuses_a_call_without_a_token(self):
        server = self._server(IdentityMode.required)
        answer = await self.call(server, "read_item", headers={})
        self.assertIn("Access denied", answer)
        self.assertNotIn("read by", answer)

    async def test_required_refuses_an_invalid_token(self):
        server = self._server(IdentityMode.required)
        for label, token in (
            ("wrong secret", mint(secret="some-other-secret")),
            ("expired", mint(exp=1)),
            ("wrong issuer", mint(iss="not-open-webui")),
        ):
            with self.subTest(token=label):
                answer = await self.call(
                    server, "read_item", headers={"x-openwebui-user-jwt": token})
                self.assertIn("Access denied", answer)

    async def test_required_shows_an_unidentified_client_an_empty_catalog(self):
        server = self._server(IdentityMode.required)
        self.assertEqual(await self.list_tools(server, headers={}), [])

    async def test_optional_lets_an_anonymous_call_through(self):
        """Migration mode: the identity arrives if it is there, and its absence
        is not an error. Deliberately not a boundary — that is what required is for."""
        server = self._server(IdentityMode.optional)
        self.assertEqual(await self.call(server, "read_item", headers={}), "read by nobody")

    async def test_optional_hands_the_identity_to_the_tool_without_applying_rules(self):
        """No policy file here. An identified caller must not come off worse
        than an anonymous one — that would make the diagnosis mode useless
        exactly when it is needed, before any rules are written.
        """
        server = self._server(IdentityMode.optional)
        with patch.dict(os.environ, {"MCP_IDENTITY_POLICY": "/nonexistent/policy.json"}):
            policy._cache = policy._cache_key = None
            answer = await self.call(
                server, "read_item", headers={"x-openwebui-user-jwt": mint()})
        self.assertEqual(answer, "read by user-1")

    async def test_optional_already_fills_the_roster(self):
        """The whole point of the mode, and the workflow it enables: switch an
        instance to optional, let everyone work, collect who turns up, assign
        rights from that list, then switch to required. If optional recorded
        nobody, the dashboard would be empty exactly when it is needed.
        """
        import app.identity_registry as registry

        server = self._server(IdentityMode.optional)
        await self.call(server, "read_item", headers={"x-openwebui-user-jwt": mint(sub="sub-anna")})
        self.assertEqual(["sub-anna"], [row["sub"] for row in registry.known()])

    async def test_off_records_nobody(self):
        # It never looks at the header, so there is nothing to record — and an
        # instance nobody governs should not quietly build a list of callers.
        import app.identity_registry as registry

        server = self._server(IdentityMode.off)
        await self.call(server, "read_item", headers={"x-openwebui-user-jwt": mint(sub="sub-anna")})
        self.assertEqual([], registry.known())

    async def test_required_without_a_configured_secret_denies_rather_than_opens(self):
        server = self._server(IdentityMode.required)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            answer = await self.call(
                server, "read_item", headers={"x-openwebui-user-jwt": mint()})
        self.assertIn("Access denied", answer)


class PlainHeaderModeTests(HandlerTestCase):
    """required mode carried by unsigned headers instead of a signed token."""

    PLAIN = {
        "x-openwebui-user-id": "sub-anna",
        "x-openwebui-user-email": "anna@example.org",
        "x-openwebui-user-name": "Anna",
        "x-openwebui-user-role": "user",
    }

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps({"users": {"sub-anna": {"instances": {"inst": "*"}}}}))
        self.extra = patch.dict(os.environ, {
            "MCP_IDENTITY_POLICY": str(path),
            "MCP_USER_TRUST_HEADERS": "1",
        })
        self.extra.start()
        self.addCleanup(self.extra.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))
        self.server = build_server(
            make_config(mode=IdentityMode.required), TOOL_DEFS, DemoTools())

    async def test_the_plain_headers_carry_the_user_through(self):
        self.assertEqual(await self.call(self.server, "read_item", self.PLAIN), "read by sub-anna")

    async def test_rules_apply_exactly_as_with_a_token(self):
        path = pathlib.Path(os.environ["MCP_IDENTITY_POLICY"])
        path.write_text(json.dumps({"users": {"sub-anna": {"instances": {"inst": ["read_item"]}}}}))
        policy._cache = policy._cache_key = None
        self.assertIn("Access denied", await self.call(self.server, "delete_item", self.PLAIN))

    async def test_required_still_refuses_a_caller_with_no_headers_at_all(self):
        self.assertIn("Access denied", await self.call(self.server, "read_item", {}))

    async def test_without_the_switch_the_same_call_is_refused(self):
        """The mode is opt-in per server, and this is what "off" means."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_USER_TRUST_HEADERS", None)
            self.assertIn("Access denied", await self.call(self.server, "read_item", self.PLAIN))

    async def test_with_no_secret_and_no_trusted_headers_required_denies(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_USER_TRUST_HEADERS", None)
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            answer = await self.call(self.server, "read_item", self.PLAIN)
        self.assertIn("Access denied", answer)
        self.assertIn("neither", answer)


class AccessTests(HandlerTestCase):
    """required mode with a policy: Anna may read, Ben may do nothing here."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps({
            "users": {"sub-anna": {"instances": {"inst": ["read_item"]}}}
        }))
        self.policy_env = patch.dict(os.environ, {"MCP_IDENTITY_POLICY": str(path)})
        self.policy_env.start()
        self.addCleanup(self.policy_env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))
        self.server = build_server(
            make_config(mode=IdentityMode.required), TOOL_DEFS, DemoTools())

    def headers(self, sub):
        return {"x-openwebui-user-jwt": mint(sub=sub)}

    async def test_a_permitted_user_runs_their_tool_and_the_tool_sees_them(self):
        self.assertEqual(
            await self.call(self.server, "read_item", self.headers("sub-anna")),
            "read by sub-anna",
        )

    async def test_the_listing_only_shows_permitted_tools(self):
        self.assertEqual(
            await self.list_tools(self.server, self.headers("sub-anna")), ["read_item"])

    async def test_an_unknown_user_gets_nothing_at_all(self):
        self.assertEqual(await self.list_tools(self.server, self.headers("sub-ben")), [])
        answer = await self.call(self.server, "read_item", self.headers("sub-ben"))
        self.assertIn("Access denied", answer)

    async def test_a_hidden_tool_cannot_be_called_by_name(self):
        """The acceptance case. delete_item is filtered out of Anna's listing;
        asking for it directly has to fail too, or the filter is decoration.
        A model that saw the name once will ask for it again."""
        self.assertNotIn("delete_item", await self.list_tools(self.server, self.headers("sub-anna")))
        answer = await self.call(self.server, "delete_item", self.headers("sub-anna"))
        self.assertIn("Access denied", answer)
        self.assertNotIn("deleted by", answer)

    async def test_a_synchronous_tool_sees_the_caller_too(self):
        """delete_item is sync, so it runs in a worker thread via to_thread."""
        path = pathlib.Path(os.environ["MCP_IDENTITY_POLICY"])
        path.write_text(json.dumps({"users": {"sub-anna": {"instances": {"inst": "*"}}}}))
        policy._cache = policy._cache_key = None
        self.assertEqual(
            await self.call(self.server, "delete_item", self.headers("sub-anna")),
            "deleted by sub-anna",
        )

    async def test_two_users_calling_at_once_stay_apart(self):
        """Fifty interleaved calls from two users against one Tools instance.

        The realistic failure this guards against is not the ContextVar but
        anything a tool keeps on self — this pins the framework half of it.
        """
        path = pathlib.Path(os.environ["MCP_IDENTITY_POLICY"])
        path.write_text(json.dumps({
            "users": {
                "sub-anna": {"instances": {"inst": "*"}},
                "sub-ben": {"instances": {"inst": "*"}},
            }
        }))
        policy._cache = policy._cache_key = None

        subs = [f"sub-{'anna' if i % 2 else 'ben'}" for i in range(50)]
        answers = await asyncio.gather(
            *(self.call(self.server, "read_item", self.headers(sub)) for sub in subs)
        )
        self.assertEqual(answers, [f"read by {sub}" for sub in subs])


class MachineIdentityTests(HandlerTestCase):
    """A caller without a login — an agent CLI, a script, a cron job.

    Assigned rather than verified, so the rules can apply to machines too. The
    line that must hold: a *broken* token is still a refusal. Falling back to
    the machine identity there would upgrade a forged token into a working one.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = pathlib.Path(self.tmp.name) / "policy.json"
        path.write_text(json.dumps({"users": {
            "codex-agent": {"instances": {"inst": ["read_item"]}},
            "sub-anna": {"instances": {"inst": "*"}},
        }}))
        self.policy_env = patch.dict(os.environ, {"MCP_IDENTITY_POLICY": str(path)})
        self.policy_env.start()
        self.addCleanup(self.policy_env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))

        config = make_config(mode=IdentityMode.required)
        config.machine_identity = MachineIdentity(sub="codex-agent", name="Codex CLI", role="agent")
        self.server = build_server(config, TOOL_DEFS, DemoTools())

    async def test_a_caller_without_any_token_becomes_the_machine(self):
        self.assertEqual(await self.call(self.server, "read_item", {}), "read by codex-agent")

    async def test_the_machine_is_bound_by_the_rules_like_anyone_else(self):
        """The gain over the old "token or nothing": an agent can be given
        three tools instead of all of them."""
        self.assertEqual(await self.list_tools(self.server, {}), ["read_item"])
        self.assertIn("Access denied", await self.call(self.server, "delete_item", {}))

    async def test_a_real_user_wins_over_the_machine_identity(self):
        answer = await self.call(
            self.server, "read_item", {"x-openwebui-user-jwt": mint(sub="sub-anna")})
        self.assertEqual(answer, "read by sub-anna")

    async def test_a_broken_token_is_refused_and_not_downgraded(self):
        for label, token in (
            ("wrong secret", mint(secret="some-other-secret")),
            ("expired", mint(exp=1)),
        ):
            with self.subTest(token=label):
                answer = await self.call(
                    self.server, "read_item", {"x-openwebui-user-jwt": token})
                self.assertIn("Access denied", answer)
                self.assertNotIn("read by", answer)

    async def test_it_works_without_any_secret_configured(self):
        """The agent case must not depend on OpenWebUI being set up at all."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            self.assertEqual(await self.call(self.server, "read_item", {}), "read by codex-agent")


class LiveTransportTests(unittest.IsolatedAsyncioTestCase):
    """One real runner over real streamable HTTP.

    Everything above fakes the request context. This test exists for the single
    assumption the whole feature rests on: that the MCP SDK hands the HTTP
    request of *this message* to the handler, so a per-call header is readable
    at all. An SDK upgrade could take that away without any of the other tests
    noticing — they would keep testing a context we set ourselves.
    """

    async def asyncSetUp(self):
        import socket

        redirect_registry(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)

        policy_file = root / "policy.json"
        policy_file.write_text(json.dumps(
            {"users": {"sub-anna": {"instances": {"live_demo": ["whoami"]}}}}))
        self.env = patch.dict(os.environ, {
            "MCP_USER_JWT_SECRET": SECRET,
            "MCP_IDENTITY_POLICY": str(policy_file),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        policy._cache = policy._cache_key = None
        self.addCleanup(lambda: setattr(policy, "_cache", None))

        tool_file = root / "live_demo.json"
        tool_file.write_text(json.dumps({
            "id": "live_demo", "name": "live demo",
            "content": (
                "class Tools:\n"
                "    def whoami(self) -> str:\n"
                '        """Report the caller."""\n'
                "        from app.identity import get_current_identity\n"
                "        who = get_current_identity()\n"
                "        return f'sub={who.sub}' if who else 'anonymous'\n"
            ),
            "specs": [{"name": "whoami", "description": "Report the caller.",
                       "parameters": {"type": "object", "properties": {}}}],
        }))

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        config_file = root / "live_demo_config.json"
        config_file.write_text(json.dumps({
            "id": "live_demo", "name": "live demo",
            "server": {"host": "127.0.0.1", "port": self.port, "endpoint": "/mcp"},
            "tool_source": {"type": "openwebui_json", "path": str(tool_file)},
            "identity_mode": "required",
        }))

        from app.mcp_runner import run_server
        self.server_task = asyncio.create_task(run_server(str(config_file)))
        self.addCleanup(self.server_task.cancel)

        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        self.fail("runner did not come up")

    async def _whoami(self, headers):
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = f"http://127.0.0.1:{self.port}/mcp"
        # Since mcp 2.x the headers no longer go to the transport but to an
        # HTTP client handed to it — which is the point of this test: the
        # header has to reach the handler through that longer path.
        async with httpx2.AsyncClient(headers=headers, timeout=10) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = [tool.name for tool in (await session.list_tools()).tools]
                    answer = (await session.call_tool("whoami", {})).content[0].text
        return listed, answer

    async def test_the_header_survives_a_real_call_and_the_tool_sees_the_user(self):
        header = {"X-OpenWebUI-User-Jwt": mint(sub="sub-anna")}
        listed, answer = await self._whoami(header)
        self.assertEqual(listed, ["whoami"])
        self.assertEqual(answer, "sub=sub-anna")

    async def test_a_token_signed_with_another_secret_gets_nowhere(self):
        listed, answer = await self._whoami(
            {"X-OpenWebUI-User-Jwt": mint(sub="sub-anna", secret="some-other-secret")})
        self.assertEqual(listed, [])
        self.assertIn("Access denied", answer)


class ResolveIdentityTests(unittest.TestCase):
    """The header-reading helper on its own, including the stdio case where
    there is no HTTP request at all (`ctx.request is None`)."""

    def setUp(self):
        redirect_registry(self)

    def test_off_never_looks_at_the_header(self):
        ctx = make_context({"x-openwebui-user-jwt": "nonsense"})
        self.assertEqual(_resolve_identity(ctx, IdentityMode.off), (None, ""))

    def test_no_http_request_is_not_a_crash(self):
        with patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET}):
            identity, refusal = _resolve_identity(make_context(None), IdentityMode.required)
        self.assertIsNone(identity)
        self.assertIn("header", refusal)

    def test_a_valid_token_resolves(self):
        with patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET}):
            identity, refusal = _resolve_identity(
                make_context({"x-openwebui-user-jwt": mint()}),
                IdentityMode.required,
            )
        self.assertEqual(refusal, "")
        self.assertEqual(identity.sub, "user-1")

    def test_the_refusal_reason_never_carries_the_token(self):
        token = mint(secret="some-other-secret")
        with patch.dict(os.environ, {"MCP_USER_JWT_SECRET": SECRET}):
            _, refusal = _resolve_identity(
                make_context({"x-openwebui-user-jwt": token}),
                IdentityMode.required,
            )
        self.assertTrue(refusal)
        self.assertNotIn(token, refusal)


if __name__ == "__main__":
    unittest.main()
