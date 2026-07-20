"""Auth is enforced at the ASGI layer, so that is where it gets tested —
no real server, no network, and no need to speak MCP to prove a request
was rejected before it reached a tool."""

import os

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bunpro_mcp.auth import BearerAuthMiddleware, require_token_env, token_ok

TOKEN = "correct-horse-battery-staple"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)

    async def protected(request):
        return JSONResponse({"reached": True})

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[Route("/mcp", protected), Route("/health", health)],
    )
    app.add_middleware(BearerAuthMiddleware)
    return TestClient(app)


def test_correct_token_reaches_the_app(client):
    resp = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"reached": True}


def test_missing_header_is_rejected(client):
    resp = client.get("/mcp")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-token",
        f"Basic {TOKEN}",  # right secret, wrong scheme
        "Bearer",  # scheme with no credentials
        f"{TOKEN}",  # bare token, no scheme
    ],
)
def test_wrong_or_malformed_credentials_are_rejected(client, header):
    assert client.get("/mcp", headers={"Authorization": header}).status_code == 401


def test_bearer_scheme_is_case_insensitive(client):
    """RFC 6750 says the scheme is case-insensitive; some clients send it
    lowercase."""
    resp = client.get("/mcp", headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200


def test_health_is_exempt_so_probes_work_without_a_credential(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_token_ok_rejects_everything_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    assert token_ok("anything") is False
    assert token_ok(None) is False


def test_require_token_env_raises_at_startup_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        require_token_env()


def test_require_token_env_returns_the_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    assert require_token_env() == TOKEN
    assert os.environ["MCP_AUTH_TOKEN"] == TOKEN
