"""The OAuth 2.1 flow, driven end to end against the real ASGI app.

The point of these is that Claude's connector dialog will walk exactly
this path — discovery, dynamic registration, authorize, login, token
exchange — and any step returning the wrong shape fails silently as
"couldn't connect".
"""

import base64
import hashlib
import re
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

TOKEN = "the-server-token"


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch):
    """Builds the real ASGI app. `mcp` is a module-level singleton whose
    session manager refuses to start twice, so each app gets a fresh one —
    that is what lets a test stand the server up again and check a
    registration outlived it."""
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://localhost:8080")

    import bunpro_mcp.server as server

    from bunpro_mcp.vault import Vault

    server._vault = Vault(tmp_path)

    def build():
        server.mcp._session_manager = None
        return server.mcp.streamable_http_app()

    return build


@pytest.fixture
def client(make_app):
    with TestClient(make_app()) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def register(client) -> dict:
    resp = client.post(
        "/register",
        json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def authorize_and_login(client, registration, challenge) -> str:
    """Walk /authorize -> login page -> submit, and return the auth code."""
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque-state",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307), resp.text
    login_url = resp.headers["location"]
    request_token = parse_qs(urlparse(login_url).query)["request"][0]

    page = client.get("/login", params={"request": request_token})
    assert page.status_code == 200

    submitted = client.post(
        "/login",
        data={"request": request_token, "token": TOKEN},
        follow_redirects=False,
    )
    assert submitted.status_code == 302, submitted.text
    callback = urlparse(submitted.headers["location"])
    assert callback.netloc == "claude.ai"
    params = parse_qs(callback.query)
    assert params["state"] == ["opaque-state"]
    return params["code"][0]


# -- discovery -------------------------------------------------------------


def test_authorization_server_metadata_is_discoverable(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()

    assert meta["issuer"].rstrip("/") == "http://localhost:8080"
    assert meta["authorization_endpoint"].endswith("/authorize")
    assert meta["token_endpoint"].endswith("/token")
    assert meta["registration_endpoint"].endswith("/register")
    assert "S256" in meta["code_challenge_methods_supported"]
    assert "authorization_code" in meta["grant_types_supported"]


def test_protected_resource_metadata_points_at_the_issuer(client):
    meta = client.get("/.well-known/oauth-protected-resource").json()

    assert meta["resource"].rstrip("/") == "http://localhost:8080"
    assert any(s.rstrip("/") == "http://localhost:8080" for s in meta["authorization_servers"])


# -- registration ----------------------------------------------------------


def test_dynamic_registration_issues_a_usable_client(client):
    registration = register(client)

    assert registration["client_id"]
    assert registration["client_secret"]
    # No expiry: a registration that lapsed would silently break the
    # connector long after setup.
    assert registration.get("client_secret_expires_at") in (None, 0)


def test_registration_survives_a_restart(client, make_app):
    """Cloud Run scales to zero between sessions. A client registered
    before that must still be recognised after, or the user is asked to
    reconnect every time."""
    registration = register(client)

    with TestClient(make_app()) as fresh:
        _, challenge = pkce()
        resp = fresh.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)  # recognised, not invalid_client


# -- the full flow ---------------------------------------------------------


def test_full_authorization_code_flow_yields_a_working_token(client):
    registration = register(client)
    verifier, challenge = pkce()
    code = authorize_and_login(client, registration, challenge)

    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert tokens["token_type"].lower() == "bearer"
    assert tokens["refresh_token"]

    # The issued token must actually open the MCP endpoint.
    mcp_resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert mcp_resp.status_code == 200
    assert "get_review_queue" in mcp_resp.text


def test_refresh_token_returns_a_fresh_access_token(client):
    registration = register(client)
    verifier, challenge = pkce()
    code = authorize_and_login(client, registration, challenge)
    first = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    ).json()

    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]


# -- the ways it should fail ----------------------------------------------


def test_wrong_password_does_not_issue_a_code(client):
    registration = register(client)
    _, challenge = pkce()
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    request_token = parse_qs(urlparse(resp.headers["location"]).query)["request"][0]

    submitted = client.post(
        "/login",
        data={"request": request_token, "token": "wrong"},
        follow_redirects=False,
    )
    assert submitted.status_code == 401
    assert "location" not in submitted.headers


def test_pkce_verifier_must_match(client):
    registration = register(client)
    _, challenge = pkce()
    code = authorize_and_login(client, registration, challenge)

    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": secrets.token_urlsafe(48),  # not the real one
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_authorization_code_cannot_be_used_twice(client):
    registration = register(client)
    verifier, challenge = pkce()
    code = authorize_and_login(client, registration, challenge)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "client_id": registration["client_id"],
        "client_secret": registration["client_secret"],
        "code_verifier": verifier,
    }

    assert client.post("/token", data=payload).status_code == 200
    replayed = client.post("/token", data=payload)
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"


def test_mcp_endpoint_rejects_a_forged_token(client):
    resp = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer not-a-real-token",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


def test_static_deployment_secret_still_works_as_a_bearer(client):
    """Header-capable clients (Claude Code, Desktop) shouldn't have to do
    the OAuth dance against a server that now offers it."""
    resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200
    assert "get_review_queue" in resp.text


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_rotating_the_secret_invalidates_issued_tokens(client, monkeypatch):
    registration = register(client)
    verifier, challenge = pkce()
    code = authorize_and_login(client, registration, challenge)
    tokens = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    ).json()

    monkeypatch.setenv("MCP_AUTH_TOKEN", "a-completely-new-secret")

    resp = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


def test_login_page_rejects_a_tampered_request_blob(client):
    resp = client.get("/login", params={"request": "forged.blob"})
    assert resp.status_code == 400


def test_login_page_does_not_reflect_the_token_into_html(client):
    """The request blob is echoed into a hidden field — make sure it can't
    break out of the attribute."""
    registration = register(client)
    _, challenge = pkce()
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    request_token = parse_qs(urlparse(resp.headers["location"]).query)["request"][0]
    page = client.get("/login", params={"request": request_token}).text

    assert TOKEN not in page
    assert re.search(r'name="request" value="[A-Za-z0-9_\-.]+"', page)
