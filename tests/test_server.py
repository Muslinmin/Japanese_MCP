"""Integration coverage for the get_practice_pool tool wiring.

server.py holds no logic worth unit-testing on its own, but get_practice_pool
combines a few moving parts (mastery filter, kind filter, stability sort, cap,
suspended exclusion) that are worth exercising against a real vault.
"""

import os
from datetime import date
from pathlib import Path

from bunpro_mcp.models import Item, MemoryState
from bunpro_mcp.vault import Vault


def _item(vault: Vault, id_: str, kind: str, stability: float, suspended: bool = False) -> None:
    vault.upsert(
        Item(
            id=id_,
            kind=kind,
            surface=id_,
            reading=id_,
            source="manual",
            first_seen=date(2026, 1, 1),
            memory=MemoryState(stability=stability),
            suspended=suspended,
        )
    )


def _load_server(tmp_path: Path):
    """Import server against a temp vault, then point it at the populated one."""
    os.environ["VAULT_PATH"] = str(tmp_path)
    os.environ.setdefault("MCP_AUTH_TOKEN", "test-token")
    import bunpro_mcp.server as server

    # The vault is built lazily and then cached, so overwrite the cache
    # rather than the env — each test has its own tmp_path.
    server._vault = Vault(tmp_path)
    return server


def test_practice_pool_returns_only_mastered_sorted_strongest_first(tmp_path: Path):
    scratch = Vault(tmp_path)
    _item(scratch, "vocab-weak-weak", "vocab", stability=5.0)
    _item(scratch, "vocab-mid-mid", "vocab", stability=30.0)
    _item(scratch, "vocab-strong-strong", "vocab", stability=120.0)
    _item(scratch, "grammar-solid-solid", "grammar", stability=60.0)

    server = _load_server(tmp_path)
    resp = server.get_practice_pool()

    ids = [e.id for e in resp.pool]
    assert "vocab-weak-weak" not in ids  # below the 21-day bar
    assert ids == ["vocab-strong-strong", "grammar-solid-solid", "vocab-mid-mid"]
    assert resp.total_mastered == 3
    assert resp.returned == 3


def test_practice_pool_filters_by_kind_but_counts_all_mastered(tmp_path: Path):
    scratch = Vault(tmp_path)
    _item(scratch, "vocab-mid-mid", "vocab", stability=30.0)
    _item(scratch, "grammar-solid-solid", "grammar", stability=60.0)

    server = _load_server(tmp_path)
    resp = server.get_practice_pool(kind="vocab")

    assert [e.id for e in resp.pool] == ["vocab-mid-mid"]
    assert resp.total_mastered == 2  # counts both kinds, like get_review_queue's total_items


def test_practice_pool_excludes_suspended_and_respects_limit(tmp_path: Path):
    scratch = Vault(tmp_path)
    _item(scratch, "vocab-a-a", "vocab", stability=100.0)
    _item(scratch, "vocab-b-b", "vocab", stability=90.0)
    _item(scratch, "vocab-hidden-hidden", "vocab", stability=200.0, suspended=True)

    server = _load_server(tmp_path)
    resp = server.get_practice_pool(limit=1)

    assert [e.id for e in resp.pool] == ["vocab-a-a"]  # strongest non-suspended, capped at 1
    assert resp.total_mastered == 2  # suspended item excluded from the count too
    assert resp.returned == 1


def test_review_queue_total_counts_suspended_items(tmp_path: Path):
    """total_items is everything in the vault, not just what's returned —
    the single-load rewrite has to keep counting suspended items."""
    scratch = Vault(tmp_path)
    _item(scratch, "vocab-a-a", "vocab", stability=10.0)
    _item(scratch, "vocab-hidden-hidden", "vocab", stability=10.0, suspended=True)

    server = _load_server(tmp_path)
    resp = server.get_review_queue()

    assert resp.total_items == 2
    assert [e.id for e in resp.queue] == ["vocab-a-a"]  # suspended not reviewable


def test_today_follows_the_configured_timezone(tmp_path: Path, monkeypatch):
    """Just past midnight in Singapore is still yesterday in UTC. The SRS
    day has to roll over for the learner, not for the container."""
    from datetime import datetime, timezone

    server = _load_server(tmp_path)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 20, 17, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(server, "datetime", FakeDatetime)

    monkeypatch.setenv("TZ", "Asia/Singapore")
    assert server._today() == date(2026, 7, 21)  # 01:30 the next day, locally

    monkeypatch.setenv("TZ", "UTC")
    assert server._today() == date(2026, 7, 20)
