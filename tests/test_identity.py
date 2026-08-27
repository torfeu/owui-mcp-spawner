"""Verifying the forwarded end-user token, and keeping it apart per call.

The MCP Bearer token says a client may connect; it says nothing about which
person is sitting in front of that client. app/identity.py verifies the HS256
token OpenWebUI forwards for the signed-in user, and publishes the result in a
ContextVar for the duration of one tool call.

Two groups of tests. The first rejects every token that is not exactly right —
including the "alg" tricks a hand-written verifier gets wrong. The second is
about concurrency: one Tools instance serves all callers, so the identity must
never be reachable from anywhere but the running call.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

from app.identity import (CLOCK_SKEW_LEEWAY, Identity, IdentityError, get_current_identity,
                          identity_from_headers, identity_scope, verify_user_jwt)

SECRET = "a-shared-secret-long-enough-to-be-real"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint(secret: str = SECRET, alg: str = "HS256", **claims) -> str:
    """Build a token the way OpenWebUI does (utils/headers.py:_mint_forward_user_jwt)."""
    now = int(time.time())
    payload = {
        "sub": "user-1", "email": "anna@example.org", "name": "Anna", "role": "user",
        "iss": "open-webui", "iat": now, "exp": now + 300,
    }
    payload.update(claims)
    for key in [k for k, v in payload.items() if v is None]:
        del payload[key]
    header_b64 = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload_b64 = _b64(json.dumps(payload).encode())
    signature = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64(signature)}"


class TokenVerificationTests(unittest.TestCase):
    def test_a_valid_token_yields_the_claims(self):
        who = verify_user_jwt(mint(), SECRET)
        self.assertEqual(who.sub, "user-1")
        self.assertEqual(who.email, "anna@example.org")
        self.assertEqual(who.name, "Anna")
        self.assertEqual(who.role, "user")

    def test_a_tampered_payload_is_rejected(self):
        header, payload, signature = mint().split(".")
        forged = _b64(json.dumps({
            "sub": "someone-else", "iss": "open-webui",
            "exp": int(time.time()) + 300,
        }).encode())
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(f"{header}.{forged}.{signature}", SECRET)
        self.assertIn("signature", str(caught.exception))

    def test_a_token_signed_with_another_secret_is_rejected(self):
        with self.assertRaises(IdentityError):
            verify_user_jwt(mint(secret="not-the-shared-secret"), SECRET)

    def test_alg_none_is_rejected(self):
        """The classic hole: honouring the token's own algorithm field.

        A verifier that trusts "alg" can be told the token needs no signature
        at all — by the same party that sends the token.
        """
        header_b64 = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload_b64 = _b64(json.dumps({
            "sub": "intruder", "iss": "open-webui", "exp": int(time.time()) + 300,
        }).encode())
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(f"{header_b64}.{payload_b64}.", SECRET)
        self.assertIn("algorithm", str(caught.exception))

    def test_another_algorithm_name_is_rejected_even_with_a_valid_hmac(self):
        # HS256 bytes, RS256 label: accepting it would mean the label decides,
        # and the label is attacker-controlled.
        with self.assertRaises(IdentityError):
            verify_user_jwt(mint(alg="RS256"), SECRET)

    def test_an_expired_token_is_rejected(self):
        now = int(time.time())
        token = mint(iat=now - 3600, exp=now - 3600 + 300)
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(token, SECRET)
        self.assertIn("expired", str(caught.exception))

    def test_expiry_allows_for_clock_skew(self):
        """Two hosts, two clocks. A token that just expired must still pass.

        Without this the first symptom of a drifting clock is that every user
        is rejected — which reads like a wrong secret and sends you hunting in
        the wrong place.
        """
        now = int(time.time())
        who = verify_user_jwt(mint(exp=now - (CLOCK_SKEW_LEEWAY - 5)), SECRET)
        self.assertEqual(who.sub, "user-1")

    def test_a_token_from_the_future_is_rejected(self):
        now = int(time.time())
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(mint(iat=now + 3600, exp=now + 4000), SECRET)
        self.assertIn("future", str(caught.exception))

    def test_a_wrong_issuer_is_rejected(self):
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(mint(iss="somebody-else"), SECRET)
        self.assertIn("issuer", str(caught.exception))

    def test_a_token_without_sub_is_rejected(self):
        with self.assertRaises(IdentityError) as caught:
            verify_user_jwt(mint(sub=None), SECRET)
        self.assertIn("sub", str(caught.exception))

    def test_a_token_without_exp_is_rejected(self):
        with self.assertRaises(IdentityError):
            verify_user_jwt(mint(exp=None), SECRET)

    def test_garbage_is_rejected_without_raising_anything_else(self):
        for junk in ("", "   ", "not-a-token", "a.b", "a.b.c.d", "@@@.###.$$$"):
            with self.subTest(token=junk), self.assertRaises(IdentityError):
                verify_user_jwt(junk, SECRET)

    def test_without_a_configured_secret_nothing_verifies(self):
        """An unset secret must not turn into "everything is fine"."""
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            with self.assertRaises(IdentityError):
                verify_user_jwt(mint())


class HeaderExtractionTests(unittest.TestCase):
    def test_the_header_is_found_regardless_of_case(self):
        for key in ("X-OpenWebUI-User-Jwt", "x-openwebui-user-jwt", "X-OPENWEBUI-USER-JWT"):
            with self.subTest(header=key):
                who = identity_from_headers({key: mint()}, SECRET)
                self.assertEqual(who.sub, "user-1")

    def test_a_missing_header_is_an_error_not_an_anonymous_pass(self):
        with self.assertRaises(IdentityError):
            identity_from_headers({"authorization": "Bearer something"}, SECRET)

    def test_the_header_name_is_configurable(self):
        with patch.dict("os.environ", {"MCP_USER_JWT_HEADER": "X-Who"}):
            who = identity_from_headers({"x-who": mint()}, SECRET)
            self.assertEqual(who.sub, "user-1")


class PlainHeaderFallbackTests(unittest.TestCase):
    """The deliberate weaker option: OpenWebUI's unsigned user headers.

    It exists for installations whose threat model is "keep the users of my
    OpenWebUI apart", not "defend the port". Everything here is about that
    line staying visible: off by default, a signature always winning, and the
    resulting identity being marked as unsigned.
    """

    PLAIN = {
        "X-OpenWebUI-User-Id": "sub-anna",
        "X-OpenWebUI-User-Email": "anna@example.org",
        "X-OpenWebUI-User-Name": "Anna%20M%C3%BCller",
        "X-OpenWebUI-User-Role": "user",
    }

    def test_plain_headers_are_ignored_unless_switched_on(self):
        # The regression guard for the whole feature: a header anyone can write
        # must not become an identity by accident.
        with self.assertRaises(IdentityError):
            identity_from_headers(self.PLAIN, SECRET)

    def test_switched_on_they_identify_the_user(self):
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            who = identity_from_headers(self.PLAIN, SECRET)
        self.assertEqual(who.sub, "sub-anna")
        self.assertEqual(who.email, "anna@example.org")
        self.assertEqual(who.role, "user")

    def test_the_display_name_is_percent_decoded(self):
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            who = identity_from_headers(self.PLAIN, SECRET)
        self.assertEqual(who.name, "Anna Müller")

    def test_such_an_identity_is_marked_unsigned(self):
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            who = identity_from_headers(self.PLAIN, SECRET)
        self.assertFalse(who.signed)
        self.assertEqual(who.raw_token, "")
        # …and a real token is marked as what it is
        self.assertTrue(verify_user_jwt(mint(), SECRET).signed)

    def test_a_token_wins_over_the_plain_headers(self):
        """Both present: the signed one decides. Otherwise switching the
        fallback on would quietly downgrade every verified call."""
        headers = dict(self.PLAIN, **{"X-OpenWebUI-User-Jwt": mint(sub="from-token")})
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            who = identity_from_headers(headers, SECRET)
        self.assertEqual(who.sub, "from-token")
        self.assertTrue(who.signed)

    def test_a_broken_token_is_still_an_error_next_to_valid_headers(self):
        """No falling back to the weaker proof when the stronger one fails —
        that is how a forged token would buy an identity."""
        headers = dict(self.PLAIN, **{"X-OpenWebUI-User-Jwt": mint(secret="wrong")})
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            with self.assertRaises(IdentityError):
                identity_from_headers(headers, SECRET)

    def test_headers_without_a_user_id_identify_nobody(self):
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            with self.assertRaises(IdentityError):
                identity_from_headers({"X-OpenWebUI-User-Email": "anna@example.org"}, SECRET)

    def test_it_works_without_any_secret_at_all(self):
        # The point of the mode: no shared secret anywhere.
        import os
        with patch.dict("os.environ", {"MCP_USER_TRUST_HEADERS": "1"}):
            os.environ.pop("MCP_USER_JWT_SECRET", None)
            who = identity_from_headers(self.PLAIN)
        self.assertEqual(who.sub, "sub-anna")


class TokenSecrecyTests(unittest.TestCase):
    def test_the_raw_token_never_shows_up_in_a_repr(self):
        """Logs, tracebacks and debugger dumps all go through repr()."""
        who = verify_user_jwt(mint(), SECRET)
        self.assertNotIn(who.raw_token, repr(who))
        self.assertNotIn(who.raw_token, str(who))

    def test_public_claims_carry_no_token(self):
        who = verify_user_jwt(mint(), SECRET)
        self.assertEqual(
            set(who.public_claims()), {"sub", "email", "name", "role"}
        )


class IdentityScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_identity_is_gone_again_after_the_call(self):
        self.assertIsNone(get_current_identity())
        with identity_scope(Identity(sub="user-1")):
            self.assertEqual(get_current_identity().sub, "user-1")
        self.assertIsNone(get_current_identity())

    async def test_it_is_taken_down_even_when_the_tool_raises(self):
        with self.assertRaises(RuntimeError):
            with identity_scope(Identity(sub="user-1")):
                raise RuntimeError("tool blew up")
        self.assertIsNone(get_current_identity())

    async def test_a_synchronous_tool_in_a_worker_thread_sees_it(self):
        """Sync tools run through asyncio.to_thread, which copies the context.

        Worth pinning: if it ever stopped holding, every synchronous tool would
        silently fall back to "no user" instead of failing loudly.
        """
        def sync_tool():
            return get_current_identity().sub

        with identity_scope(Identity(sub="user-7")):
            self.assertEqual(await asyncio.to_thread(sync_tool), "user-7")

    async def test_fifty_concurrent_calls_by_two_users_do_not_mix(self):
        """The reason the identity lives in a ContextVar and not on the tool.

        Each task yields control in the middle of its "call", so the scheduler
        interleaves them: with a shared attribute, the second user's value
        would be read by the first user's call.
        """
        seen: list[tuple[str, str]] = []

        async def call(user: str):
            with identity_scope(Identity(sub=user)):
                await asyncio.sleep(0)              # hand control to another task
                first = get_current_identity().sub
                await asyncio.sleep(0.001)          # and again, mid-call
                seen.append((user, first))
                return get_current_identity().sub

        users = [f"user-{i % 2}" for i in range(50)]
        results = await asyncio.gather(*(call(u) for u in users))

        self.assertEqual(results, users)
        for expected, observed in seen:
            self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
