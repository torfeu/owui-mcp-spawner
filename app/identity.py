"""
Who is calling? — verified end-user identity for a single MCP tool call.

The MCP Bearer token (see app/auth.py) answers "may this client talk to this
server at all". It says nothing about *which person* sits in front of the
client: one token serves every user of an OpenWebUI installation. Anything a
tool does with credentials of its own therefore happens with the same rights
for everybody.

This module adds the missing half. OpenWebUI can forward the signed-in user as
an HS256-signed JWT (ENABLE_FORWARD_USER_INFO_HEADERS plus
FORWARD_USER_INFO_HEADER_JWT_SECRET on its side); the runner verifies that
token against the shared secret and publishes the result for the duration of
one tool call:

    from app.identity import get_current_identity
    who = get_current_identity()          # None when no identity was verified
    if who:
        ...                               # who.sub is the stable user id

Configuration (environment, read by the runner subprocess it inherits from the
manager):

  MCP_USER_JWT_SECRET   shared secret; unset means identity is never verified
  MCP_USER_JWT_HEADER   header carrying the token   (default X-OpenWebUI-User-Jwt)
  MCP_USER_JWT_ISSUER   required "iss" claim        (default open-webui)
  MCP_USER_TRUST_HEADERS  accept OpenWebUI's plain, unsigned user headers when
                        no signed token is present — see below

The plain-header fallback exists because not every installation needs a
signature. OpenWebUI can also forward the user as four ordinary headers
(X-OpenWebUI-User-Id and friends). Those are a *claim*, not a proof: anyone
who can reach the instance's port with the MCP Bearer token can write them
themselves, and that token is shared by every user. Turning this on therefore
separates the users of one OpenWebUI installation from each other — nothing
more. It is off by default and has to be chosen deliberately.

Two rules this module exists to enforce, both easy to get wrong by hand:

  * HS256 and nothing else. A JWT library that honours the token's own "alg"
    header can be talked into "none" by whoever sends the token.
  * The identity lives in a ContextVar, never on the Tools instance. One
    instance serves all callers concurrently — an attribute set on it would
    be read by whichever call happens to run next.
"""
import base64
import contextvars
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

DEFAULT_HEADER = "X-OpenWebUI-User-Jwt"
DEFAULT_ISSUER = "open-webui"

# OpenWebUI's unsigned variant, sent when it has no JWT secret of its own.
PLAIN_HEADERS = {
    "sub": "X-OpenWebUI-User-Id",
    "email": "X-OpenWebUI-User-Email",
    "name": "X-OpenWebUI-User-Name",
    "role": "X-OpenWebUI-User-Role",
}

# How an identity was established. Everything downstream treats both the same;
# the distinction exists so a diagnosis can say which one it was.
SOURCE_TOKEN = "signed token"
SOURCE_HEADERS = "unsigned headers"
SOURCE_MACHINE = "machine identity (configured)"

# Clocks between the OpenWebUI host and this one are rarely identical, and the
# forwarded token lives ~5 minutes. Without a little slack the first symptom of
# a drifting clock is "every user is rejected", which reads like a broken
# secret and sends you looking in the wrong place.
CLOCK_SKEW_LEEWAY = 60


class IdentityError(Exception):
    """Rejected token. The message names the reason, never the token."""


@dataclass(frozen=True)
class Identity:
    """A verified end user. Every field here survived signature checking."""

    sub: str
    email: str = ""
    name: str = ""
    role: str = ""
    issued_at: int = 0
    expires_at: int = 0
    # SOURCE_TOKEN or SOURCE_HEADERS — what this identity rests on.
    source: str = SOURCE_TOKEN
    # The original token, for forwarding to a downstream MCP server. repr=False
    # so no log line, traceback or debugger dump ever renders it by accident.
    raw_token: str = field(default="", repr=False, compare=False)

    def public_claims(self) -> dict:
        """The parts that are safe to show a user or write to a log."""
        return {"sub": self.sub, "email": self.email, "name": self.name, "role": self.role}

    @property
    def signed(self) -> bool:
        """True when a signature was checked, False for the plain-header mode."""
        return self.source == SOURCE_TOKEN


_current_identity: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "mcp_current_identity", default=None
)


def get_current_identity() -> Optional[Identity]:
    """The verified user of the tool call running right now, or None.

    Safe to call from anywhere inside a tool, including from a synchronous
    method: asyncio.to_thread copies the context into the worker thread.
    """
    return _current_identity.get()


@contextmanager
def identity_scope(identity: Optional[Identity]) -> Iterator[None]:
    """Publish *identity* for the duration of one tool call. Runner-only."""
    token = _current_identity.set(identity)
    try:
        yield
    finally:
        _current_identity.reset(token)


# ── configuration ─────────────────────────────────────────────────────────────

def configured_secret() -> Optional[str]:
    return os.environ.get("MCP_USER_JWT_SECRET") or None


def header_name() -> str:
    return os.environ.get("MCP_USER_JWT_HEADER") or DEFAULT_HEADER


def expected_issuer() -> str:
    return os.environ.get("MCP_USER_JWT_ISSUER") or DEFAULT_ISSUER


def trust_plain_headers() -> bool:
    """Whether unsigned user headers count as an identity. Off unless chosen."""
    return str(os.environ.get("MCP_USER_TRUST_HEADERS", "")).strip().lower() in ("1", "true", "yes")


def identity_configured() -> bool:
    """True when this server can establish an identity at all — either way."""
    return bool(configured_secret()) or trust_plain_headers()


# ── verification ──────────────────────────────────────────────────────────────

def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as e:
        raise IdentityError(f"token is not valid base64url: {e}") from e


def verify_user_jwt(token: str, secret: Optional[str] = None, *, now: Optional[int] = None) -> Identity:
    """Verify an HS256 user token and return the Identity, or raise IdentityError.

    Deliberately implemented against hmac/hashlib instead of a JWT library:
    every runner runs in its own venv (app/venv_manager.py installs five base
    packages), so a dependency here would have to be installed into all of
    them. The verification we need is one algorithm and five claims.
    """
    secret = secret if secret is not None else configured_secret()
    if not secret:
        raise IdentityError("no user-JWT secret configured on this server")
    if not token or not token.strip():
        raise IdentityError("empty token")

    parts = token.strip().split(".")
    if len(parts) != 3:
        raise IdentityError("malformed token (expected three dot-separated segments)")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, TypeError) as e:
        raise IdentityError(f"unreadable token header: {e}") from e
    if not isinstance(header, dict):
        raise IdentityError("unreadable token header")
    # Trusting the token's own algorithm field is the classic JWT hole: "none"
    # would make the signature optional, and an asymmetric name would let a
    # public key be used as the HMAC secret. We accept exactly one algorithm.
    if header.get("alg") != "HS256":
        raise IdentityError(f"unsupported algorithm {header.get('alg')!r} (only HS256 is accepted)")

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, _b64url_decode(signature_b64)):
        raise IdentityError("signature does not match")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError) as e:
        raise IdentityError(f"unreadable token payload: {e}") from e
    if not isinstance(claims, dict):
        raise IdentityError("unreadable token payload")

    issuer = expected_issuer()
    if claims.get("iss") != issuer:
        raise IdentityError(f"wrong issuer (expected {issuer!r})")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise IdentityError("token has no 'sub' — cannot identify the user")

    now = int(time.time()) if now is None else now

    expires_at = claims.get("exp")
    if not isinstance(expires_at, (int, float)):
        raise IdentityError("token has no usable 'exp'")
    if now > int(expires_at) + CLOCK_SKEW_LEEWAY:
        raise IdentityError("token has expired")

    issued_at = claims.get("iat")
    if issued_at is not None:
        if not isinstance(issued_at, (int, float)):
            raise IdentityError("token has an unusable 'iat'")
        if int(issued_at) > now + CLOCK_SKEW_LEEWAY:
            raise IdentityError("token is issued in the future — check the clocks")

    return Identity(
        sub=sub,
        email=str(claims.get("email") or ""),
        name=str(claims.get("name") or ""),
        role=str(claims.get("role") or ""),
        issued_at=int(issued_at) if isinstance(issued_at, (int, float)) else 0,
        expires_at=int(expires_at),
        raw_token=token.strip(),
    )


def identity_from_headers(headers: dict, secret: Optional[str] = None) -> Identity:
    """Establish the caller's identity from a header mapping. Raises IdentityError.

    A signed token always wins: when one is present it is verified, and a bad
    one is an error even if the plain headers next to it look fine. Only when
    no token was sent at all does the unsigned fallback come into play, and
    only if it was switched on.

    *headers* may be any case-insensitive-ish mapping of str to str; keys are
    compared lowercased, because ASGI, httpx and Starlette each hand them over
    a little differently.
    """
    lookup = {str(key).lower(): str(value) for key, value in (headers or {}).items()}

    token = lookup.get(header_name().lower(), "")
    if token:
        return verify_user_jwt(token, secret)

    if not trust_plain_headers():
        raise IdentityError(f"no {header_name()} header on the request")

    sub = lookup.get(PLAIN_HEADERS["sub"].lower(), "").strip()
    if not sub:
        raise IdentityError(
            f"neither {header_name()} nor {PLAIN_HEADERS['sub']} on the request"
        )
    return Identity(
        sub=sub,
        email=lookup.get(PLAIN_HEADERS["email"].lower(), "").strip(),
        # OpenWebUI percent-encodes the display name; a wrong name is cosmetic,
        # so a failed decode must not cost the identity.
        name=_unquote(lookup.get(PLAIN_HEADERS["name"].lower(), "").strip()),
        role=lookup.get(PLAIN_HEADERS["role"].lower(), "").strip(),
        source=SOURCE_HEADERS,
    )


def _unquote(value: str) -> str:
    try:
        from urllib.parse import unquote
        return unquote(value)
    except Exception:
        return value
