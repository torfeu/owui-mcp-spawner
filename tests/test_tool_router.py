import json
from pathlib import Path
import unittest
import unittest.mock

from app.tool_editor import validate_tool_code


ROUTER_PATH = Path(__file__).resolve().parents[1] / "examples" / "mcp-tool-router.json"


def load_router():
    exported = json.loads(ROUTER_PATH.read_text())[0]
    namespace = {}
    exec(exported["content"], namespace)
    return namespace["Tools"]()


class RouterSpecTests(unittest.TestCase):
    """Same rule as for the control tool: the shipped `specs` are generated from
    the code, never hand-edited — a drifting description is what a model reads."""

    def test_specs_match_the_docstrings_of_the_shipped_code(self):
        exported = json.loads(ROUTER_PATH.read_text())[0]
        result = validate_tool_code(exported["content"])

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["warnings"])
        self.assertEqual(
            {t["name"]: t for t in result["tools"]},
            {s["name"]: s for s in exported["specs"]},
        )

    def test_exactly_the_four_meta_tools_are_exposed(self):
        # The whole point is a constant, tiny tool list in the prompt. A fifth
        # tool is a decision, not an accident — the fourth, find_functions,
        # was one (11.10.: look inside one instance, instead of a help tool).
        exported = json.loads(ROUTER_PATH.read_text())[0]
        self.assertEqual(
            {"find_tools", "find_functions", "describe_tools", "call_tool"},
            {s["name"] for s in exported["specs"]},
        )

    def test_find_tools_works_without_arguments(self):
        # "Call it without arguments" is the documented entry point — if any
        # parameter became required, a model would have to guess a query first.
        exported = json.loads(ROUTER_PATH.read_text())[0]
        find = next(s for s in exported["specs"] if s["name"] == "find_tools")
        self.assertEqual([], find["parameters"].get("required", []))

    def test_call_tool_takes_one_handle_and_an_object(self):
        exported = json.loads(ROUTER_PATH.read_text())[0]
        call = next(s for s in exported["specs"] if s["name"] == "call_tool")
        props = call["parameters"]["properties"]
        # One dotted string, copied from the listing — not instance + tool in
        # two fields, which invites pairing the wrong two.
        self.assertEqual(["tool"], call["parameters"]["required"])
        self.assertEqual("string", props["tool"]["type"])
        # Arguments stay structured: a free-form string would push parsing into
        # the router and format invention into the model.
        self.assertEqual("object", props["arguments"]["type"])


class RouterSafetyTests(unittest.TestCase):
    def test_the_router_never_routes_into_itself(self):
        tools = load_router()
        self.assertIn("mcp_tool_router", tools._denied())
        # A copy installed under another ID is caught by its tool set, so the
        # loop protection does not depend on the ID alone.
        namespace = {}
        exec(json.loads(ROUTER_PATH.read_text())[0]["content"], namespace)
        self.assertEqual(
            {"find_tools", "describe_tools", "call_tool"}, namespace["SELF_FINGERPRINT"]
        )

    def test_the_deny_list_defaults_to_the_control_tool(self):
        # Without this, any model in a chat could reconfigure the spawner
        # through the router.
        tools = load_router()
        self.assertIn("mcp_manager_control", tools._denied())

    def test_only_the_bearer_and_the_user_token_reach_the_target(self):
        """The router forwards an identity, it does not create one.

        Two headers and no more: its own bearer, and the caller's token exactly
        as it arrived — so the target can verify it against the shared secret
        instead of trusting the router. Nothing else of the original request
        (Authorization, cookies) is passed on.
        """
        from app.identity import identity_scope, verify_user_jwt
        from tests.test_identity import SECRET, mint

        tools = load_router()
        tools.valves.mcp_token = "router-bearer"
        token = mint()

        with unittest.mock.patch.dict("os.environ", {"MCP_USER_JWT_SECRET": SECRET}):
            with identity_scope(verify_user_jwt(token, SECRET)):
                headers = tools._headers_for({"id": "target"})

        self.assertEqual(
            headers,
            {"Authorization": "Bearer router-bearer", "X-OpenWebUI-User-Jwt": token},
        )

    def test_an_unsigned_identity_is_forwarded_as_the_plain_headers(self):
        """With no token there is nothing to hand on verbatim, so the four
        headers are rebuilt — otherwise the target would see an anonymous call
        where a user made it, and the whole mode would end at the router."""
        from app.identity import SOURCE_HEADERS, Identity, identity_scope

        tools = load_router()
        tools.valves.mcp_token = "router-bearer"
        who = Identity(sub="sub-anna", email="anna@example.org", name="Anna",
                       role="user", source=SOURCE_HEADERS)
        with identity_scope(who):
            headers = tools._headers_for({"id": "target"})

        self.assertEqual(headers, {
            "Authorization": "Bearer router-bearer",
            "X-OpenWebUI-User-Id": "sub-anna",
            "X-OpenWebUI-User-Email": "anna@example.org",
            "X-OpenWebUI-User-Name": "Anna",
            "X-OpenWebUI-User-Role": "user",
        })

    def test_without_a_caller_only_the_bearer_travels(self):
        # No identity is not an error here — the target refuses if it needs one.
        tools = load_router()
        tools.valves.mcp_token = "router-bearer"
        self.assertEqual(tools._headers_for({"id": "target"}), {"Authorization": "Bearer router-bearer"})

    def test_forwarding_can_be_switched_off(self):
        from app.identity import Identity, identity_scope

        tools = load_router()
        tools.valves.mcp_token = "router-bearer"
        tools.valves.forward_user_identity = False
        with identity_scope(Identity(sub="user-1", raw_token="a.b.c")):
            self.assertEqual(
                tools._headers_for({"id": "target"}), {"Authorization": "Bearer router-bearer"})

    def test_the_tool_still_loads_where_there_is_no_framework(self):
        """The same JSON is installable in OpenWebUI itself, where `app` does
        not exist. The import has to stay soft, or the tool dies on import."""
        import builtins

        real_import = builtins.__import__

        def no_app(name, *args, **kwargs):
            if name.startswith("app."):
                raise ImportError(f"no module named {name}")
            return real_import(name, *args, **kwargs)

        tools = load_router()
        tools.valves.mcp_token = "router-bearer"
        with unittest.mock.patch.object(builtins, "__import__", no_app):
            self.assertEqual(
                tools._headers_for({"id": "target"}), {"Authorization": "Bearer router-bearer"})

    def test_umlauts_are_normalised_for_search(self):
        # German and English descriptions sit side by side in one installation.
        namespace = {}
        exec(json.loads(ROUTER_PATH.read_text())[0]["content"], namespace)
        norm = namespace["_norm"]
        self.assertEqual("kueche", norm("Küche"))
        self.assertEqual("strasse", norm("Straße"))
        self.assertEqual("gesetz", norm("GESETZ"))


if __name__ == "__main__":
    unittest.main()


class ForwardedCredentialTests(unittest.TestCase):
    """What travels to the target, and — just as much — what must not.

    Two callers, two credentials, and they do not mix. A person's identity is a
    JWT and belongs in the user header; a named agent's is a bearer and belongs
    in Authorization. Sending the agent token in the user header made the
    target answer "malformed token (expected three dot-separated segments)" —
    found live on 03.09., the day the forwarding was built, and the reason this
    class exists.
    """

    def setUp(self):
        from app.identity import Identity, SOURCE_AGENT, SOURCE_TOKEN, identity_scope

        self.Identity, self.SOURCE_AGENT = Identity, SOURCE_AGENT
        self.SOURCE_TOKEN, self.identity_scope = SOURCE_TOKEN, identity_scope
        self.tools = load_router()
        self.tools.valves.mcp_token = "the-shared-token"
        self.tools.valves.forward_user_identity = True
        from app.identity import header_name
        self.user_header = header_name()

    def _headers(self, identity):
        with self.identity_scope(identity):
            return self.tools._headers_for({"id": "target"})

    def test_an_agent_travels_as_a_bearer_and_nowhere_else(self):
        agent = self.Identity(sub="Claude", name="Claude", role="KI",
                              source=self.SOURCE_AGENT, raw_token="mcpa_secret")
        headers = self._headers(agent)
        self.assertEqual("Bearer mcpa_secret", headers["Authorization"])
        # The target would try to parse this as a JWT and refuse the call.
        self.assertNotIn(self.user_header, headers)

    def test_a_person_travels_as_a_user_token_and_the_router_keeps_its_own(self):
        user = self.Identity(sub="u-1", name="Torsten", role="admin",
                             source=self.SOURCE_TOKEN, raw_token="a.b.c")
        headers = self._headers(user)
        self.assertEqual("a.b.c", headers[self.user_header])
        # The bearer says "this client may talk to that server" and stays ours.
        self.assertEqual("Bearer the-shared-token", headers["Authorization"])

    def test_an_agent_without_a_token_changes_nothing(self):
        # The manager hands the token down only for an instance configured for
        # it. Without it there is nothing to forward, and the router falls back
        # to its own bearer rather than sending an empty one.
        agent = self.Identity(sub="Claude", name="Claude", role="KI",
                              source=self.SOURCE_AGENT, raw_token="")
        headers = self._headers(agent)
        self.assertEqual("Bearer the-shared-token", headers["Authorization"])
        self.assertNotIn(self.user_header, headers)

    def test_nobody_identified_means_the_routers_own_bearer_alone(self):
        headers = self._headers(None)
        self.assertEqual({"Authorization": "Bearer the-shared-token"}, headers)


class ErrorAnswerTests(unittest.TestCase):
    """A failed call comes back with the schema attached — also when the
    runner words the failure as text rather than flagging is_error.

    The runner deliberately does not set is_error (a model has to *read* the
    reason), so a wrong parameter name reached the model without the schema
    that would have fixed it. Seen live: six attempts through the router at
    external_mcp_bridge.call_tool before the model found the right shape.
    """

    SPEC = {"name": "search", "description": "Search the thing.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}
    INST = {"id": "demo", "url": "http://127.0.0.1:8101/mcp", "note": ""}

    def invoke(self, text, is_error=False):
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        tools = load_router()

        @asynccontextmanager
        async def streams(url, headers, timeout):
            yield None, None

        class Session:
            def __init__(self, read, write):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def initialize(self):
                return SimpleNamespace(server_info=SimpleNamespace(name="demo"))

            async def call_tool(self, name, arguments):
                return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=is_error)

        glob = type(tools)._invoke.__globals__
        with unittest.mock.patch.dict(glob, {"_mcp_streams": streams}), \
                unittest.mock.patch("mcp.ClientSession", Session):
            return asyncio.run(tools._invoke(self.INST, self.SPEC, "demo.search", {"q": "x"}))

    def test_a_runner_error_gets_the_schema(self):
        answer = self.invoke("Error: 'q' is not a parameter of 'search' — did you mean 'query'?")
        self.assertIn("reported an error", answer)
        self.assertIn("query (string, required)", answer)

    def test_also_behind_the_storage_warning(self):
        answer = self.invoke("Note: the file storage of this server is nearly full (9.0 of 10.0 MB). "
                             "Tell the user to delete old files or contact the administrator.\n\n"
                             "Error: something broke")
        self.assertIn("query (string, required)", answer)

    def test_an_is_error_result_still_gets_it(self):
        self.assertIn("query (string, required)", self.invoke("upstream said no", is_error=True))

    def test_a_normal_result_mentioning_an_error_is_left_alone(self):
        # Only the runner's own wording counts — a result *about* errors is a result.
        answer = self.invoke("Found 3 entries. Error: rate is 2 %.")
        self.assertEqual("Found 3 entries. Error: rate is 2 %.", answer)


def spec(name, description="", params=None, required=()):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": params or {}, "required": list(required)}}


def instance(instance_id, specs, note="", name=""):
    return {"id": instance_id, "name": name or instance_id, "category": "Test",
            "description": f"{instance_id} instance", "status": "running",
            "url": f"http://127.0.0.1:9999/mcp/{instance_id}", "note": note, "specs": specs}


def cached_router(*catalog):
    import time
    tools = load_router()
    # A fresh cache: _catalog() answers from it without asking the manager.
    tools._cache, tools._cache_time = list(catalog), time.time()
    return tools


class FakeRun:
    """Stands in for _run(): records each call, answers from a table."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def __call__(self, inst, name, arguments):
        self.calls.append((inst["id"], name, arguments))
        answer = self.answers.get(name, ("(empty result)", False))
        if isinstance(answer, Exception):
            raise answer
        return answer(arguments) if callable(answer) else answer


NEXTCLOUD = instance("nextcloud", [
    spec("list_mails", "Lists all messages in a mailbox folder", {"folder": {"type": "string"}}, ["folder"]),
    spec("get_mail_account_list", "Lists all available email accounts"),
    spec("get_mail_folder_list", "Lists all mail folders for an email account",
         {"account_id": {"type": "integer"}}, ["account_id"]),
    spec("get_file_tree", "Get the file tree of the user"),
    spec("get_target_info", "Nothing to do with get as a word start"),
    spec("schedule_event", "Create a new event in a calendar"),
] + [spec(f"extra_{n:02d}", "Filler") for n in range(30)])
LAWS = instance("laws", [
    spec("get_paragraph", "One paragraph of a law",
         {"law": {"type": "string"}, "paragraph": {"type": "string"}}, ["law", "paragraph"]),
    spec("list_mails", "A second list_mails, to make the short name ambiguous"),
])


class FindFunctionsTests(unittest.TestCase):
    """find_functions looks inside one instance — nothing cut off, and the
    closer the question, the more detail comes back (11.10.)."""

    def run_(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_without_words_every_name_and_no_descriptions(self):
        listing = self.run_(cached_router(NEXTCLOUD, LAWS).find_functions("nextcloud"))
        self.assertIn("nextcloud: 36 functions", listing)
        for n in range(30):
            self.assertIn(f"\nextra_{n:02d}\n".strip(), listing)
        self.assertNotIn("Filler", listing)           # names only
        self.assertNotIn("get_paragraph", listing)    # only this instance

    def test_words_narrow_down_with_and(self):
        listing = self.run_(cached_router(NEXTCLOUD).find_functions("nextcloud", ["get", "mail"]))
        self.assertIn("2 match(es)", listing)
        self.assertIn("nextcloud.get_mail_account_list", listing)
        self.assertIn("nextcloud.get_mail_folder_list", listing)
        self.assertNotIn("nextcloud.list_mails", listing)
        self.assertNotIn("get_target_info", listing)  # word starts, not substrings

    def test_three_matches_or_fewer_come_with_schema_and_example(self):
        listing = self.run_(cached_router(NEXTCLOUD).find_functions("nextcloud", ["folder"]))
        self.assertIn("account_id (integer, required)", listing)
        self.assertIn('Call: call_tool(tool="nextcloud.get_mail_folder_list", arguments={"account_id": "<account_id>"})', listing)

    def test_and_falls_back_to_or_and_then_to_everything(self):
        tools = cached_router(NEXTCLOUD)
        some = self.run_(tools.find_functions("nextcloud", ["mail", "calendar"]))
        self.assertIn("showing those matching any", some)
        self.assertIn("nextcloud.schedule_event", some)
        nothing = self.run_(tools.find_functions("nextcloud", ["xyzzy"]))
        self.assertIn("showing every function", nothing)
        self.assertIn("extra_29", nothing)

    def test_the_instance_can_be_named_loosely(self):
        tools = cached_router(NEXTCLOUD, instance("bridge", [spec("call_tool")], note="Wikidata"))
        self.assertIn("nextcloud: 36", self.run_(tools.find_functions("nextcloud.list_mails")))
        self.assertIn("bridge: 1 function", self.run_(tools.find_functions("wikidata")))
        unknown = self.run_(tools.find_functions("nope"))
        self.assertIn("Unknown tool 'nope'. Instances: nextcloud, bridge", unknown)

    def test_a_cut_listing_points_at_find_functions(self):
        tools = cached_router(NEXTCLOUD, LAWS)
        tools.valves.max_results = 10
        listing = self.run_(tools.find_tools())
        self.assertIn('find_functions(tool="nextcloud")  — 26 more there', listing)
        self.assertIn('find_functions(tool="laws")  — 2 more there', listing)


class ShortNameTests(unittest.TestCase):
    def test_a_unique_short_name_is_enough(self):
        import asyncio
        tools = cached_router(NEXTCLOUD, LAWS)
        described = asyncio.run(tools.describe_tools(["get_paragraph"]))
        self.assertTrue(described.startswith("laws.get_paragraph"))

    def test_an_ambiguous_one_names_the_candidates(self):
        import asyncio
        described = asyncio.run(cached_router(NEXTCLOUD, LAWS).describe_tools(["list_mails"]))
        self.assertIn("Unknown tool 'list_mails'. Did you mean:", described)
        self.assertIn("nextcloud.list_mails", described)
        self.assertIn("laws.list_mails", described)


class ArgumentHintTests(unittest.TestCase):
    """What to change, and a call built from the model's own values."""

    PARAGRAPH = spec("get_paragraph", "", {"law": {"type": "string"}, "paragraph": {"type": "string"}},
                     ["law", "paragraph"])

    def hints(self, arguments, target=None):
        return load_router()._argument_hints(target or self.PARAGRAPH, arguments)

    def test_a_misspelt_name_gets_a_did_you_mean(self):
        hints, fixed = self.hints({"law": "bgb", "paragraf": "823"})
        self.assertEqual(["'paragraf' is not a parameter — did you mean 'paragraph'?"], hints)
        self.assertEqual({"law": "bgb", "paragraph": "823"}, fixed)

    def test_one_stray_value_and_one_empty_slot_are_paired(self):
        hints, fixed = self.hints({"law": "bgb", "nummer": "823"})
        self.assertIn("probably meant 'paragraph'", hints[0])
        self.assertEqual({"law": "bgb", "paragraph": "823"}, fixed)

    def test_a_second_nesting_is_unwrapped(self):
        hints, fixed = self.hints({"name": "get_paragraph", "arguments": {"law": "bgb", "paragraph": "1"}})
        self.assertIn("not nested a second time", hints[0])
        self.assertEqual({"law": "bgb", "paragraph": "1"}, fixed)

    def test_missing_values_stay_placeholders(self):
        hints, fixed = self.hints({"law": "bgb"})
        self.assertEqual(["'paragraph' is required but missing"], hints)
        self.assertEqual({"law": "bgb", "paragraph": "<paragraph>"}, fixed)

    def test_correct_arguments_need_no_hint(self):
        self.assertEqual(([], {"law": "bgb", "paragraph": "1"}),
                         self.hints({"law": "bgb", "paragraph": "1"}))

    def test_the_error_answer_carries_the_call_to_copy(self):
        tools = load_router()
        answer = tools._failure("laws.get_paragraph", self.PARAGRAPH, LAWS,
                                {"law": "bgb", "paragraf": "823"}, "Error: 'paragraf' is not a parameter")
        self.assertIn('Try: call_tool(tool="laws.get_paragraph", arguments={"law": "bgb", "paragraph": "823"})', answer)
        self.assertIn('find_functions(tool="laws"', answer)


BRIDGE = instance("wd", [spec("list_tools"), spec("describe_tool", "", {"name": {"type": "string"}}, ["name"]),
                         spec("call_tool", "", {"name": {"type": "string"}, "arguments": {"type": "object"}},
                              ["name"])], note="Wikidata")
LIST_TEXT = ("2 tool(s) at https://wd.example/mcp/:\n\n"
             "search_items — Search Wikidata items (QIDs).\n"
             "execute_sparql — Execute a SPARQL query.\n\n"
             'Next: describe_tool(name="<name>") for parameters, then\n'
             'call_tool(name="<name>", arguments={...}) — arguments is a nested object.')
DESCRIBE_TEXT = ("search_items\nSearch Wikidata items (QIDs)\nusing keywords.\nParameters:\n"
                 "  query (string, required): What to look for\n"
                 "  lang (['string', 'null'], optional)\n"
                 'Call: call_tool(name="search_items", arguments={\'query\': \'<query>\'})')


class BridgeExpansionTests(unittest.TestCase):
    """The tools behind an external_mcp_bridge are listed as functions of
    their own — Nova once took six attempts at the double nesting."""

    def router(self, answers):
        tools = cached_router(BRIDGE, NEXTCLOUD)
        tools._run = FakeRun(answers)
        return tools

    def run_(self, coro):
        import asyncio
        return asyncio.run(coro)

    def answers(self, **extra):
        return {"list_tools": (LIST_TEXT, False), "describe_tool": (DESCRIBE_TEXT, False), **extra}

    def test_the_external_tools_replace_the_bridge_trio(self):
        listing = self.run_(self.router(self.answers()).find_tools(["person", "wikidata"]))
        self.assertIn("wd.search_items — Search Wikidata items", listing)
        self.assertNotIn("wd.list_tools", listing)
        self.assertNotIn("wd.call_tool", listing)

    def test_the_list_is_read_once_not_per_search(self):
        tools = self.router(self.answers())
        self.run_(tools.find_tools())
        self.run_(tools.find_tools())
        self.assertEqual(1, [c[1] for c in tools._run.calls].count("list_tools"))

    def test_describe_reads_the_schema_from_the_bridge(self):
        tools = self.router(self.answers())
        described = self.run_(tools.describe_tools(["wd.search_items"]))
        self.assertIn("using keywords.", described)
        self.assertIn("query (string, required): What to look for", described)
        self.assertIn("lang (['string', 'null'], optional)", described)
        self.assertIn('Call: call_tool(tool="wd.search_items", arguments={"query": "<query>"})', described)

    def test_a_call_is_wrapped_by_the_router(self):
        tools = self.router(self.answers(call_tool=("Q937 Albert Einstein", False)))
        result = self.run_(tools.call_tool("wd.search_items", {"query": "Einstein"}))
        self.assertEqual("Q937 Albert Einstein", result)
        self.assertEqual(("wd", "call_tool", {"name": "search_items", "arguments": {"query": "Einstein"}}),
                         tools._run.calls[-1])

    def test_the_old_double_nesting_still_arrives_once_nested(self):
        tools = self.router(self.answers(call_tool=("ok", False)))
        self.run_(tools.call_tool("wd.search_items",
                                  {"name": "search_items", "arguments": {"query": "Einstein"}}))
        self.assertEqual({"name": "search_items", "arguments": {"query": "Einstein"}}, tools._run.calls[-1][2])

    def test_a_bridge_failure_text_gets_the_schema_and_a_fix(self):
        failure = ("search_items reported an error:\nmissing query", False)
        tools = self.router(self.answers(call_tool=failure))
        answer = self.run_(tools.call_tool("wd.search_items", {"suche": "Einstein"}))
        self.assertTrue(answer.startswith("wd.search_items reported an error:\nmissing query"))
        self.assertIn("query (string, required)", answer)
        self.assertIn('Try: call_tool(tool="wd.search_items", arguments={"query": "Einstein"})', answer)

    def test_the_bridge_trio_stays_callable(self):
        tools = self.router(self.answers(call_tool=("ok", False)))
        self.assertEqual("ok", self.run_(tools.call_tool("wd.call_tool", {"name": "search_items"})))
        self.assertEqual(("wd", "call_tool", {"name": "search_items"}), tools._run.calls[-1])

    def test_an_unreachable_bridge_shows_its_own_three(self):
        tools = self.router({"list_tools": ("Could not reach the external server: timeout", False)})
        listing = self.run_(tools.find_tools(["wikidata"]))
        self.assertIn("wd.call_tool", listing)
        self.assertNotIn("search_items", listing)

    def test_find_functions_shows_what_is_behind_the_bridge(self):
        listing = self.run_(self.router(self.answers()).find_functions("wikidata"))
        self.assertIn("wd: 2 functions", listing)
        self.assertIn("search_items", listing)
        self.assertIn("execute_sparql", listing)

    def test_it_can_be_switched_off(self):
        tools = self.router(self.answers())
        tools.valves.expand_bridges = False
        self.assertIn("wd.call_tool", self.run_(tools.find_tools(["wikidata"])))
        self.assertEqual([], tools._run.calls)


class MixedUpCallTests(unittest.TestCase):
    """What Nova sent on 16.10. with OpenWebUI's tool search on: its call_tool
    shape (tool_name/params) at the router, the target's parameters next to
    `tool`, describe_tools(handles=…). Taken as meant, or answered with the
    call to copy — and none of it shows up in the schema."""

    def router(self):
        tools = cached_router(NEXTCLOUD, LAWS)
        tools._run = FakeRun({"get_paragraph": ("§ 823 text", False)})
        return tools

    def run_(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_openwebuis_tool_name_and_params_string_are_taken(self):
        tools = self.router()
        result = self.run_(tools.call_tool(tool_name="laws.get_paragraph",
                                           params='{"law": "bgb", "paragraph": "823"}'))
        self.assertEqual("§ 823 text", result)
        self.assertEqual(("laws", "get_paragraph", {"law": "bgb", "paragraph": "823"}), tools._run.calls[-1])

    def test_arguments_written_out_as_json_are_taken(self):
        tools = self.router()
        self.run_(tools.call_tool("laws.get_paragraph", '{"law": "bgb", "paragraph": "1"}'))
        self.assertEqual({"law": "bgb", "paragraph": "1"}, tools._run.calls[-1][2])

    def test_parameters_next_to_the_handle_are_moved_into_arguments(self):
        tools = self.router()
        self.run_(tools.call_tool(tool="laws.get_paragraph", law="bgb", paragraph="823"))
        self.assertEqual({"law": "bgb", "paragraph": "823"}, tools._run.calls[-1][2])

    def test_explicit_arguments_win_over_stray_ones(self):
        tools = self.router()
        self.run_(tools.call_tool(tool="laws.get_paragraph", arguments={"law": "bgb", "paragraph": "1"},
                                  paragraph="2"))
        self.assertEqual("1", tools._run.calls[-1][2]["paragraph"])

    def test_no_handle_names_the_stray_values_and_the_function_they_fit(self):
        tools = self.router()
        answer = self.run_(tools.call_tool(law="bgb", paragraph="823"))
        self.assertTrue(answer.startswith("Error: call_tool needs `tool`"))
        self.assertIn("You passed law, paragraph directly", answer)
        self.assertIn('call_tool(tool="laws.get_paragraph", arguments={"law": "bgb", "paragraph": "823"})', answer)
        self.assertEqual([], tools._run.calls)   # nothing guessed, nothing run

    def test_no_handle_and_nothing_fits_points_at_find_tools(self):
        answer = self.run_(self.router().call_tool(sparql="SELECT ?x"))
        self.assertIn('call_tool(tool="<instance.action>", arguments={"sparql": "SELECT ?x"})', answer)
        self.assertIn("find_tools()", answer)

    def test_arguments_that_are_not_an_object_are_refused_plainly(self):
        answer = self.run_(self.router().call_tool("laws.get_paragraph", "bgb 823"))
        self.assertIn("`arguments` must be an object", answer)

    def test_describe_takes_handles_and_a_single_string(self):
        tools = self.router()
        self.assertTrue(self.run_(tools.describe_tools(handles=["laws.get_paragraph"])).startswith("laws.get_paragraph"))
        self.assertTrue(self.run_(tools.describe_tools(tools="laws.get_paragraph")).startswith("laws.get_paragraph"))
        self.assertTrue(self.run_(tools.describe_tools('["laws.get_paragraph"]')).startswith("laws.get_paragraph"))

    def test_describe_without_handles_shows_how(self):
        answer = self.run_(self.router().describe_tools())
        self.assertIn('describe_tools(tools=["', answer)

    def test_the_schema_shows_none_of_it(self):
        # The aliases are taken, not advertised: one required handle and one
        # object, as before — anything more would sit in every prompt.
        exported = json.loads(ROUTER_PATH.read_text())[0]
        specs = {s["name"]: s["parameters"] for s in exported["specs"]}
        self.assertEqual({"tool", "arguments"}, set(specs["call_tool"]["properties"]))
        self.assertEqual(["tool"], specs["call_tool"]["required"])
        self.assertEqual({"tools"}, set(specs["describe_tools"]["properties"]))
        self.assertEqual(["tools"], specs["describe_tools"]["required"])
