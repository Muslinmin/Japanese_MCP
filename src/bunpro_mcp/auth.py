"""The auth seam.

One shared secret, checked in one place. Deliberately the whole story: if
this ever needs to become real OAuth, this file and its one wiring line in
`server.py` are what change — no tool and no lower module knows auth
exists.

Not FastMCP's built-in `auth=`: that path is an OAuth 2.1 resource server
(token verifiers, issuer URLs, scopes). A fixed bearer header is not that
shape, so it goes in as ASGI middleware around the app instead.
"""

from __future__ import annotations

import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

TOKEN_ENV_VAR = "MCP_AUTH_TOKEN"

# Liveness probes have to answer before any credential is presented, and
# the response says nothing an unauthenticated caller couldn't guess.
EXEMPT_PATHS = frozenset({"/health"})


def require_token_env() -> str:
    """Startup check: fail loudly at boot rather than 401ing every request."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} environment variable is not set. Generate a long "
            "random string and pass it as a secret; clients send it as "
            "'Authorization: Bearer <token>'."
        )
    return token


def token_ok(presented: str | None) -> bool:
    """Constant-time comparison against the configured token."""
    expected = os.environ.get(TOKEN_ENV_VAR)
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != "bearer" or not credentials:
        return None
    return credentials.strip()


class BearerAuthMiddleware:
    """Rejects unauthenticated requests before they reach the MCP app.

    Plain ASGI rather than `BaseHTTPMiddleware` so it stays out of the way
    of streamable-HTTP's long-lived response bodies.
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

        presented = _bearer(Headers(scope=scope).get("authorization"))
        if not token_ok(presented):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
