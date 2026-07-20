from datetime import date
from pathlib import Path

import pytest

from bunpro_mcp.models import Item, MemoryState
from bunpro_mcp.vault import Vault, VaultError


def make_item(id_="vocab-面倒くさい-めんどくさい", **overrides) -> Item:
    defaults = dict(
        id=id_,
        kind="vocab",
        surface="面倒くさい",
        reading="めんどくさい",
        meaning="troublesome",
        level="N4",
        source="manual",
        first_seen=date(2026, 1, 1),
    )
    defaults.update(overrides)
    return Item(**defaults)


def test_construction_raises_on_missing_root(tmp_path: Path):
    with pytest.raises(VaultError):
        Vault(tmp_path / "does-not-exist")


def test_upsert_creates_new_note(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item()

    created = vault.upsert(item)

    assert created is True
    key = vault.path_for(item)  # a vault-relative key, not an absolute path
    assert (tmp_path / key).exists()
    assert key.startswith("Vocab/")


def test_round_trip_write_then_read_back(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item()
    vault.upsert(item)

    vault2 = Vault(tmp_path)
    loaded = vault2.get(item.id)

    assert loaded is not None
    assert loaded.id == item.id
    assert loaded.surface == item.surface
    assert loaded.reading == item.reading
    assert loaded.meaning == item.meaning
    assert loaded.level == item.level
    assert loaded.source == item.source
    assert loaded.first_seen == item.first_seen


def test_upsert_update_touches_only_owned_fields_and_preserves_prose(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item()
    vault.upsert(item, body="met this at the konbini")

    existing = vault.get(item.id)
    assert existing is not None
    _, prose_before = vault.get_detail(item.id)

    updated_item = existing.model_copy(update={"meaning": "a real pain"})
    created = vault.upsert(updated_item)

    assert created is False
    reloaded, prose_after = vault.get_detail(item.id)
    assert reloaded.meaning == "a real pain"
    assert prose_after == prose_before  # prose untouched when body not given


def test_upsert_appends_body_never_clobbers(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item()
    vault.upsert(item, body="first note")

    existing = vault.get(item.id)
    vault.upsert(existing, body="second note")

    _, prose = vault.get_detail(item.id)
    assert "first note" in prose
    assert "second note" in prose


def test_write_memory_changes_only_memory_fields(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item(tags=["leech"], suspended=False)
    vault.upsert(item, body="mnemonic here")

    new_state = MemoryState(
        last_review=date(2026, 7, 15),
        stability=6.4,
        difficulty=5.1,
        reps=7,
        lapses=2,
        last_error="read it wrong",
    )
    vault.write_memory(item.id, new_state)

    reloaded, prose = vault.get_detail(item.id)
    assert reloaded.memory == new_state
    assert reloaded.meaning == item.meaning
    assert reloaded.tags == ["leech"]
    assert reloaded.suspended is False
    assert "mnemonic here" in prose


def test_malformed_note_is_skipped_not_fatal(tmp_path: Path):
    good = tmp_path / "Vocab"
    good.mkdir()
    (good / "good.md").write_text(
        "---\nid: vocab-a-a\nkind: vocab\nsurface: a\nsource: manual\n"
        "first_seen: 2026-01-01\n---\n",
        encoding="utf-8",
    )
    (good / "bad.md").write_text("---\nnot: valid\n---\nno required fields", encoding="utf-8")

    vault = Vault(tmp_path)
    items = vault.load_all()

    assert len(items) == 1
    assert items[0].id == "vocab-a-a"


def test_duplicate_id_raises_at_load(tmp_path: Path):
    folder = tmp_path / "Vocab"
    folder.mkdir()
    note = (
        "---\nid: vocab-a-a\nkind: vocab\nsurface: a\nsource: manual\n"
        "first_seen: 2026-01-01\n---\n"
    )
    (folder / "one.md").write_text(note, encoding="utf-8")
    (folder / "two.md").write_text(note, encoding="utf-8")

    with pytest.raises(VaultError):
        Vault(tmp_path)


def test_path_for_appends_reading_on_filename_collision(tmp_path: Path):
    vault = Vault(tmp_path)
    item_a = make_item(id_="vocab-同じ-おなじ", surface="同じ", reading="おなじ")
    vault.upsert(item_a)

    item_b = make_item(id_="vocab-同じ-べつ", surface="同じ", reading="べつ")
    key_b = vault.path_for(item_b)

    assert key_b != vault.path_for(item_a)
    assert "べつ" in key_b


def test_path_for_existing_item_returns_current_location_even_if_renamed(tmp_path: Path):
    vault = Vault(tmp_path)
    item = make_item()
    vault.upsert(item)

    original_path = tmp_path / vault.path_for(item)
    renamed_path = original_path.parent / "renamed-by-user.md"
    original_path.rename(renamed_path)

    vault2 = Vault(tmp_path)
    assert vault2.path_for(item) == "Vocab/renamed-by-user.md"
    assert vault2.get(item.id) is not None
