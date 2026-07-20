"""The signing primitives every OAuth credential rests on.

`test_oauth.py` drives the flow from the outside. These pin the
properties that flow silently depends on: a tampered token doesn't
verify, a token minted for one role can't be replayed as another, and
expiry is actually enforced.
"""

import time

import pytest

from bunpro_mcp.auth import (
    _spent_codes,
    _mark_code_spent,
    public_url,
    require_token_env,
    sign,
    token_ok,
    verify,
)

TOKEN = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)


def test_round_trip_returns_the_payload():
    token = sign("access", {"client_id": "abc", "scopes": ["bunpro"]}, ttl=60)
    payload = verify("access", token)

    assert payload["client_id"] == "abc"
    assert payload["scopes"] == ["bunpro"]


def test_tampered_payload_does_not_verify():
    token = sign("access", {"client_id": "abc"}, ttl=60)
    encoded, _, signature = token.partition(".")
    forged = sign("access", {"client_id": "attacker"}, ttl=60).partition(".")[0]

    assert verify("access", f"{forged}.{signature}") is None


@pytest.mark.parametrize("garbage", ["", "no-dot", "a.b", "....", "x" * 200])
def test_malformed_tokens_are_rejected_not_raised(garbage):
    assert verify("access", garbage) is None


def test_a_token_cannot_be_replayed_in_another_role():
    """A refresh token presented as an access token must not work — this
    is why every payload carries its purpose."""
    refresh = sign("refresh", {"client_id": "abc"}, ttl=60)

    assert verify("refresh", refresh) is not None
    assert verify("access", refresh) is None
    assert verify("code", refresh) is None


def test_expiry_is_enforced():
    assert verify("access", sign("access", {}, ttl=-1)) is None
    assert verify("access", sign("access", {}, ttl=60)) is not None


def test_ttl_none_means_no_expiry():
    """Client registrations never expire; one that lapsed would break the
    connector long after setup."""
    assert verify("client", sign("client", {"redirect_uris": []}, ttl=None)) is not None


def test_changing_the_secret_invalidates_existing_tokens(monkeypatch):
    token = sign("access", {"client_id": "abc"}, ttl=60)
    monkeypatch.setenv("MCP_AUTH_TOKEN", "a-different-secret")

    assert verify("access", token) is None


def test_verify_returns_none_rather_than_raising_when_unconfigured(monkeypatch):
    """A local run with no secret should 401, not 500."""
    token = sign("access", {"client_id": "abc"}, ttl=60)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)

    assert verify("access", token) is None


def test_token_ok_compares_the_static_secret():
    assert token_ok(TOKEN) is True
    assert token_ok("wrong") is False
    assert token_ok(None) is False


def test_token_ok_rejects_everything_when_no_secret_is_configured(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    assert token_ok("anything") is False
    assert token_ok(None) is False


def test_require_token_env_raises_at_startup_when_unset(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        require_token_env()


def test_public_url_defaults_to_localhost_and_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PORT", "9999")
    assert public_url() == "http://localhost:9999"

    monkeypatch.setenv("MCP_PUBLIC_URL", "https://example.run.app/")
    assert public_url() == "https://example.run.app"


def test_spent_code_tracking_does_not_grow_without_bound():
    """One entry per login, forever, would be a slow leak on an instance
    that stays warm. Entries past their expiry protect nothing — the
    signature has already stopped verifying."""
    _spent_codes.clear()
    for i in range(50):
        _mark_code_spent(f"stale-{i}", expires_at=time.time() - 1)
    _mark_code_spent("live", expires_at=time.time() + 60)

    assert set(_spent_codes) == {"live"}


def test_a_secret_stored_with_a_trailing_newline_still_works(monkeypatch):
    """`openssl rand | gcloud secrets create` keeps the trailing newline.
    A newline can't be typed into the login form or sent in a header, so
    without this every login fails with no indication why."""
    monkeypatch.setenv("MCP_AUTH_TOKEN", f"{TOKEN}\n")

    assert token_ok(TOKEN) is True
    assert require_token_env() == TOKEN
    # And the signing key must be the stripped one, so tokens issued
    # before and after a whitespace-only change stay interchangeable.
    assert verify("access", sign("access", {"a": 1}, ttl=60)) is not None


def test_whitespace_around_a_presented_token_is_tolerated(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    assert token_ok(f"  {TOKEN}\n") is True
