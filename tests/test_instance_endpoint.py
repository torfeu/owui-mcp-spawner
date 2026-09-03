"""`/mcp/<id>` on the manager port: the same forwarding, one port fewer.

The shared-port listener has done this since 0.1.x on a port of its own. What
is new here is the second way in — the dispatcher in `admin_server.asgi`, next
to the category endpoints — and the three things that decide whether it is
safe: it is off until somebody switches it on, a category is never shadowed by
an instance, and the address the manager dials is the one the runner actually
bound rather than a hardcoded loopback.

Nothing here checks rights, and that is the point: the caller's headers travel
unchanged and the instance is the gate, exactly as on a direct connection.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

import httpx

import app.shared_proxy as sp
from app.schema import MCPInstance, MCPStatus
# The two socket helpers of the category transport test are pure and already
# proven; a second copy would only be a second thing to keep right. The class
# itself is dropped again right away — pytest collects every TestCase it can
# see in a module, and importing one would run its five tests a second time.
from tests.test_category_endpoint import CategoryTransportTests as _live
free_port = _live._free_port
await_port = _live._await_port
del _live


def instance(instance_id="gesetze", status=MCPStatus.running, port=8101,
             host="127.0.0.1", endpoint="/mcp"):
    return MCPInstance(id=instance_id, name=instance_id.title(), category="Recht",
                       status=status, port=port, host=host, endpoint=endpoint)


def run(coro):
    return asyncio.run(coro)


def settings(case, **payload):
    """Read the flags from *payload* instead of `runtime/settings.json`.

    The same reason `test_category_endpoint` patches the read rather than the
    predicate: the flag name and its default are then exercised by every test
    that uses this, and the suite never depends on what the settings file of
    this particular machine happens to hold.
    """
    patcher = patch.object(sp, "load_settings", lambda: dict(payload))
    patcher.start()
    case.addCleanup(patcher.stop)


def fake_state(case, inst):
    """What `get_instance_state` answers — one instance, or nothing."""
    patcher = patch.object(sp, "get_instance_state",
                           lambda instance_id: inst if inst and inst.id == instance_id else None)
    patcher.start()
    case.addCleanup(patcher.stop)


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b'{"ok":true}',)):
        self.status_code = status
        self.headers = httpx.Headers(headers or {"content-type": "application/json",
                                                 "content-length": "11"})
        self._chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class FakeClient:
    """Records what the proxy would have sent upstream."""

    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.built = []

    def build_request(self, method, url, headers=None, content=None):
        self.built.append({"method": method, "url": url, "headers": dict(headers or [])})
        return ("request", method, url)

    async def send(self, request, stream=False):
        if self.error:
            raise self.error
        return self.response


def call(path, client, method="POST", headers=(), query=b""):
    """Drive `_forward` once and collect the ASGI messages it sends back."""
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "path": path, "method": method,
             "headers": list(headers), "query_string": query}
    run(sp._forward(scope, receive, send, client))
    status = sent[0]["status"] if sent else None
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return status, body.decode(), sent


class SwitchTests(unittest.TestCase):
    """Off by default — an upgrade must not open a door nobody asked for."""

    def test_a_manager_that_was_never_told_serves_none(self):
        with patch.object(sp, "load_settings", dict):
            self.assertFalse(sp.manager_port_enabled())

    def test_the_flag_switches_it_on(self):
        settings(self, instance_endpoints_enabled=True)
        self.assertTrue(sp.manager_port_enabled())

    def test_the_shared_port_is_a_separate_decision(self):
        # Two ways in, two switches: turning the manager-port path on says
        # nothing about the listener, and vice versa.
        settings(self, instance_endpoints_enabled=True)
        self.assertIsNone(sp.configured_port())
        settings(self, shared_port=8100)
        self.assertFalse(sp.manager_port_enabled())


class TargetHostTests(unittest.TestCase):
    """Which address the manager dials to reach an instance's own listener."""

    def setUp(self):
        self.original = os.environ.get("MCP_RUNNER_HOST")
        os.environ.pop("MCP_RUNNER_HOST", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("MCP_RUNNER_HOST", None)
        if self.original is not None:
            os.environ["MCP_RUNNER_HOST"] = self.original

    def test_shared_port_mode_means_loopback_whatever_the_config_says(self):
        # process_manager force-binds every runner to 127.0.0.1 while a shared
        # port is set; the config host is then stale and must not be dialled.
        settings(self, shared_port=8100)
        self.assertEqual("127.0.0.1", sp._target_host(instance(host="::1")))

    def test_without_a_shared_port_the_configured_host_is_dialled(self):
        settings(self)
        self.assertEqual("192.168.1.100", sp._target_host(instance(host="192.168.1.100")))

    def test_the_runner_host_of_the_environment_wins_over_the_config(self):
        # That is what the runner was started with — the config value never
        # reached the socket.
        settings(self)
        os.environ["MCP_RUNNER_HOST"] = "127.0.0.1"
        self.assertEqual("127.0.0.1", sp._target_host(instance(host="0.0.0.0")))

    def test_a_wildcard_is_not_an_address(self):
        settings(self)
        self.assertEqual("127.0.0.1", sp._target_host(instance(host="0.0.0.0")))
        self.assertEqual("[::1]", sp._target_host(instance(host="::")))

    def test_ipv6_is_bracketed_for_the_url(self):
        settings(self)
        self.assertEqual("[fd00::5]", sp._target_host(instance(host="fd00::5")))


class ForwardingTests(unittest.TestCase):
    """What reaches the instance, and what the caller hears when nothing does."""

    def setUp(self):
        settings(self)

    def test_the_request_goes_to_the_instance_endpoint(self):
        fake_state(self, instance(port=8101, endpoint="/mcp"))
        client = FakeClient()
        status, _, _ = call("/mcp/gesetze", client)
        self.assertEqual(200, status)
        self.assertEqual("http://127.0.0.1:8101/mcp", client.built[0]["url"])

    def test_the_tail_and_the_query_travel_along(self):
        fake_state(self, instance())
        client = FakeClient()
        call("/mcp/gesetze/messages", client, query=b"session=7")
        self.assertEqual("http://127.0.0.1:8101/mcp/messages?session=7",
                         client.built[0]["url"])

    def test_the_callers_own_headers_reach_the_instance(self):
        # The endpoint checks no rights of its own. It can only leave that to
        # the instance if the instance still sees who is calling.
        fake_state(self, instance())
        client = FakeClient()
        call("/mcp/gesetze", client, headers=[(b"authorization", b"Bearer caller"),
                                              (b"host", b"manager:7860"),
                                              (b"content-length", b"0")])
        sent = client.built[0]["headers"]
        self.assertEqual("Bearer caller", sent["authorization"])
        # Hop-by-hop headers describe the connection to *this* manager and
        # would be wrong upstream.
        self.assertNotIn("host", sent)
        self.assertNotIn("content-length", sent)

    def test_an_unknown_instance_is_named_in_the_answer(self):
        fake_state(self, None)
        status, body, _ = call("/mcp/nope", FakeClient())
        self.assertEqual(404, status)
        self.assertIn("nope", body)

    def test_a_stopped_instance_says_so_instead_of_timing_out(self):
        # At the other end is a small model: "not running" is something it can
        # act on, a hanging connection is not.
        fake_state(self, instance(status=MCPStatus.stopped))
        status, body, _ = call("/mcp/gesetze", FakeClient())
        self.assertEqual(503, status)
        self.assertIn("not running", body)

    def test_the_bare_prefix_says_what_the_url_should_look_like(self):
        fake_state(self, None)
        status, body, _ = call("/mcp/", FakeClient())
        self.assertEqual(404, status)
        self.assertIn("/mcp/<instance-id>", body)

    def test_an_unreachable_instance_becomes_a_502_not_a_stack_trace(self):
        fake_state(self, instance())
        client = FakeClient(error=httpx.ConnectError("connection refused"))
        status, body, _ = call("/mcp/gesetze", client)
        self.assertEqual(502, status)
        self.assertIn("unreachable", body)

    def test_a_client_closed_under_us_is_a_503(self):
        # The switch went off, or the manager is shutting down, while this
        # request was being built. httpx raises RuntimeError, not HTTPError.
        fake_state(self, instance())
        client = FakeClient(error=RuntimeError("client has been closed"))
        status, _, _ = call("/mcp/gesetze", client)
        self.assertEqual(503, status)

    def test_the_upstream_response_is_streamed_through(self):
        fake_state(self, instance())
        response = FakeResponse(chunks=(b"event: one\n", b"event: two\n"))
        status, body, messages = call("/mcp/gesetze", FakeClient(response=response))
        self.assertEqual(200, status)
        self.assertEqual("event: one\nevent: two\n", body)
        # More than one body message: the chunks were not collected first.
        self.assertGreater(len([m for m in messages if m["type"] == "http.response.body"]), 1)
        self.assertTrue(response.closed)
        headers = dict(messages[0]["headers"])
        self.assertEqual(b"application/json", headers[b"content-type"])
        # Length and framing belong to the connection this manager answers on.
        self.assertNotIn(b"content-length", headers)


class DispatchClientTests(unittest.IsolatedAsyncioTestCase):
    """The client behind the manager-port path outlives the shared listener."""

    async def asyncTearDown(self):
        await sp.stop_dispatch_client()

    async def test_the_client_is_built_once_and_kept(self):
        settings(self, instance_endpoints_enabled=True)
        fake_state(self, instance())
        seen = []

        async def spy(scope, receive, send, client):
            seen.append(client)

        with patch.object(sp, "_forward", spy):
            await sp.handle({"type": "http", "path": "/mcp/gesetze"}, None, None)
            await sp.handle({"type": "http", "path": "/mcp/gesetze"}, None, None)
        self.assertIs(seen[0], seen[1])
        self.assertIsNotNone(seen[0])

    async def test_switching_off_closes_it(self):
        settings(self, instance_endpoints_enabled=True)
        async def spy(scope, receive, send, client):
            pass

        with patch.object(sp, "_forward", spy):
            await sp.handle({"type": "http", "path": "/mcp/gesetze"}, None, None)
        client = sp._dispatch_client
        self.assertIsNotNone(client)
        await sp.stop_dispatch_client()
        self.assertIsNone(sp._dispatch_client)
        self.assertTrue(client.is_closed)

    async def test_stopping_the_shared_listener_leaves_it_alone(self):
        # Two independent lifetimes: switching the shared port off must not
        # take the manager-port path down with it.
        settings(self, instance_endpoints_enabled=True)

        async def spy(scope, receive, send, client):
            pass

        with patch.object(sp, "_forward", spy):
            await sp.handle({"type": "http", "path": "/mcp/gesetze"}, None, None)
        await sp.stop_proxy()
        self.assertIsNotNone(sp._dispatch_client)
        self.assertFalse(sp._dispatch_client.is_closed)


class DispatcherTests(unittest.TestCase):
    """Which of the three ways a `/mcp/...` URL can go, it goes."""

    def setUp(self):
        import app.admin_server as admin_server
        self.admin_server = admin_server

    def _dispatch(self, path, categories_on, instances_on):
        import app.category_endpoint as ce

        went = []

        async def to_category(scope, receive, send):
            went.append(("category", scope["path"]))

        async def to_instance(scope, receive, send):
            went.append(("instance", scope["path"]))

        async def to_app(scope, receive, send):
            went.append(("app", scope["path"]))

        with patch.object(ce, "load_settings",
                          lambda: {"category_endpoints_enabled": categories_on}), \
             patch.object(sp, "load_settings",
                          lambda: {"instance_endpoints_enabled": instances_on}), \
             patch.object(self.admin_server.category_endpoint, "handle", to_category), \
             patch.object(self.admin_server.shared_proxy, "handle", to_instance), \
             patch.object(self.admin_server, "app", to_app):
            run(self.admin_server.asgi({"type": "http", "path": path}, None, None))
        return went

    def test_an_instance_url_never_reaches_the_fastapi_app(self):
        # Same reason as the category endpoints: `_revalidate_static` is an
        # `@app.middleware("http")`, and Starlette middleware wraps the
        # response of everything entering the app — including a stream that is
        # meant to stay open.
        self.assertEqual([("instance", "/mcp/gesetze")],
                         self._dispatch("/mcp/gesetze", False, True))

    def test_switched_off_the_path_is_not_intercepted_at_all(self):
        # Not a 403: that would tell an unauthenticated caller the feature is
        # there and merely closed.
        self.assertEqual([("app", "/mcp/gesetze")],
                         self._dispatch("/mcp/gesetze", False, False))

    def test_a_category_is_never_shadowed_by_an_instance(self):
        self.assertEqual([("category", "/mcp/category/Recht")],
                         self._dispatch("/mcp/category/Recht", True, True))

    def test_with_categories_off_that_url_is_just_an_instance_id(self):
        # And `category` is a reserved ID, so no instance can answer it.
        self.assertEqual([("instance", "/mcp/category/Recht")],
                         self._dispatch("/mcp/category/Recht", False, True))

    def test_ordinary_api_traffic_is_untouched(self):
        self.assertEqual([("app", "/api/instances")],
                         self._dispatch("/api/instances", True, True))

    def test_the_bare_word_mcp_is_not_ours(self):
        self.assertEqual([("app", "/mcp")], self._dispatch("/mcp", True, True))


class ReservedIdTests(unittest.TestCase):
    """`category` cannot be taken, or `/mcp/category/<name>` would be ambiguous."""

    def test_an_instance_cannot_be_called_category(self):
        from fastapi import HTTPException
        from app.api_helpers import require_available_id

        with self.assertRaises(HTTPException) as raised:
            require_available_id("category")
        self.assertEqual(400, raised.exception.status_code)
        self.assertIn("reserved", raised.exception.detail)


class SettingsRouteTests(unittest.TestCase):
    """The switch as a person meets it."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from app.admin_server import app
        import app.auth as auth

        self.client = TestClient(app)
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    def test_the_route_reports_it(self):
        settings(self)
        body = self.client.get("/api/settings").json()
        self.assertIs(False, body["instance_endpoints_enabled"])

    def test_the_route_accepts_it(self):
        settings(self)
        # Only the store is redirected: `runtime/settings.json` holds the
        # password hash, three tokens and the JWT secret on the machine the
        # suite also runs on.
        written = {}
        with patch("app.settings_store.save_settings", lambda changes: written.update(changes)):
            response = self.client.put("/api/settings", json={"instance_endpoints_enabled": True})
        self.assertEqual(200, response.status_code)
        self.assertIs(True, written.get("instance_endpoints_enabled"))

    def test_a_non_boolean_is_refused(self):
        settings(self)
        response = self.client.put("/api/settings", json={"instance_endpoints_enabled": "yes"})
        self.assertEqual(400, response.status_code)


class AdvertisedUrlTests(unittest.TestCase):
    """Which address the dashboard shows and the export writes.

    Reported by the user on 03.09.: the manager port was on, the row and the
    export still named the instance's own port. Both now ask one question in
    one place — a port of its own, else the manager port, else direct.
    """

    def setUp(self):
        self.original = os.environ.get("MCP_MANAGER_PORT")
        os.environ["MCP_MANAGER_PORT"] = "7860"
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("MCP_MANAGER_PORT", None)
        if self.original is not None:
            os.environ["MCP_MANAGER_PORT"] = self.original

    def test_neither_way_in_means_the_instances_own_address(self):
        settings(self)
        self.assertIsNone(sp.advertised_port())

    def test_the_manager_port_is_advertised_when_it_serves(self):
        settings(self, instance_endpoints_enabled=True)
        self.assertEqual(7860, sp.advertised_port())

    def test_a_port_of_its_own_wins(self):
        # More deliberate of the two, and it keeps answering if the manager
        # port is switched off later.
        settings(self, instance_endpoints_enabled=True, shared_port=8100)
        self.assertEqual(8100, sp.advertised_port())

    def test_the_row_url_follows(self):
        from app.api_helpers import _instance_to_dict

        inst = instance("gesetze", port=8106)
        settings(self, instance_endpoints_enabled=True)
        d = _instance_to_dict(inst, "192.168.1.100", sp.advertised_port())
        self.assertEqual("http://192.168.1.100:7860/mcp/gesetze", d["url"])

        settings(self)
        d = _instance_to_dict(inst, "192.168.1.100", sp.advertised_port())
        self.assertEqual("http://192.168.1.100:8106/mcp", d["url"])


class PortRegistryTests(unittest.IsolatedAsyncioTestCase):
    """Listeners are kept by port, not one per feature.

    Two roles — instances and categories — and each may have a port of its own,
    the same port as the other, or none. What a listener serves is read from
    the settings per request, so only adding or removing a *port* is allowed to
    start or stop anything.
    """

    async def asyncTearDown(self):
        await sp.stop_proxy()

    def _started(self):
        return sorted(sp._listeners)

    async def test_nothing_is_bound_when_no_port_is_configured(self):
        settings(self)
        self.assertEqual([], await sp.sync_listeners("127.0.0.1"))
        self.assertEqual([], self._started())

    async def test_each_role_can_have_a_port_of_its_own(self):
        a, b = free_port(), free_port()
        settings(self, shared_port=a, category_port=b)
        self.assertEqual([], await sp.sync_listeners("127.0.0.1"))
        self.assertEqual(sorted([a, b]), self._started())

    async def test_the_same_port_for_both_is_one_listener(self):
        # Not two binds on one port, and not a refusal either: the app on that
        # port simply answers both paths.
        port = free_port()
        settings(self, shared_port=port, category_port=port)
        self.assertEqual([], await sp.sync_listeners("127.0.0.1"))
        self.assertEqual([port], self._started())

    async def test_a_port_that_keeps_its_listener_is_not_touched(self):
        # Moving a role between ports must not interrupt a listener that stays.
        a, b = free_port(), free_port()
        settings(self, shared_port=a)
        await sp.sync_listeners("127.0.0.1")
        server = sp._listeners[a]["server"]

        settings(self, shared_port=a, category_port=b)
        await sp.sync_listeners("127.0.0.1")
        self.assertIs(server, sp._listeners[a]["server"])
        self.assertEqual(sorted([a, b]), self._started())

    async def test_removing_a_port_takes_its_listener_down(self):
        a, b = free_port(), free_port()
        settings(self, shared_port=a, category_port=b)
        await sp.sync_listeners("127.0.0.1")
        settings(self, shared_port=a)
        await sp.sync_listeners("127.0.0.1")
        self.assertEqual([a], self._started())

    async def test_a_port_that_cannot_be_bound_is_reported_not_swallowed(self):
        import socket as socketlib

        blocker = socketlib.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        taken = blocker.getsockname()[1]

        settings(self, shared_port=taken)
        errors = await sp.sync_listeners("127.0.0.1")
        self.assertEqual(1, len(errors))
        self.assertIn(str(taken), errors[0])
        self.assertEqual([], self._started())

    async def test_the_client_lives_as_long_as_the_listeners(self):
        port = free_port()
        settings(self, shared_port=port)
        await sp.sync_listeners("127.0.0.1")
        self.assertIsNotNone(sp._client)
        client = sp._client
        settings(self)
        await sp.sync_listeners("127.0.0.1")
        self.assertIsNone(sp._client)
        self.assertTrue(client.is_closed)


class PortAppTests(unittest.TestCase):
    """What the app on one port answers, decided per request."""

    def _ask(self, port, path, **payload):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        seen = []

        async def to_category(scope, receive_, send_):
            seen.append(("category", scope["path"]))

        async def to_forward(scope, receive_, send_, client):
            seen.append(("instance", scope["path"]))

        with patch.object(sp, "load_settings", lambda: dict(payload)), \
             patch.object(sp.category_endpoint, "load_settings", lambda: dict(payload)), \
             patch.object(sp.category_endpoint, "handle", to_category), \
             patch.object(sp, "_forward", to_forward), \
             patch.object(sp, "_client", object()):
            run(sp._listener_app(port)({"type": "http", "path": path, "method": "POST",
                                        "headers": [], "query_string": b""}, receive, send))
        status = sent[0]["status"] if sent else None
        body = b"".join(m.get("body", b"") for m in sent[1:]).decode()
        return seen, status, body

    def test_one_port_can_serve_both(self):
        seen, _, _ = self._ask(8110, "/mcp/category/Recht",
                               shared_port=8110, category_port=8110,
                               category_endpoints_enabled=False)
        self.assertEqual([("category", "/mcp/category/Recht")], seen)
        seen, _, _ = self._ask(8110, "/mcp/gesetze", shared_port=8110, category_port=8110)
        self.assertEqual([("instance", "/mcp/gesetze")], seen)

    def test_a_category_port_does_not_serve_instances(self):
        # And says which URL it does serve — at the other end may be a model
        # that has to decide what to try next.
        seen, status, body = self._ask(8110, "/mcp/gesetze", category_port=8110)
        self.assertEqual([], seen)
        self.assertEqual(404, status)
        self.assertIn("/mcp/category/<category-name>", body)

    def test_an_instance_port_does_not_serve_categories(self):
        # It falls through to the forwarder, which looks for an instance named
        # "category" and says so — the same answer as any unknown id.
        seen, _, _ = self._ask(8110, "/mcp/category/Recht", shared_port=8110)
        self.assertEqual([("instance", "/mcp/category/Recht")], seen)

    def test_the_manager_port_switch_does_not_gate_the_own_port(self):
        # Two ways in, two decisions: a port of its own answers even while the
        # manager port serves nothing.
        seen, _, _ = self._ask(8110, "/mcp/category/Recht",
                               category_port=8110, category_endpoints_enabled=False)
        self.assertEqual([("category", "/mcp/category/Recht")], seen)

    def test_a_port_nobody_claims_answers_nothing(self):
        seen, status, _ = self._ask(8110, "/mcp/gesetze", shared_port=9999)
        self.assertEqual([], seen)
        self.assertEqual(404, status)


class BindHostTests(unittest.TestCase):
    """Where a listener binds, and in which order the two variables count."""

    def setUp(self):
        self.original = {k: os.environ.get(k) for k in ("MCP_MANAGER_HOST", "MCP_RUNNER_HOST")}
        for key in self.original:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self.original.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_the_manager_host_wins(self):
        # `manager.py` sets both to `--host`, so they normally agree. The
        # order matters for the case where they do not: the listener is the
        # public way in, and must not follow a variable about where instances
        # bind.
        os.environ["MCP_MANAGER_HOST"] = "0.0.0.0"
        os.environ["MCP_RUNNER_HOST"] = "127.0.0.1"
        self.assertEqual("0.0.0.0", sp.bind_host())

    def test_the_runner_host_is_the_fallback(self):
        os.environ["MCP_RUNNER_HOST"] = "::1"
        self.assertEqual("::1", sp.bind_host())

    def test_loopback_when_nothing_says_otherwise(self):
        self.assertEqual("127.0.0.1", sp.bind_host())


class InstanceTransportTests(unittest.IsolatedAsyncioTestCase):
    """The whole path over real sockets: MCP client → manager port → a real runner.

    Everything above this class stops at the ASGI boundary with a faked httpx
    client. That is the right place to test decisions and the wrong place to
    find out whether an MCP session survives the forwarding at all — the same
    reason `CategoryTransportTests` exists next door, and the same three
    findings from 30.08. that a green suite did not catch.

    What only this class can show: the tool names arrive **undotted**. A
    category endpoint rewrites them, this path must not — a client registered
    against `/mcp/<id>` sees exactly what it sees on a direct connection.
    """

    TOKEN = "the-shared-mcp-token"

    async def asyncSetUp(self):
        import json
        import pathlib as _pathlib
        import tempfile
        import uvicorn
        from app.mcp_runner import run_server
        from tests.test_category_endpoint import isolate_agent_store
        from tests.test_runner_identity import isolate_usage_db, redirect_registry

        # A real runner counts every call and notes every caller — both would
        # otherwise land in the live runtime/ of whichever machine this runs on.
        isolate_usage_db(self)
        redirect_registry(self)
        isolate_agent_store(self)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = _pathlib.Path(tmp.name)

        env = patch.dict(os.environ, {"MCP_BEARER_TOKEN": self.TOKEN})
        env.start()
        self.addCleanup(env.stop)
        # The manager dials what the runner bound; nothing inherited from the
        # environment of the machine may decide that here.
        runner_host = patch.dict(os.environ, {}, clear=False)
        runner_host.start()
        os.environ.pop("MCP_RUNNER_HOST", None)
        self.addCleanup(runner_host.stop)

        self.instance_port = free_port()
        tool_file = root / "tool.json"
        tool_file.write_text(json.dumps({
            "id": "gesetze", "name": "gesetze",
            "content": (
                "class Tools:\n"
                "    def hello(self, who: str = 'world') -> str:\n"
                '        """Greet somebody."""\n'
                "        return f'Gesetze greets {who}'\n"
            ),
            "specs": [{"name": "hello", "description": "Greet somebody.",
                       "parameters": {"type": "object",
                                      "properties": {"who": {"type": "string"}}}}],
        }))
        config_file = root / "config.json"
        config_file.write_text(json.dumps({
            "id": "gesetze", "name": "gesetze", "category": "Recht",
            "server": {"host": "127.0.0.1", "port": self.instance_port, "endpoint": "/mcp"},
            "tool_source": {"type": "openwebui_json", "path": str(tool_file)},
        }))
        runner = asyncio.create_task(run_server(str(config_file)))
        self.addCleanup(runner.cancel)
        await await_port(self.instance_port)

        settings(self)  # no shared port: the configured host is dialled
        fake_state(self, instance(port=self.instance_port))

        async def asgi(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    event = await receive()
                    if event["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif event["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            else:
                await sp.handle(scope, receive, send)

        self.port = free_port()
        server = uvicorn.Server(uvicorn.Config(
            asgi, host="127.0.0.1", port=self.port, log_level="error"))
        serving = asyncio.create_task(server.serve())
        self.addCleanup(serving.cancel)
        self.addCleanup(lambda: setattr(server, "should_exit", True))
        await await_port(self.port)
        self.addCleanup(lambda: asyncio.get_event_loop().create_task(
            sp.stop_dispatch_client()))

    async def test_a_real_session_runs_through_the_manager_port(self):
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = f"http://127.0.0.1:{self.port}/mcp/gesetze"
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.TOKEN}"},
                                      timeout=20) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = [t.name for t in (await session.list_tools()).tools]
                    answer = (await session.call_tool("hello", {"who": "Torsten"})).content[0].text
        # Undotted: this path forwards, it does not rebuild the catalogue.
        self.assertEqual(["hello"], listed)
        self.assertEqual("Gesetze greets Torsten", answer)

    async def test_a_port_of_its_own_carries_a_whole_real_session(self):
        # The listener the registry starts, not the hand-rolled one above: bind,
        # per-port app and forwarding in one go, over a real socket. Everything
        # else about the ports is decided from settings, and settings can be
        # faked into agreeing with themselves — this cannot.
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        own = free_port()
        settings(self, shared_port=own)
        self.assertEqual([], await sp.sync_listeners("127.0.0.1"))
        self.addAsyncCleanup(sp.stop_proxy)

        url = f"http://127.0.0.1:{own}/mcp/gesetze"
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.TOKEN}"},
                                      timeout=20) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    answer = (await session.call_tool("hello", {"who": "Torsten"})).content[0].text
        self.assertEqual("Gesetze greets Torsten", answer)

    async def test_the_instance_is_the_gate_not_the_proxy(self):
        import httpx2

        url = f"http://127.0.0.1:{self.port}/mcp/gesetze"
        async with httpx2.AsyncClient(timeout=10) as client:
            refused = await client.post(url, headers={"Authorization": "Bearer wrong"},
                                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        # The 401 was written by the runner, not here — the proxy forwards the
        # header and lets the instance decide, exactly as on a direct connection.
        self.assertEqual(401, refused.status_code)

    async def test_an_unknown_id_is_answered_at_the_socket(self):
        import httpx2

        async with httpx2.AsyncClient(timeout=10) as client:
            response = await client.post(f"http://127.0.0.1:{self.port}/mcp/nope",
                                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(404, response.status_code)
        self.assertIn("nope", response.text)


if __name__ == "__main__":
    unittest.main()
