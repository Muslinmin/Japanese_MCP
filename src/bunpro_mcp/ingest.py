"""Turns messy outside data into clean Items, then hands them to vault.

All Bunpro-format knowledge lives here and nowhere else, so when the
export format changes there is exactly one file to fix.

The real export (zyaga Bunpro Exporter userscript) is vocab-only, with
columns "word", "reading", "description", "progress" — not the richer
shape originally assumed.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from pathlib import Path

from bunpro_mcp.models import ImportReport, Item, Kind, Level, MemoryState, Progress
from bunpro_mcp.vault import Vault

EXPECTED_COLUMNS = ("word", "reading", "description", "progress")

# Seeded stability (days) by Bunpro SRS bucket, applied only when an item is
# first created — either by CSV import or by a manual add_item that states
# a starting bucket. An unseeded import makes a word known cold for a year
# look identical to one met this morning, which floods the early queue with
# reviews the learner doesn't need. Seeds stability only, never difficulty
# — difficulty starts neutral for every item and moves solely from the
# learner's own review grades; pre-judging how hard a word is before it has
# ever been reviewed is guesswork the system shouldn't bake in.
BUCKET_SEED: dict[str, float] = {
    "Beginner": 2.0,
    "Adept": 7.0,
    "Seasoned": 21.0,
    "Expert": 60.0,
    "Master": 150.0,
}
DEFAULT_SEED = 1.0  # unknown or blank bucket


def _seed_memory_state(progress: str | None, today: date) -> MemoryState:
    """Shared by the CSV importer and manual add_item so an imported word
    and a manually-added word stated at the same bucket start on the exact
    same schedule. Creation-only — never called on an update."""
    stability = BUCKET_SEED.get(progress or "", DEFAULT_SEED)
    return MemoryState(last_review=today, stability=stability)


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w-]", "", text, flags=re.UNICODE)
    return text or "item"


def _make_id(surface: str, kind: Kind, reading: str | None) -> str:
    if kind == "grammar":
        return f"grammar-{_slugify(surface)}"
    reading_part = reading or surface
    return f"vocab-{surface}-{reading_part}"


def make_item(
    surface: str,
    kind: Kind,
    today: date,
    reading: str | None = None,
    meaning: str | None = None,
    level: Level | None = None,
    progress: Progress | None = None,
) -> Item:
    """The single-item constructor, shared by add_item so the two ingest
    paths (bulk CSV, conversational add) can't drift apart.

    `progress` is optional and only meaningful when the learner states a
    starting Bunpro bucket for a word they already partly know (e.g. "add
    this, I'm Adept on it") — it seeds `stability` via the same BUCKET_SEED
    table the CSV importer uses, so the two paths agree. Left unset, the
    item starts as genuinely new: default stability, no last_review.
    """
    memory = _seed_memory_state(progress, today) if progress is not None else MemoryState()
    return Item(
        id=_make_id(surface, kind, reading),
        kind=kind,
        surface=surface,
        reading=reading,
        meaning=meaning,
        level=level,
        source="manual",
        first_seen=today,
        memory=memory,
    )


def parse_bunpro_csv(
    csv_content: str, today: date | None = None
) -> tuple[list[Item], list[str], list[str]]:
    """Parse a Bunpro export from CSV *text*. Returns (items, skipped, errors).

    Text rather than a path because the server that calls this runs on a
    different machine than the person holding the export — there is no
    server-side file for them to point at.

    The export is vocab-only (the userscript hardcodes this) — every row
    becomes `kind="vocab"`. `skipped` holds per-row reasons a row was
    dropped. `errors` holds structural warnings (a missing expected column,
    or a row that had to fall back to using the surface as its reading).
    Unknown columns are ignored silently.
    """
    effective_today = today if today is not None else date.today()

    items: list[Item] = []
    skipped: list[str] = []
    errors: list[str] = []

    reader = csv.DictReader(io.StringIO(csv_content, newline=""))
    fieldnames = set(reader.fieldnames or [])

    if not fieldnames:
        errors.append("CSV file has no header row")
        return items, skipped, errors

    for expected in EXPECTED_COLUMNS:
        if expected not in fieldnames:
            errors.append(f"Missing expected column: {expected!r}")

    for line_no, row in enumerate(reader, start=2):  # header is row 1
        surface = (row.get("word") or "").strip()
        if not surface:
            skipped.append(f"Row {line_no}: missing 'word'")
            continue

        meaning = (row.get("description") or "").strip() or None

        reading = (row.get("reading") or "").strip() or None
        if reading is None:
            reading = surface
            errors.append(f"no reading available, used surface as reading: {surface}")

        progress = (row.get("progress") or "").strip()
        memory = _seed_memory_state(progress, effective_today)

        items.append(
            Item(
                id=_make_id(surface, "vocab", reading),
                kind="vocab",
                surface=surface,
                reading=reading,
                meaning=meaning,
                level=None,
                source="bunpro",
                bunpro_srs=None,
                bunpro_url=None,
                first_seen=effective_today,
                memory=memory,
            )
        )

    return items, skipped, errors


def parse_bunpro_csv_path(
    csv_path: Path, today: date | None = None
) -> tuple[list[Item], list[str], list[str]]:
    """Read a CSV off disk and parse it. Convenience for local runs and
    fixtures; the server never has a path to give."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    return parse_bunpro_csv(csv_path.read_text(encoding="utf-8"), today=today)


def import_export(vault: Vault, csv_content: str, today: date, dry_run: bool) -> ImportReport:
    """Import a Bunpro CSV export. dry_run=True computes counts and writes
    nothing. On creation, seeds memory state from the `progress` bucket. On
    update of an existing item, touches only import-owned fields: reading,
    meaning. Never memory state, suspended, tags, or prose — a re-import
    must never re-seed and wipe real review history. first_seen on an
    existing item is never rewritten."""
    items, skipped, errors = parse_bunpro_csv(csv_content, today=today)

    would_create = 0
    would_update = 0

    for parsed in items:
        existing = vault.get(parsed.id)
        if existing is None:
            would_create += 1
            if not dry_run:
                vault.upsert(parsed)
            continue

        would_update += 1
        if dry_run:
            continue

        merged = existing.model_copy(
            update={
                "reading": parsed.reading if parsed.reading is not None else existing.reading,
                "meaning": parsed.meaning if parsed.meaning is not None else existing.meaning,
            }
        )
        vault.upsert(merged)

    return ImportReport(
        dry_run=dry_run,
        rows_read=len(items) + len(skipped),
        would_create=would_create,
        would_update=would_update,
        skipped=skipped,
        errors=errors,
    )
