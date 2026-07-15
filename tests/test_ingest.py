from datetime import date
from pathlib import Path

import pytest

from bunpro_mcp.ingest import BUCKET_SEED, DEFAULT_SEED, import_export, make_item, parse_bunpro_csv
from bunpro_mcp.scoring import apply_grade
from bunpro_mcp.vault import Vault

TODAY = date(2026, 7, 15)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_bunpro.csv"


def write_csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "export.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_fixture_csv():
    items, skipped, errors = parse_bunpro_csv(FIXTURE, today=TODAY)

    assert len(items) == 3
    assert skipped == []
    assert errors == []
    assert all(i.kind == "vocab" for i in items)

    mendokusai = next(i for i in items if i.surface == "面倒くさい")
    assert mendokusai.reading == "めんどくさい"
    assert mendokusai.meaning == "troublesome, a pain"
    assert mendokusai.source == "bunpro"


def test_parse_seeds_stability_only_from_progress_bucket():
    items, _, _ = parse_bunpro_csv(FIXTURE, today=TODAY)

    master = next(i for i in items if i.surface == "面倒くさい")
    assert master.memory.stability == BUCKET_SEED["Master"]
    assert master.memory.difficulty == 5.0  # neutral default, never seeded
    assert master.memory.last_review == TODAY

    beginner = next(i for i in items if i.surface == "曖昧")
    assert beginner.memory.stability == BUCKET_SEED["Beginner"]
    assert beginner.memory.difficulty == 5.0
    assert beginner.memory.last_review == TODAY

    adept = next(i for i in items if i.surface == "締め切り")
    assert adept.memory.stability == BUCKET_SEED["Adept"]
    assert adept.memory.difficulty == 5.0


def test_parse_blank_progress_uses_default_seed_and_is_not_an_error(tmp_path: Path):
    csv_text = '"word","reading","description","progress"\n"猫","ねこ","cat",""\n'
    path = write_csv(tmp_path, csv_text)
    items, skipped, errors = parse_bunpro_csv(path, today=TODAY)

    assert len(items) == 1
    assert skipped == []
    assert errors == []
    assert items[0].memory.stability == DEFAULT_SEED
    assert items[0].memory.difficulty == 5.0


def test_parse_unknown_progress_uses_default_seed_and_is_not_an_error(tmp_path: Path):
    csv_text = '"word","reading","description","progress"\n"猫","ねこ","cat","Legendary"\n'
    path = write_csv(tmp_path, csv_text)
    items, skipped, errors = parse_bunpro_csv(path, today=TODAY)

    assert len(items) == 1
    assert errors == []
    assert items[0].memory.stability == DEFAULT_SEED


def test_parse_skips_row_missing_word(tmp_path: Path):
    csv_text = '"word","reading","description","progress"\n"","","cat","Master"\n'
    path = write_csv(tmp_path, csv_text)
    items, skipped, _ = parse_bunpro_csv(path, today=TODAY)

    assert items == []
    assert any("missing 'word'" in s for s in skipped)


def test_parse_missing_reading_column_falls_back_to_surface(tmp_path: Path):
    csv_text = '"word","description","progress"\n"猫","cat","Master"\n'
    path = write_csv(tmp_path, csv_text)
    items, skipped, errors = parse_bunpro_csv(path, today=TODAY)

    assert len(items) == 1
    assert items[0].reading == "猫"
    assert any("Missing expected column: 'reading'" in e for e in errors)
    assert any("no reading available, used surface as reading: 猫" in e for e in errors)


def test_parse_blank_reading_cell_falls_back_to_surface(tmp_path: Path):
    csv_text = (
        '"word","reading","description","progress"\n'
        '"猫","ねこ","cat","Master"\n'
        '"犬","","dog","Adept"\n'
    )
    path = write_csv(tmp_path, csv_text)
    items, _, errors = parse_bunpro_csv(path, today=TODAY)

    neko = next(i for i in items if i.surface == "猫")
    inu = next(i for i in items if i.surface == "犬")
    assert neko.reading == "ねこ"
    assert inu.reading == "犬"
    assert any("no reading available, used surface as reading: 犬" in e for e in errors)
    assert not any("猫" in e for e in errors if "no reading" in e)


def test_parse_unknown_columns_ignored_silently(tmp_path: Path):
    csv_text = (
        '"word","reading","description","progress","bogus"\n'
        '"猫","ねこ","cat","Master","surprise"\n'
    )
    path = write_csv(tmp_path, csv_text)
    items, skipped, errors = parse_bunpro_csv(path, today=TODAY)

    assert len(items) == 1
    assert skipped == []
    assert errors == []


def test_parse_missing_csv_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        parse_bunpro_csv(tmp_path / "does-not-exist.csv", today=TODAY)


def test_make_item_manual_vocab_defaults():
    item = make_item("猫", "vocab", TODAY, reading="ねこ", meaning="cat")
    assert item.id == "vocab-猫-ねこ"
    assert item.source == "manual"
    assert item.first_seen == TODAY
    assert item.memory.stability == 1.0
    assert item.memory.last_review is None


def test_make_item_manual_grammar_slugifies_surface():
    item = make_item("てしまう", "grammar", TODAY)
    assert item.id == "grammar-てしまう"
    assert item.source == "manual"


def test_make_item_vocab_without_reading_repeats_surface_in_id():
    item = make_item("パソコン", "vocab", TODAY)
    assert item.id == "vocab-パソコン-パソコン"


def test_make_item_with_progress_seeds_stability_and_last_review(tmp_path: Path):
    item = make_item("頑張る", "vocab", TODAY, reading="がんばる", progress="Adept")
    assert item.memory.stability == BUCKET_SEED["Adept"]
    assert item.memory.last_review == TODAY
    assert item.memory.difficulty == 5.0  # never seeded, even here


def test_make_item_with_progress_matches_csv_import_seeding():
    """A manually-added word stated at a bucket and an imported word from
    the same bucket must start on the identical schedule."""
    manual = make_item("面倒くさい", "vocab", TODAY, reading="めんどくさい", progress="Master")
    imported, _, _ = parse_bunpro_csv(FIXTURE, today=TODAY)
    imported_master = next(i for i in imported if i.surface == "面倒くさい")

    assert manual.memory.stability == imported_master.memory.stability
    assert manual.memory.last_review == imported_master.memory.last_review


def test_import_dry_run_writes_nothing(tmp_path: Path):
    vault = Vault(tmp_path)
    report = import_export(vault, FIXTURE, TODAY, dry_run=True)

    assert report.dry_run is True
    assert report.rows_read == 3
    assert report.would_create == 3
    assert report.would_update == 0
    assert report.skipped == []
    assert vault.load_all() == []


def test_import_real_run_creates_notes_with_seeded_memory(tmp_path: Path):
    vault = Vault(tmp_path)
    report = import_export(vault, FIXTURE, TODAY, dry_run=False)

    assert report.would_create == 3
    assert len(vault.load_all()) == 3

    master_item = vault.get("vocab-面倒くさい-めんどくさい")
    assert master_item.memory.stability == BUCKET_SEED["Master"]
    assert master_item.memory.difficulty == 5.0
    assert master_item.memory.last_review == TODAY


def test_import_second_run_updates_without_duplicating(tmp_path: Path):
    vault = Vault(tmp_path)
    import_export(vault, FIXTURE, TODAY, dry_run=False)

    report2 = import_export(vault, FIXTURE, TODAY, dry_run=False)

    assert report2.would_create == 0
    assert report2.would_update == 3
    assert len(vault.load_all()) == 3


def test_reimport_does_not_reseed_and_wipe_review_history(tmp_path: Path):
    vault = Vault(tmp_path)
    import_export(vault, FIXTURE, TODAY, dry_run=False)

    item_id = "vocab-面倒くさい-めんどくさい"
    item = vault.get(item_id)
    graded_state = apply_grade(item.memory, 3, TODAY, None)
    vault.write_memory(item_id, graded_state)

    later = date(2026, 8, 1)
    import_export(vault, FIXTURE, later, dry_run=False)

    reloaded = vault.get(item_id)
    assert reloaded.memory == graded_state
    assert reloaded.first_seen == TODAY


def test_import_updates_only_import_owned_fields_preserves_tags_and_prose(tmp_path: Path):
    vault = Vault(tmp_path)
    import_export(vault, FIXTURE, TODAY, dry_run=False)

    item_id = "vocab-面倒くさい-めんどくさい"
    item = vault.get(item_id)
    item.tags = ["leech"]
    item.suspended = True
    vault.upsert(item, body="my mnemonic")

    import_export(vault, FIXTURE, TODAY, dry_run=False)

    reloaded, prose = vault.get_detail(item_id)
    assert reloaded.tags == ["leech"]
    assert reloaded.suspended is True
    assert "my mnemonic" in prose
