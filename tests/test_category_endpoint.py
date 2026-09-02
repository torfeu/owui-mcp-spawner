"""One category as one MCP endpoint: what it lists, and what it says when it can't.

The two handlers built by `build_server()` are driven directly, with the
upstream MCP session faked — so what is tested is the endpoint's own decisions
(which instance a name points at, what a caller hears when it points at
nothing) and not the SDK's transport.

Two things carry this file. That a stopped or silent instance costs the caller
a sentence it can act on rather than a stack trace or an empty answer — at the
other end is a small model, and a model that gets no usable answer invents a
cause. And that the caller's own headers reach the instance unchanged, because
the endpoint checks no rights of its own: the instance is the gate, and it can
only be the gate if it still sees who is calling.
"""
import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from mcp import types

import app.category_endpoint as ce
from app.schema import MCPInstance, MCPStatus
from tests.test_runner_identity import isolate_agent_store, make_context


def instance(instance_id, category="Recht", status=MCPStatus.running, port=8101):
    return MCPInstance(id=instance_id, name=instance_id.title(), category=category,
                       status=status, port=port, host="127.0.0.1", endpoint="/mcp")


def run(coro):
    return asyncio.run(coro)


def tool(name, description="does a thing"):
    return types.Tool(name=name, description=description,
                      inputSchema={"type": "object", "properties": {}})


class FakeSession:
    """Stands in for an initialised MCP session to one instance."""

    def __init__(self, tools=(), result=None, error=None, seen=None):
        self._tools = list(tools)
        self._result = result
        self._error = error
        self.seen = seen if seen is not None else []

    async def list_tools(self):
        if self._error:
            raise self._error
        return types.ListToolsResult(tools=self._tools)

    async def call_tool(self, name, arguments=None):
        self.seen.append((name, arguments))
        if self._error:
            raise self._error
        return self._result or types.CallToolResult(
            content=[types.TextContent(type="text", text=f"called {name}")]
        )


def fake_upstream(case, by_instance: dict, record_headers=None):
    """Patch the module's upstream session with a per-instance fake.

    *by_instance* maps an instance id to a FakeSession. An id that is missing
    raises — a test that reaches an instance it did not set up should say so
    loudly rather than quietly pass.
    """
    @asynccontextmanager
    async def _fake(inst, headers, read_timeout):
        if record_headers is not None:
            record_headers.append((inst.id, dict(headers), read_timeout))
        session = by_instance[inst.id]
        yield session

    patcher = patch.object(ce, "_session", _fake)
    patcher.start()
    case.addCleanup(patcher.stop)


def fake_states(case, instances):
    """Every category question in this module answers from *instances* only.

    Without this the module reads the real `configs/` of whichever machine the
    suite runs on — sixteen instances on the server, a different set here. That
    is the "green locally, red on the server" trap, and it is the reason this
    helper exists rather than a patch per test.
    """
    patcher = patch.object(ce, "get_all_states", lambda: list(instances))
    patcher.start()
    case.addCleanup(patcher.stop)


def switch(case, on: bool):
    """Turn category endpoints on or off for one test, without the real file.

    Patches the settings *read* rather than `enabled()` itself, so the default
    and the flag name are exercised by every test that uses this — and so the
    suite never depends on what `runtime/settings.json` happens to hold on the
    machine it runs on, which is the whole point of the isolate_* helpers next
    to it.
    """
    payload = {"category_endpoints_enabled": on}
    patcher = patch.object(ce, "load_settings", lambda: dict(payload))
    patcher.start()
    case.addCleanup(patcher.stop)


class CategoryListingTests(unittest.TestCase):
    """Which categories exist is derived, never configured."""

    def setUp(self):
        fake_states(self, [
            instance("gesetze", "Recht"),
            instance("rechtsprechung", "Recht", status=MCPStatus.stopped),
            instance("wetter", "Umwelt"),
            instance("nameless", ""),
            instance("blank", "   "),
        ])

    def test_a_category_exists_as_long_as_an_instance_carries_it(self):
        self.assertEqual(["Recht", "Umwelt"], ce.categories())

    def test_an_instance_without_a_category_is_in_none(self):
        # Neither the empty string nor whitespace may open an endpoint — a
        # category nobody named is not a category.
        self.assertNotIn("", ce.categories())
        self.assertNotIn("   ", ce.categories())

    def test_the_listing_counts_stopped_instances_but_does_not_serve_them(self):
        self.assertEqual(["gesetze", "rechtsprechung"],
                         [i.id for i in ce.members("Recht", running_only=False)])
        self.assertEqual(["gesetze"], [i.id for i in ce.members("Recht")])

    def test_a_url_segment_finds_its_category_whatever_its_case(self):
        # The segment is typed by hand into OpenWebUI and into agent configs.
        for segment in ("Recht", "recht", "RECHT"):
            self.assertEqual("Recht", ce.resolve(segment))

    def test_an_unknown_or_empty_segment_resolves_to_nothing(self):
        self.assertIsNone(ce.resolve("Strafrecht"))
        self.assertIsNone(ce.resolve(""))
        self.assertIsNone(ce.resolve("   "))

    def test_a_category_with_a_space_survives_the_round_trip(self):
        fake_states(self, [instance("x", "Umwelt & Recht")])
        path = ce.endpoint_path("Umwelt & Recht")
        self.assertEqual("/mcp/category/Umwelt%20%26%20Recht", path)
        self.assertEqual("Umwelt & Recht", ce.resolve(path[len(ce.prefix()):]))


class ForwardedHeaderTests(unittest.TestCase):
    """The endpoint judges nothing — so the instance has to see everything."""

    def test_the_authorization_header_travels_on(self):
        # Without this the instance sees an anonymous caller and every access
        # rule behind it becomes meaningless.
        forwarded = ce.forwardable({"Authorization": "Bearer abc",
                                    "X-OpenWebUI-User-Token": "jwt"})
        self.assertEqual({"Authorization": "Bearer abc",
                          "X-OpenWebUI-User-Token": "jwt"}, forwarded)

    def test_our_own_session_id_does_not_travel_on(self):
        # It identifies the caller's session with *this* endpoint. Upstream it
        # would be read as a session that never existed there.
        forwarded = ce.forwardable({"mcp-session-id": "ours", "Host": "manager:7860",
                                    "Content-Length": "12", "Authorization": "Bearer abc"})
        self.assertEqual({"Authorization": "Bearer abc"}, forwarded)

    def test_the_header_names_are_matched_regardless_of_case(self):
        # ASGI, Starlette and httpx each hand headers over with their own
        # capitalisation.
        self.assertEqual({}, ce.forwardable({"MCP-Session-Id": "ours"}))


class ListToolsTests(unittest.TestCase):
    def setUp(self):
        self.instances = [instance("gesetze", "Recht"), instance("urteile", "Recht")]
        fake_states(self, self.instances)
        self.server = ce.build_server("Recht")
        self.handler = self.server.get_request_handler("tools/list").handler

    def _list(self, headers=None):
        return run(self.handler(make_context(headers, method="tools/list"), None))

    def test_every_tool_is_named_after_the_instance_it_lives_in(self):
        fake_upstream(self, {
            "gesetze": FakeSession(tools=[tool("search"), tool("fetch")]),
            "urteile": FakeSession(tools=[tool("search")]),
        })
        names = [t.name for t in self._list({}).tools]
        # The same tool name in two instances is two distinct names here —
        # which is the whole reason the prefix is unconditional.
        self.assertEqual(["gesetze.search", "gesetze.fetch", "urteile.search"], names)

    def test_the_schema_and_description_are_passed_through_untouched(self):
        fake_upstream(self, {
            "gesetze": FakeSession(tools=[tool("search", "finds a law")]),
            "urteile": FakeSession(),
        })
        listed = self._list({}).tools[0]
        self.assertEqual("finds a law", listed.description)
        self.assertEqual({"type": "object", "properties": {}}, listed.input_schema)

    def test_a_stopped_instance_is_simply_absent(self):
        self.instances[1].status = MCPStatus.stopped
        fake_upstream(self, {"gesetze": FakeSession(tools=[tool("search")])})
        self.assertEqual(["gesetze.search"], [t.name for t in self._list({}).tools])

    def test_one_silent_instance_does_not_take_the_category_off_the_air(self):
        # The alternative — failing the whole listing — would let a single
        # wedged instance hide every working one behind it.
        fake_upstream(self, {
            "gesetze": FakeSession(tools=[tool("search")]),
            "urteile": FakeSession(error=OSError("All connection attempts failed")),
        })
        self.assertEqual(["gesetze.search"], [t.name for t in self._list({}).tools])

    def test_an_empty_category_lists_nothing_rather_than_failing(self):
        fake_states(self, [])
        self.assertEqual([], self._list({}).tools)

    def test_the_callers_headers_reach_every_instance(self):
        seen = []
        fake_upstream(self, {"gesetze": FakeSession(), "urteile": FakeSession()},
                      record_headers=seen)
        self._list({"Authorization": "Bearer abc", "mcp-session-id": "ours"})
        self.assertEqual({"gesetze", "urteile"}, {row[0] for row in seen})
        for _, headers, _ in seen:
            self.assertEqual({"Authorization": "Bearer abc"}, headers)


class CallToolTests(unittest.TestCase):
    def setUp(self):
        self.instances = [instance("gesetze", "Recht"), instance("urteile", "Recht")]
        fake_states(self, self.instances)
        self.server = ce.build_server("Recht")
        self.handler = self.server.get_request_handler("tools/call").handler

    def _call(self, name, arguments=None, headers=None):
        params = types.CallToolRequestParams(name=name, arguments=arguments or {})
        return run(self.handler(make_context(headers if headers is not None else {}), params))

    @staticmethod
    def _text(result):
        return "".join(block.text for block in result.content
                       if isinstance(block, types.TextContent))

    def test_the_call_goes_to_the_named_instance_under_the_bare_tool_name(self):
        # The instance never learns that it was reached through a category —
        # it is asked for `search`, exactly as a direct client would ask.
        gesetze, urteile = FakeSession(), FakeSession()
        fake_upstream(self, {"gesetze": gesetze, "urteile": urteile})
        self._call("urteile.search", {"q": "BGH"})
        self.assertEqual([], gesetze.seen)
        self.assertEqual([("search", {"q": "BGH"})], urteile.seen)

    def test_a_tool_name_containing_a_dot_splits_at_the_first_one(self):
        # Instance ids cannot contain a dot, so the first one is the boundary
        # even when the tool's own name carries more.
        session = FakeSession()
        fake_upstream(self, {"gesetze": session})
        self._call("gesetze.sub.deep")
        self.assertEqual([("sub.deep", {})], session.seen)

    def test_the_result_is_handed_back_exactly_as_it_arrived(self):
        # Re-wrapping it as text here would throw away structured content and
        # every content block that is not a string.
        original = types.CallToolResult(
            content=[types.TextContent(type="text", text="42")],
            structured_content={"answer": 42},
            is_error=True,
        )
        fake_upstream(self, {"gesetze": FakeSession(result=original)})
        returned = self._call("gesetze.search")
        self.assertIs(original, returned)

    def test_an_unprefixed_name_is_told_what_the_names_here_look_like(self):
        fake_upstream(self, {})
        answer = self._text(self._call("search"))
        self.assertIn("<instance>.<tool>", answer)
        self.assertIn("tools/list", answer)

    def test_a_tool_of_another_category_names_the_category_it_is_not_in(self):
        fake_upstream(self, {})
        answer = self._text(self._call("wetter.forecast"))
        self.assertIn("No instance 'wetter' in category 'Recht'", answer)

    def test_a_stopped_instance_says_so_and_clears_the_arguments(self):
        # Without the second sentence a model reads "not available" as "I got
        # the parameters wrong" and retries the same call with new guesses.
        self.instances[1].status = MCPStatus.stopped
        fake_upstream(self, {})
        answer = self._text(self._call("urteile.search"))
        self.assertIn("'urteile' is not running", answer)
        self.assertIn("Nothing is wrong with the arguments", answer)

    def test_an_instance_that_fails_mid_call_reports_the_cause_not_the_wrapper(self):
        # The SDK runs its transport in a task group, so a refused connection
        # arrives wrapped in "unhandled errors in a TaskGroup" — true, useless.
        group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)",
                               [OSError("All connection attempts failed")])
        fake_upstream(self, {"gesetze": FakeSession(error=group)})
        answer = self._text(self._call("gesetze.search"))
        self.assertIn("All connection attempts failed", answer)
        self.assertNotIn("TaskGroup", answer)

    def test_a_refusal_is_not_flagged_as_an_error_result(self):
        # Same reasoning as in the runner: an error result is what clients hand
        # to their own error handling instead of to the model that has to read it.
        fake_upstream(self, {})
        self.assertFalse(self._call("nope.search").is_error)

    def test_a_call_has_no_read_timeout(self):
        # A tool that legitimately runs for two minutes must not be cut off by
        # the endpoint sitting in front of it.
        seen = []
        fake_upstream(self, {"gesetze": FakeSession()}, record_headers=seen)
        self._call("gesetze.search")
        self.assertIsNone(seen[0][2])


class AuthorisationTests(unittest.TestCase):
    """The endpoint reaches a whole category — it cannot be the open door."""

    def setUp(self):
        isolate_agent_store(self)
        self.original = os.environ.get("MCP_BEARER_TOKEN")
        self.addCleanup(self._restore)

    def _restore(self):
        if self.original is None:
            os.environ.pop("MCP_BEARER_TOKEN", None)
        else:
            os.environ["MCP_BEARER_TOKEN"] = self.original

    @staticmethod
    def _scope(authorization=None):
        headers = [(b"authorization", authorization.encode())] if authorization else []
        return {"type": "http", "path": "/mcp/category/Recht", "headers": headers}

    def test_without_a_configured_token_the_endpoint_is_open(self):
        os.environ.pop("MCP_BEARER_TOKEN", None)
        self.assertTrue(ce._authorised(self._scope()))

    def test_the_shared_token_opens_it(self):
        os.environ["MCP_BEARER_TOKEN"] = "secret"
        self.assertTrue(ce._authorised(self._scope("Bearer secret")))

    def test_the_scheme_is_matched_case_insensitively(self):
        # RFC 7235: the auth scheme is case-insensitive, and a client that
        # sends "bearer" is not an attacker.
        os.environ["MCP_BEARER_TOKEN"] = "secret"
        self.assertTrue(ce._authorised(self._scope("bearer secret")))

    def test_a_wrong_or_missing_token_does_not(self):
        os.environ["MCP_BEARER_TOKEN"] = "secret"
        self.assertFalse(ce._authorised(self._scope("Bearer wrong")))
        self.assertFalse(ce._authorised(self._scope()))
        self.assertFalse(ce._authorised(self._scope("Basic secret")))

    def test_an_agents_own_token_opens_it_too(self):
        # Refusing it here would mean every agent has to carry the shared token
        # as well; which of the two arrived is decided again upstream, per call.
        from app import agent_identity

        os.environ["MCP_BEARER_TOKEN"] = "secret"
        _, token = agent_identity.create("claude", "Claude", "KI")
        self.assertTrue(ce._authorised(self._scope(f"Bearer {token}")))


class HttpEntryTests(unittest.TestCase):
    """What a browser or a misconfigured client gets back."""

    def setUp(self):
        fake_states(self, [instance("gesetze", "Recht"), instance("wetter", "Umwelt")])
        self.original = os.environ.get("MCP_BEARER_TOKEN")
        os.environ.pop("MCP_BEARER_TOKEN", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.original is None:
            os.environ.pop("MCP_BEARER_TOKEN", None)
        else:
            os.environ["MCP_BEARER_TOKEN"] = self.original

    def _get(self, path):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        run(ce.handle({"type": "http", "path": path, "method": "POST", "headers": []},
                      receive, send))
        status = sent[0]["status"]
        body = b"".join(m.get("body", b"") for m in sent[1:]).decode()
        return status, body

    def test_an_unknown_category_is_told_which_ones_exist(self):
        status, body = self._get("/mcp/category/Strafrecht")
        self.assertEqual(404, status)
        self.assertIn("Recht, Umwelt", body)

    def test_the_bare_prefix_says_what_the_url_should_look_like(self):
        status, body = self._get("/mcp/category/")
        self.assertEqual(404, status)
        self.assertIn("/mcp/category/<category-name>", body)

    def test_a_wrong_token_is_refused_before_the_category_is_looked_up(self):
        # Order matters: answering "unknown category" to an unauthenticated
        # caller would list every category this server has to anyone who asks.
        os.environ["MCP_BEARER_TOKEN"] = "secret"
        isolate_agent_store(self)
        status, body = self._get("/mcp/category/Strafrecht")
        self.assertEqual(401, status)
        self.assertNotIn("Umwelt", body)




class CategoryApiTests(unittest.TestCase):
    """`GET /api/categories` — what the dashboard puts in front of a person."""

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.auth as auth
        import app.health as health
        import app.routes.categories as routes

        self.client = TestClient(app_under_test())
        switch(self, True)
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))
        patcher = patch.object(routes.category_endpoint, "get_all_states", lambda: [
            instance("gesetze", "Recht"),
            instance("urteile", "Recht", status=MCPStatus.stopped),
        ])
        patcher.start()
        self.addCleanup(patcher.stop)
        # The tool counts come off the health check, which on this machine holds
        # whatever the last real pass left there.
        health_patch = patch.object(health, "for_instance",
                                    lambda instance_id: {"tools": 3} if instance_id == "gesetze" else None)
        health_patch.start()
        self.addCleanup(health_patch.stop)

    def _rows(self):
        return self.client.get("/api/categories").json()["categories"]

    def test_the_listing_names_the_url_to_register(self):
        row = self._rows()[0]
        self.assertEqual("Recht", row["name"])
        self.assertTrue(row["url"].endswith("/mcp/category/Recht"))
        # The host comes from the request, not from the bind arguments: the URL
        # is going to be pasted somewhere and has to work from there.
        self.assertIn(self.client.base_url.host, row["url"])

    def test_a_stopped_member_is_listed_but_not_counted_as_running(self):
        row = self._rows()[0]
        self.assertEqual(["gesetze", "urteile"], [i["id"] for i in row["instances"]])
        self.assertEqual(1, row["running"])
        self.assertEqual(2, row["total"])
        self.assertEqual(3, row["tools"])

    def test_never_probed_reads_as_unknown_rather_than_zero(self):
        import app.health as health

        with patch.object(health, "for_instance", lambda instance_id: None):
            row = self._rows()[0]
        self.assertIsNone(row["tools"])

    def test_the_export_carries_the_url_and_the_token(self):
        with patch.object(ce, "mcp_bearer_token", lambda: "secret"), \
             patch("app.routes.categories.mcp_bearer_token", lambda: "secret"):
            payload = self.client.get("/api/categories/recht/export").json()
        self.assertEqual("mcp", payload[0]["type"])
        self.assertTrue(payload[0]["url"].endswith("/mcp/category/Recht"))
        self.assertEqual("bearer", payload[0]["auth_type"])
        self.assertEqual("secret", payload[0]["key"])
        # Prefixed so it cannot collide with an instance of the same name in
        # the importing system's own list.
        self.assertEqual("category-Recht", payload[0]["info"]["id"])

    def test_an_unknown_category_cannot_be_exported(self):
        self.assertEqual(404, self.client.get("/api/categories/Strafrecht/export").status_code)


class DispatcherTests(unittest.TestCase):
    """MCP traffic must reach the endpoint without passing through FastAPI.

    `_revalidate_static` is an `@app.middleware("http")`, and Starlette
    middleware wraps the response of everything that enters the app — including
    a streamable-HTTP session meant to stay open. This is the test that the
    dispatcher sits in front, not inside.
    """

    def test_a_category_url_never_reaches_the_fastapi_app(self):
        import app.admin_server as admin_server

        seen = []

        async def spy(scope, receive, send):
            seen.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def never(scope, receive, send):          # pragma: no cover - must not run
            raise AssertionError("the MCP path went through the FastAPI app")

        switch(self, True)
        with patch.object(admin_server.category_endpoint, "handle", spy), \
             patch.object(admin_server, "app", never):
            run(admin_server.asgi({"type": "http", "path": "/mcp/category/Recht"},
                                  None, lambda message: asyncio.sleep(0)))
        self.assertEqual(["/mcp/category/Recht"], seen)

    def test_the_lifespan_still_runs_in_the_manager(self):
        # The dispatcher owns no startup of its own: everything the manager
        # does at boot lives in the FastAPI lifespan and must keep getting it.
        import app.admin_server as admin_server

        seen = []

        async def spy(scope, receive, send):
            seen.append(scope["type"])

        with patch.object(admin_server, "app", spy):
            run(admin_server.asgi({"type": "lifespan"}, None, None))
        self.assertEqual(["lifespan"], seen)

    def test_ordinary_api_traffic_is_untouched(self):
        import app.admin_server as admin_server

        seen = []

        async def spy(scope, receive, send):
            seen.append(scope["path"])

        with patch.object(admin_server, "app", spy):
            run(admin_server.asgi({"type": "http", "path": "/api/instances"}, None, None))
        self.assertEqual(["/api/instances"], seen)


class SwitchedOffTests(unittest.TestCase):
    """The off switch, and what "off" has to look like from outside.

    Off is the default: one endpoint reaches the tools of a whole category at
    once, and an upgrade must not quietly open a door nobody asked for — the
    same rule the shared port already follows.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.auth as auth

        fake_states(self, [instance("gesetze", "Recht")])
        self.client = TestClient(app_under_test())
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))

    def test_a_manager_that_was_never_told_serves_none(self):
        # The default, read through the real settings function with an empty
        # file — an upgrade alone must not switch this on.
        with patch.object(ce, "load_settings", dict):
            self.assertFalse(ce.enabled())

    def test_the_flag_switches_it_on(self):
        switch(self, True)
        self.assertTrue(ce.enabled())

    def test_the_url_is_not_intercepted_at_all_when_off(self):
        # Not a 403: that would tell an unauthenticated caller the feature is
        # there and merely closed. Off means the path falls through to the
        # ordinary 404 of a URL this manager does not serve.
        import app.admin_server as admin_server

        switch(self, False)
        seen = []

        async def spy(scope, receive, send):
            seen.append(scope["path"])

        async def never(scope, receive, send):        # pragma: no cover - must not run
            raise AssertionError("the endpoint was served although it is switched off")

        with patch.object(admin_server.category_endpoint, "handle", never), \
             patch.object(admin_server, "app", spy):
            run(admin_server.asgi({"type": "http", "path": "/mcp/category/Recht"},
                                  None, None))
        self.assertEqual(["/mcp/category/Recht"], seen)

    def test_off_answers_like_a_url_this_manager_does_not_have(self):
        """The other half of the test above, and the part a caller can see.

        That one proves the dispatcher hands the path to the FastAPI app; this
        one proves what the app then answers is the same thing any unknown URL
        gets. Together: switched off, the endpoint is not "there and closed",
        it is not there. Measured live too — `POST /mcp/category/Spike` and
        `POST /voellig/andere/url` both came back 405, both GETs 404.
        """
        for method in ("post", "get"):
            category = getattr(self.client, method)("/mcp/category/Recht")
            unknown = getattr(self.client, method)("/nothing/here/at/all")
            self.assertEqual(unknown.status_code, category.status_code)

    def test_the_listing_still_names_the_categories_when_off(self):
        """Off is reported, not simulated by an empty answer.

        The settings list has to fill itself the moment the switch is ticked,
        before anything is saved — and "no categories" and "switched off" are
        different states that must not arrive as the same empty list. Nothing
        is given away: every category is already on its instance's row.
        """
        switch(self, False)
        payload = self.client.get("/api/categories").json()
        self.assertIs(False, payload["enabled"])
        self.assertEqual(["Recht"], [c["name"] for c in payload["categories"]])

    def test_the_listing_says_so_when_on(self):
        switch(self, True)
        self.assertIs(True, self.client.get("/api/categories").json()["enabled"])

    def test_nothing_can_be_exported_when_off(self):
        # An export handing out a URL that answers 404 would be worse than none.
        switch(self, False)
        self.assertEqual(404, self.client.get("/api/categories/Recht/export").status_code)

    def test_the_settings_route_reports_and_accepts_it(self):
        switch(self, False)
        self.assertIs(False, self.client.get("/api/settings").json()["category_endpoints_enabled"])

        # The write goes through the real route; only the *store* is
        # redirected, because `runtime/settings.json` holds the password hash,
        # three tokens and the JWT secret on the machine the suite also runs on.
        written = {}
        with patch("app.settings_store.save_settings",
                   lambda changes: written.update(changes)):
            response = self.client.put("/api/settings",
                                       json={"category_endpoints_enabled": True})
        self.assertEqual(200, response.status_code)
        self.assertIs(True, written.get("category_endpoints_enabled"))

    def test_a_non_boolean_is_refused(self):
        switch(self, False)
        response = self.client.put("/api/settings", json={"category_endpoints_enabled": "yes"})
        self.assertEqual(400, response.status_code)



class UrlSegmentTests(unittest.TestCase):
    """The word between `/mcp/` and the category name, and what it drags along.

    It is configurable because it ends up in every category URL anybody
    registers — and precisely for that reason it cannot be set freely: an
    instance whose id is that word would vanish behind the category endpoints,
    so the reservation of instance ids has to follow the setting rather than
    naming `category` once and for all.
    """

    def test_a_manager_that_was_never_told_uses_category(self):
        with patch.object(ce, "load_settings", dict):
            self.assertEqual("category", ce.segment())
            self.assertEqual("/mcp/category/", ce.prefix())

    def test_the_setting_moves_every_url(self):
        with patch.object(ce, "load_settings", lambda: {"category_url_segment": "gruppe"}):
            self.assertEqual("/mcp/gruppe/", ce.prefix())
            self.assertEqual("/mcp/gruppe/Recht", ce.endpoint_path("Recht"))

    def test_a_stored_value_that_could_not_have_been_set_is_ignored(self):
        # The route refuses these, but a hand-edited settings file must not be
        # able to break every URL at once — a slash would swallow the category
        # name, an empty string would make the prefix `/mcp//`.
        for broken in ("has/slash", "", "   ", "a" * 33, "mit umlaut ä", 7, None, True):
            with self.subTest(value=broken):
                with patch.object(ce, "load_settings", lambda: {"category_url_segment": broken}):
                    self.assertEqual("/mcp/category/", ce.prefix())

    def test_the_reserved_instance_id_follows_the_setting(self):
        from fastapi import HTTPException
        from app.api_helpers import require_available_id, reserved_ids

        with patch.object(ce, "load_settings", lambda: {"category_url_segment": "gruppe"}):
            self.assertIn("gruppe", reserved_ids())
            with self.assertRaises(HTTPException) as raised:
                require_available_id("gruppe")
            self.assertEqual(400, raised.exception.status_code)
            # And the old word is free again — nothing sits under it any more.
            self.assertNotIn("category", reserved_ids())

    def test_the_dispatcher_follows_the_setting(self):
        import app.admin_server as admin_server

        went = []

        async def to_category(scope, receive, send):
            went.append(("category", scope["path"]))

        async def to_app(scope, receive, send):
            went.append(("app", scope["path"]))

        with patch.object(ce, "load_settings",
                          lambda: {"category_endpoints_enabled": True,
                                   "category_url_segment": "gruppe"}), \
             patch.object(admin_server.shared_proxy, "manager_port_enabled", lambda: False), \
             patch.object(admin_server.category_endpoint, "handle", to_category), \
             patch.object(admin_server, "app", to_app):
            run(admin_server.asgi({"type": "http", "path": "/mcp/gruppe/Recht"}, None, None))
            # The old address is nobody's now: not a redirect, not a 403 — the
            # ordinary 404 of a URL this manager does not serve.
            run(admin_server.asgi({"type": "http", "path": "/mcp/category/Recht"}, None, None))
        self.assertEqual([("category", "/mcp/gruppe/Recht"), ("app", "/mcp/category/Recht")], went)

    def test_the_bare_prefix_names_the_configured_segment(self):
        fake_states(self, [instance("gesetze", "Recht")])
        sent = []

        async def send(message):
            sent.append(message)

        with patch.object(ce, "load_settings", lambda: {"category_url_segment": "gruppe"}):
            run(ce.handle({"type": "http", "path": "/mcp/gruppe/", "method": "POST",
                           "headers": []}, None, send))
        body = b"".join(m.get("body", b"") for m in sent[1:]).decode()
        self.assertEqual(404, sent[0]["status"])
        self.assertIn("/mcp/gruppe/<category-name>", body)


class UrlSegmentRouteTests(unittest.TestCase):
    """Setting it: what is refused, and what happens to open sessions."""

    def setUp(self):
        from fastapi.testclient import TestClient
        import app.auth as auth

        fake_states(self, [instance("gesetze", "Recht")])
        self.client = TestClient(app_under_test())
        original = auth._password_hash
        auth._password_hash = None
        self.addCleanup(lambda: setattr(auth, "_password_hash", original))
        self.written = {}
        patcher = patch("app.settings_store.save_settings",
                        lambda changes: self.written.update(changes))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_route_reports_the_segment(self):
        switch(self, False)
        self.assertEqual("category", self.client.get("/api/settings").json()["category_url_segment"])

    def test_a_good_word_is_stored(self):
        response = self.client.put("/api/settings", json={"category_url_segment": "gruppe"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("gruppe", self.written.get("category_url_segment"))

    def test_what_cannot_be_a_path_element_is_refused(self):
        for bad in ("has/slash", "with space", "ä", "a" * 33, "?query"):
            with self.subTest(value=bad):
                response = self.client.put("/api/settings", json={"category_url_segment": bad})
                self.assertEqual(400, response.status_code, bad)

    def test_a_word_an_instance_already_answers_to_is_refused(self):
        # The whole reason this is validated at all: `gesetze` would still be
        # in the instance list and still be running, and be reachable nowhere.
        with patch("app.routes.settings.config_exists", lambda name: name == "gesetze"):
            response = self.client.put("/api/settings", json={"category_url_segment": "gesetze"})
        self.assertEqual(409, response.status_code)
        self.assertIn("gesetze", response.json()["detail"])
        self.assertEqual({}, self.written)

    def test_an_empty_value_means_back_to_the_default(self):
        with patch.object(ce, "load_settings", lambda: {"category_url_segment": "gruppe"}):
            response = self.client.put("/api/settings", json={"category_url_segment": "  "})
        self.assertEqual(200, response.status_code)
        self.assertEqual("category", self.written.get("category_url_segment"))

    def test_a_non_string_is_refused(self):
        self.assertEqual(400, self.client.put("/api/settings",
                                              json={"category_url_segment": 7}).status_code)

    def test_moving_the_segment_takes_the_open_sessions_down(self):
        # They hang on an address that is no longer routed; leaving them would
        # mean a client holding a session its URL no longer reaches.
        stopped = []

        async def stop_all():
            stopped.append(True)

        with patch.object(ce, "stop_all", stop_all):
            self.client.put("/api/settings", json={"category_url_segment": "gruppe"})
        self.assertEqual([True], stopped)

    def test_setting_it_to_what_it_already_is_changes_nothing(self):
        stopped = []

        async def stop_all():
            stopped.append(True)

        with patch.object(ce, "stop_all", stop_all):
            response = self.client.put("/api/settings", json={"category_url_segment": "category"})
        self.assertEqual(200, response.status_code)
        self.assertNotIn("category_url_segment", self.written)
        self.assertEqual([], stopped)


def app_under_test():
    from app.admin_server import app
    return app


if __name__ == "__main__":
    unittest.main()


class CategoryTransportTests(unittest.IsolatedAsyncioTestCase):
    """The whole path over real sockets: client → endpoint → two real runners.

    Everything above this class starts *after* the transport, with the upstream
    session faked. That is the right place to test decisions and the wrong
    place to find out whether a dotted tool name survives a real `tools/list`,
    whether the Bearer token actually arrives at the instance, or whether the
    session manager comes up at all. Three findings on 30.08. were all made on
    a green suite and only showed up on a real call — this is the class that
    would have caught them.
    """

    CATEGORY = "Live-Transport"
    TOKEN = "the-shared-mcp-token"

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    async def _start_runner(self, instance_id: str, answer: str, port: int):
        import json
        from app.mcp_runner import run_server

        tool_file = self.root / f"{instance_id}_tool.json"
        tool_file.write_text(json.dumps({
            "id": instance_id, "name": instance_id,
            "content": (
                "class Tools:\n"
                "    def hello(self, who: str = 'world') -> str:\n"
                '        """Greet somebody."""\n'
                f"        return f'{answer} greets {{who}}'\n"
            ),
            "specs": [{"name": "hello", "description": "Greet somebody.",
                       "parameters": {"type": "object",
                                      "properties": {"who": {"type": "string"}}}}],
        }))
        config_file = self.root / f"{instance_id}_config.json"
        config_file.write_text(json.dumps({
            "id": instance_id, "name": instance_id, "category": self.CATEGORY,
            "server": {"host": "127.0.0.1", "port": port, "endpoint": "/mcp"},
            "tool_source": {"type": "openwebui_json", "path": str(tool_file)},
        }))
        task = asyncio.create_task(run_server(str(config_file)))
        self.addCleanup(task.cancel)
        await self._await_port(port)
        return task

    @staticmethod
    async def _await_port(port: int) -> None:
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise AssertionError(f"nothing came up on port {port}")

    async def asyncSetUp(self):
        import tempfile
        import pathlib
        import uvicorn
        from tests.test_runner_identity import isolate_usage_db, redirect_registry

        # A real runner counts every call it serves and notes every caller. Both
        # would land in the live runtime/ of whichever machine this runs on.
        isolate_usage_db(self)
        redirect_registry(self)
        isolate_agent_store(self)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

        env = patch.dict(os.environ, {"MCP_BEARER_TOKEN": self.TOKEN})
        env.start()
        self.addCleanup(env.stop)

        self.ports = {"alpha": self._free_port(), "beta": self._free_port()}
        self.runners = {}
        for instance_id, answer in (("alpha", "Alpha"), ("beta", "Beta")):
            self.runners[instance_id] = await self._start_runner(
                instance_id, answer, self.ports[instance_id])

        self.instances = [
            instance("alpha", self.CATEGORY, port=self.ports["alpha"]),
            instance("beta", self.CATEGORY, port=self.ports["beta"]),
        ]
        fake_states(self, self.instances)

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
                await ce.handle(scope, receive, send)

        self.port = self._free_port()
        server = uvicorn.Server(uvicorn.Config(
            asgi, host="127.0.0.1", port=self.port, log_level="error"))
        serving = asyncio.create_task(server.serve())
        self.addCleanup(serving.cancel)
        self.addCleanup(lambda: setattr(server, "should_exit", True))
        await self._await_port(self.port)
        self.addCleanup(lambda: asyncio.get_event_loop().create_task(ce.stop_all()))

    @asynccontextmanager
    async def _client(self, token=None, url=None):
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        url = url or f"http://127.0.0.1:{self.port}{ce.endpoint_path(self.CATEGORY)}"
        headers = {"Authorization": f"Bearer {token or self.TOKEN}"}
        async with httpx2.AsyncClient(headers=headers, timeout=20) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def test_both_instances_are_listed_under_their_dotted_names(self):
        async with self._client() as session:
            listed = sorted(t.name for t in (await session.list_tools()).tools)
        self.assertEqual(["alpha.hello", "beta.hello"], listed)

    async def test_the_call_reaches_the_instance_the_prefix_names(self):
        async with self._client() as session:
            alpha = (await session.call_tool("alpha.hello", {"who": "Torsten"})).content[0].text
            beta = (await session.call_tool("beta.hello", {"who": "Torsten"})).content[0].text
        self.assertEqual("Alpha greets Torsten", alpha)
        self.assertEqual("Beta greets Torsten", beta)

    async def test_the_schema_arrives_intact_so_a_client_can_fill_it_in(self):
        async with self._client() as session:
            listed = {t.name: t for t in (await session.list_tools()).tools}
        self.assertEqual("Greet somebody.", listed["alpha.hello"].description)
        self.assertIn("who", listed["alpha.hello"].input_schema["properties"])

    async def test_an_instance_that_is_gone_costs_a_sentence_not_the_catalog(self):
        # Beta's state still says "running" while nothing is listening on its
        # port any more — the window between a crash and the next watchdog
        # pass, which is exactly when a client is most likely to ask. Stated
        # rather than provoked: a cancelled uvicorn task does not release its
        # listener promptly, so killing the runner would make this a race.
        self.instances[1].port = self._free_port()
        async with self._client() as session:
            listed = [t.name for t in (await session.list_tools()).tools]
            answer = (await session.call_tool("beta.hello", {})).content[0].text
        self.assertEqual(["alpha.hello"], listed)
        self.assertIn("did not answer", answer)

    async def test_a_moved_segment_carries_a_whole_real_session(self):
        # The segment is read on every request, in two places that have to
        # agree: the URL the client is handed, and the path the handler cuts
        # the category name out of. Faked settings prove they read the same
        # value; only a real session proves they agree about where it ends.
        with patch.object(ce, "load_settings", lambda: {"category_url_segment": "gruppe"}):
            url = f"http://127.0.0.1:{self.port}{ce.endpoint_path(self.CATEGORY)}"
            self.assertIn("/mcp/gruppe/", url)
            async with self._client(url=url) as session:
                listed = sorted(t.name for t in (await session.list_tools()).tools)
                answer = (await session.call_tool("alpha.hello", {"who": "Torsten"})).content[0].text
        self.assertEqual(["alpha.hello", "beta.hello"], listed)
        self.assertEqual("Alpha greets Torsten", answer)

    async def test_a_wrong_token_never_reaches_an_instance(self):
        import httpx2

        url = f"http://127.0.0.1:{self.port}{ce.endpoint_path(self.CATEGORY)}"
        async with httpx2.AsyncClient(timeout=10) as client:
            # Asked at the socket: the SDK turns a rejected request into an
            # error that no longer carries the status code, and "some exception"
            # would also pass for an endpoint that never started.
            refused = await client.post(url, headers={"Authorization": "Bearer wrong"},
                                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            missing = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(401, refused.status_code)
        self.assertEqual(401, missing.status_code)
