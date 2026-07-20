"""Vault behaviour against GCS-shaped storage.

`test_vault.py` covers the local filesystem. These cover what the
filesystem cannot express: an index built from object metadata, and writes
that have to cope with the object moving underneath them.
"""

from datetime import date

import pytest

from bunpro_mcp.models import Item, MemoryState
from bunpro_mcp.storage import PreconditionFailed
from bunpro_mcp.vault import Vault, VaultError
from fakes import FakeInMemoryBackend

NOTE = (
    "---\nid: vocab-a-a\nkind: vocab\nsurface: a\nsource: manual\n"
    "first_seen: 2026-01-01\nstability: 1.0\n---\n\nprose the learner wrote\n"
)


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


def test_index_uses_note_id_metadata_without_reading_bodies():
    backend = FakeInMemoryBackend()
    backend.seed("Vocab/a.md", NOTE, note_id="vocab-a-a")

    vault = Vault(backend=backend)

    assert vault.get("vocab-a-a") is not None


def test_index_falls_back_to_parsing_when_metadata_missing():
    """A hand-uploaded object has no note_id — it must still be found."""
    backend = FakeInMemoryBackend()
    backend.seed("Vocab/a.md", NOTE, note_id=None)

    vault = Vault(backend=backend)

    assert vault.get("vocab-a-a") is not None


def test_object_with_neither_metadata_nor_parseable_body_is_skipped():
    backend = FakeInMemoryBackend()
    backend.seed("Vocab/a.md", NOTE, note_id="vocab-a-a")
    backend.seed("Vocab/junk.md", "---\nnot: valid\n---\n", note_id=None)

    vault = Vault(backend=backend)

    assert len(vault.load_all()) == 1


def test_duplicate_note_id_across_objects_raises():
    backend = FakeInMemoryBackend()
    backend.seed("Vocab/one.md", NOTE, note_id="vocab-a-a")
    backend.seed("Vocab/two.md", NOTE, note_id="vocab-a-a")

    with pytest.raises(VaultError):
        Vault(backend=backend)


def test_create_uses_create_only_precondition():
    backend = FakeInMemoryBackend()
    vault = Vault(backend=backend)

    assert vault.upsert(make_item()) is True
    # Writing the same key again with if_generation=None must be refused.
    with pytest.raises(PreconditionFailed):
        backend.write("Vocab/面倒くさい.md", "x", note_id="whatever", if_generation=None)


def test_write_memory_retries_and_preserves_the_other_writers_change():
    """The reason the retry lives in Vault rather than the backend.

    A 412 means someone else advanced the object. Re-sending the text we
    already built would erase their change; re-reading and re-applying
    merges. Here the concurrent writer adds prose, and it must survive.
    """
    backend = FakeInMemoryBackend()
    vault = Vault(backend=backend)
    item = make_item()
    vault.upsert(item, body="original prose")
    key = vault.path_for(item)

    # Next write loses the race; meanwhile the winner appends prose.
    backend.fail_next_write_for = key
    text, _ = backend.read(key)
    backend.seed(key, text.replace("original prose", "original prose\n\nWINNER"), item.id)

    vault.write_memory(item.id, MemoryState(last_review=date(2026, 7, 15), stability=6.4))

    reloaded, prose = vault.get_detail(item.id)
    assert reloaded.memory.stability == 6.4  # our change landed
    assert "WINNER" in prose  # and theirs was not clobbered


def test_write_gives_up_with_a_clear_error_after_repeated_conflicts():
    class AlwaysConflicts(FakeInMemoryBackend):
        def write(self, key, text, note_id, if_generation=None):
            if if_generation is not None:
                raise PreconditionFailed(key)
            return super().write(key, text, note_id, if_generation)

    backend = AlwaysConflicts()
    vault = Vault(backend=backend)
    item = make_item()
    vault.upsert(item)

    with pytest.raises(VaultError, match="another writer kept winning"):
        vault.write_memory(item.id, MemoryState(stability=2.0))


def test_upsert_appends_prose_and_write_memory_leaves_it_alone():
    backend = FakeInMemoryBackend()
    vault = Vault(backend=backend)
    item = make_item()

    vault.upsert(item, body="first note")
    vault.upsert(vault.get(item.id), body="second note")
    vault.write_memory(item.id, MemoryState(stability=9.0))

    reloaded, prose = vault.get_detail(item.id)
    assert "first note" in prose
    assert "second note" in prose
    assert reloaded.memory.stability == 9.0
