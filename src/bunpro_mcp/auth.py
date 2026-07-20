"""The auth seam: a minimal OAuth 2.1 authorization server.

Claude's *Add custom connector* dialog authenticates remote MCP servers by
OAuth only — there is no field for a fixed `Authorization` header outside
enterprise-managed connectors. So the shared-secret bearer this file used
to hold isn't enough to connect from web or mobile, and this provides the
OAuth flow instead.

The MCP SDK already implements `/authorize`, `/token`, `/register`,
`/revoke` and both discovery documents. What it does not decide is where
tokens live and who counts as the user. That is this file:

**Nothing is stored.** Every credential — client registration,
authorization code, access token, refresh token — is a payload signed
with HMAC-SHA256 and handed to the client. Verification recomputes the
signature; there is no database and no in-process registry. This is not
cleverness for its own sake: Cloud Run scales to zero between review
sessions, so anything held in memory is gone by the next request, and a
connector whose registration evaporates asks the user to re-authorize
every single time. Signed tokens survive cold starts and would survive
several instances.

**The user is whoever knows `MCP_AUTH_TOKEN`.** This is a single-user
server, so `/login` asks for that one secret rather than pretending to
have accounts. The same secret seeds the signing key, which means
rotating it invalidates every issued token — correct behaviour, and worth
knowing before rotating.

That secret is also still accepted directly as a bearer token, so clients
that *can* set a header (Claude Code, Claude Desktop) keep working
unchanged against a server that now also speaks OAuth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "MCP_AUTH_TOKEN"
PUBLIC_URL_ENV_VAR = "MCP_PUBLIC_URL"

# Short enough that a leaked code in a redirect log is near-useless, long
# enough to survive a slow round trip through the browser.
AUTH_CODE_TTL = 60
ACCESS_TOKEN_TTL = 24 * 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
LOGIN_REQUEST_TTL = 10 * 60

SCOPE = "bunpro"

# Liveness probes have to answer before any credential is presented, and
# the response says nothing an unauthenticated caller couldn't guess.
EXEMPT_PATHS = frozenset({"/health"})

# Authorization codes are single-use, enforced here on a best effort. A
# cold start empties this, which leaves a replayable window no longer than
# AUTH_CODE_TTL. Enforcing it properly needs shared storage, which would
# mean a database this system otherwise does not have.
_spent_codes: set[str] = set()


# --------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------


def require_token_env() -> str:
    """Startup check: fail loudly at boot rather than 401ing every request."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} environment variable is not set. Generate a long "
            "random string and pass it as a secret; it is the password for the "
            "OAuth login page and the key every issued token is signed with."
        )
    return token


def public_url() -> str:
    """The externally reachable base URL, which OAuth discovery advertises.

    Must match what the client actually dialled — it becomes the issuer
    and the resource identifier, and clients compare it.
    """
    url = os.environ.get(PUBLIC_URL_ENV_VAR)
    if not url:
        port = os.environ.get("PORT") or "8080"
        return f"http://localhost:{port}"
    return url.rstrip("/")


def _signing_key() -> bytes:
    # Domain-separated so the key is not literally the deployment secret.
    return hashlib.sha256(b"bunpro-mcp/token-signing/v1|" + require_token_env().encode()).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(purpose: str, payload: dict[str, Any], ttl: int | None) -> str:
    """Serialise and sign a payload. `purpose` prevents a token minted for
    one role (say a refresh token) being replayed as another."""
    body = dict(payload, purpose=purpose)
    if ttl is not None:
        body["exp"] = int(time.time()) + ttl
    encoded = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_signing_key(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64(signature)}"


def verify(purpose: str, token: str | None) -> dict[str, Any] | None:
    """Return the payload if the signature, purpose and expiry all hold."""
    if not token or "." not in token:
        return None
    encoded, _, presented = token.partition(".")
    expected = hmac.new(_signing_key(), encoded.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(presented), expected):
            return None
        payload = json.loads(_unb64(encoded))
    except Exception:
        return None

    if payload.get("purpose") != purpose:
        return None
    expires_at = payload.get("exp")
    if expires_at is not None and time.time() > expires_at:
        return None
    return payload


def token_ok(presented: str | None) -> bool:
    """Constant-time comparison against the static deployment secret."""
    expected = os.environ.get(TOKEN_ENV_VAR)
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


# --------------------------------------------------------------------------
# the provider
# --------------------------------------------------------------------------


class StatelessOAuthProvider:
    """Implements the SDK's `OAuthAuthorizationServerProvider` with no store.

    Every `load_*` reduces to verifying a signature, so any instance can
    serve any request and a cold start loses nothing.
    """

    # -- client registration (RFC 7591) ------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        payload = verify("client", client_id)
        if payload is None:
            return None
        auth_method = payload["auth_method"]
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=self._client_secret(client_id) if auth_method != "none" else None,
            redirect_uris=payload["redirect_uris"],
            token_endpoint_auth_method=auth_method,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=payload.get("scope"),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Rewrite the SDK's random client_id into a signed, self-describing
        one, so the registration needs no storage to survive.

        The handler returns this same object to the caller, so mutating it
        here is what the client is issued.
        """
        if not client_info.redirect_uris:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="At least one redirect_uri is required",
            )

        client_info.client_id = sign(
            "client",
            {
                "redirect_uris": [str(uri) for uri in client_info.redirect_uris],
                "auth_method": client_info.token_endpoint_auth_method,
                "scope": client_info.scope,
                # Distinguishes two registrations with identical metadata,
                # so revoking one does not silently revoke the other.
                "nonce": secrets.token_urlsafe(8),
            },
            ttl=None,
        )
        client_info.client_secret = (
            self._client_secret(client_info.client_id)
            if client_info.token_endpoint_auth_method != "none"
            else None
        )
        client_info.client_secret_expires_at = None
        logger.info("Registered OAuth client %r", client_info.client_name or "unnamed")

    def _client_secret(self, client_id: str) -> str:
        """Derived, not stored — recomputable from the client_id alone."""
        return _b64(hmac.new(_signing_key(), b"client-secret|" + client_id.encode(), hashlib.sha256).digest())

    # -- authorization -----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Send the browser to our own login page.

        The whole authorization request travels in a signed blob, so the
        POST back can be trusted without a session cookie.
        """
        if not params.code_challenge:
            raise AuthorizeError(
                error="invalid_request",
                error_description="PKCE is required (code_challenge missing)",
            )

        request_token = sign(
            "authreq",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "resource": params.resource,
            },
            ttl=LOGIN_REQUEST_TTL,
        )
        return f"{public_url()}/login?{urlencode({'request': request_token})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        payload = verify("code", authorization_code)
        if payload is None or payload["client_id"] != client.client_id:
            return None
        if authorization_code in _spent_codes:
            logger.warning("Authorization code replayed; refusing")
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=payload["scopes"],
            expires_at=payload["exp"],
            client_id=payload["client_id"],
            code_challenge=payload["code_challenge"],
            redirect_uri=payload["redirect_uri"],
            redirect_uri_provided_explicitly=payload["redirect_uri_explicit"],
            resource=payload.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code in _spent_codes:
            raise TokenError(
                error="invalid_grant",
                error_description="Authorization code has already been used",
            )
        _spent_codes.add(authorization_code.code)
        return self._issue(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )

    # -- tokens ------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        claims = {"client_id": client_id, "scopes": scopes, "resource": resource}
        return OAuthToken(
            access_token=sign("access", claims, ttl=ACCESS_TOKEN_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=sign("refresh", claims, ttl=REFRESH_TOKEN_TTL),
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        # The deployment secret is accepted directly, so header-capable
        # clients keep working without going through the OAuth dance.
        if token_ok(token):
            return AccessToken(token=token, client_id="static-token", scopes=[SCOPE], expires_at=None)

        payload = verify("access", token)
        if payload is None:
            return None
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=payload.get("exp"),
            resource=payload.get("resource"),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        payload = verify("refresh", refresh_token)
        if payload is None or payload["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=payload.get("exp"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Narrowing is allowed, widening is not (RFC 6749 §6).
        granted = scopes or refresh_token.scopes
        if not set(granted).issubset(set(refresh_token.scopes)):
            raise TokenError(
                error="invalid_scope",
                error_description="Cannot request scopes beyond the original grant",
            )
        return self._issue(client_id=client.client_id, scopes=granted, resource=None)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Best effort only, and deliberately honest about it.

        Signed tokens carry their own validity; without shared storage
        there is nothing to write a revocation to. The endpoint exists
        because the spec advertises it. Real revocation is rotating
        MCP_AUTH_TOKEN, which changes the signing key and invalidates
        every token at once.
        """
        logger.info("Revocation requested for a %s token; expiry still governs", type(token).__name__)


# --------------------------------------------------------------------------
# the login page
# --------------------------------------------------------------------------

_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect bunpro-mcp</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; display: grid;
         place-items: center; min-height: 100vh; margin: 0; padding: 1.5rem; }}
  form {{ width: 100%; max-width: 22rem; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .25rem; }}
  p {{ margin: 0 0 1.25rem; opacity: .7; font-size: .9rem; line-height: 1.5; }}
  label {{ display: block; font-size: .85rem; margin-bottom: .4rem; }}
  input {{ width: 100%; padding: .6rem .7rem; font-size: 1rem; border-radius: .5rem;
          border: 1px solid rgba(128,128,128,.5); background: transparent;
          color: inherit; box-sizing: border-box; }}
  button {{ width: 100%; margin-top: .9rem; padding: .65rem; font-size: 1rem;
           border: 0; border-radius: .5rem; background: #4338ca; color: #fff;
           cursor: pointer; }}
  .error {{ color: #dc2626; font-size: .85rem; margin-top: .75rem; }}
</style></head>
<body><form method="post" action="/login">
  <h1>Connect bunpro-mcp</h1>
  <p>Enter the server token to let this client read and update your Japanese notes.</p>
  <input type="hidden" name="request" value="{request}">
  <label for="token">Server token</label>
  <input id="token" name="token" type="password" autocomplete="current-password"
         autofocus required>
  <button type="submit">Approve</button>
  {error}
</form></body></html>
"""


async def login_page(request) -> Response:
    request_token = request.query_params.get("request", "")
    if verify("authreq", request_token) is None:
        return HTMLResponse("<h1>This login link has expired.</h1>", status_code=400)
    return HTMLResponse(_LOGIN_HTML.format(request=_escape(request_token), error=""))


async def login_submit(request) -> Response:
    form = await request.form()
    request_token = str(form.get("request", ""))
    payload = verify("authreq", request_token)
    if payload is None:
        return HTMLResponse("<h1>This login link has expired.</h1>", status_code=400)

    if not token_ok(str(form.get("token", ""))):
        logger.warning("Failed login attempt at /login")
        return HTMLResponse(
            _LOGIN_HTML.format(
                request=_escape(request_token),
                error='<p class="error">That token is not correct.</p>',
            ),
            status_code=401,
        )

    code = sign(
        "code",
        {
            "client_id": payload["client_id"],
            "redirect_uri": payload["redirect_uri"],
            "redirect_uri_explicit": payload["redirect_uri_explicit"],
            "code_challenge": payload["code_challenge"],
            "scopes": payload["scopes"],
            "resource": payload.get("resource"),
        },
        ttl=AUTH_CODE_TTL,
    )
    return RedirectResponse(
        construct_redirect_uri(payload["redirect_uri"], code=code, state=payload.get("state")),
        status_code=302,
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


# --------------------------------------------------------------------------
# legacy: static-bearer middleware
# --------------------------------------------------------------------------


class BearerAuthMiddleware:
    """Guards routes with the static deployment secret.

    Superseded by the OAuth flow for the MCP endpoint, which the SDK
    protects itself. Kept for wrapping anything mounted outside that.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") in EXEMPT_PATHS or path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization")
        scheme, _, credentials = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not token_ok(credentials.strip()):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
