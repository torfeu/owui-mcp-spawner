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

    def test_exactly_the_three_meta_tools_are_exposed(self):
        # The whole point is a constant, tiny tool list in the prompt. A fourth
        # tool is a decision, not an accident.
        exported = json.loads(ROUTER_PATH.read_text())[0]
        self.assertEqual(
            {"find_tools", "describe_tools", "call_tool"},
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
