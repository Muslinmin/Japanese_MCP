"""The front door. Declares tools, validates, hands off to lower modules.

Never write to stdout — stdout is the MCP protocol channel. Logging is
configured to stderr before anything else happens.
"""

import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Literal

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from bunpro_mcp import ingest  # noqa: E402
from bunpro_mcp.ingest import make_item  # noqa: E402
from bunpro_mcp.models import (  # noqa: E402
    AddReport,
    Grade,
    GradeReport,
    ImportReport,
    ItemDetail,
    Kind,
    Level,
    PracticePoolResponse,
    Progress,
    QueueResponse,
)
from bunpro_mcp.scoring import apply_grade, is_mastered, to_queue_entry  # noqa: E402
from bunpro_mcp.vault import Vault  # noqa: E402

logger = logging.getLogger(__name__)

GRAMMAR_FOCUS_MAX = 3


class ItemNotFound(Exception):
    pass


vault_path_raw = os.environ.get("VAULT_PATH")
if not vault_path_raw:
    raise RuntimeError(
        "VAULT_PATH environment variable is not set. Point it at the Obsidian "
        "vault folder that holds the Japanese notes."
    )
vault = Vault(Path(vault_path_raw))

mcp = FastMCP("bunpro")


@mcp.tool()
def get_review_queue(
    limit: int = 20,
    kind: Literal["grammar", "vocab", "both"] = "both",
) -> QueueResponse:
    """Get the Japanese items most in need of review right now, ordered by
    priority. Call this at the start of a review session. Returns grammar
    points and vocabulary with a freshness score for each, plus a note on
    how the learner last got it wrong."""
    today = date.today()
    all_items = [item for item in vault.load_all() if not item.suspended]

    all_entries = [to_queue_entry(item, today) for item in all_items]

    grammar_entries = sorted(
        (e for e in all_entries if e.kind == "grammar"),
        key=lambda e: e.priority,
        reverse=True,
    )
    grammar_focus = [e.id for e in grammar_entries[:GRAMMAR_FOCUS_MAX]]

    filtered = all_entries if kind == "both" else [e for e in all_entries if e.kind == kind]
    filtered.sort(key=lambda e: e.priority, reverse=True)
    queue = filtered[:limit]

    return QueueResponse(
        generated_at=today,
        total_items=len(vault.load_all()),
        returned=len(queue),
        grammar_focus=grammar_focus,
        queue=queue,
    )


@mcp.tool()
def get_practice_pool(
    limit: int = 60,
    kind: Literal["grammar", "vocab", "both"] = "both",
) -> PracticePoolResponse:
    """Get a pool of Japanese words and grammar the learner has ALREADY
    mastered, for active-use practice — the opposite of the review queue.
    Use this when the learner wants to be tested on or practice words they
    already know, not review what they're forgetting. Workflow: (1) ideate a
    concrete conversation theme or scenario (ordering at an izakaya,
    complaining about the weather, a job interview); (2) call this to get the
    mastered pool; (3) the pool is NOT pre-filtered by theme — from it, you
    pick the words and grammar that fit your theme, using each item's meaning;
    (4) propose the scenario and your chosen words to the learner and get
    their buy-in before starting; (5) run the practice conversation; (6) at
    the end, call submit_grades once for every item you practiced — grade
    fluent use 3 or 4, hesitation 2, and a blank or misuse 1 with a
    one-sentence error_note. Returns each item with its meaning and reading so
    you can select by theme."""
    today = date.today()
    mastered = [i for i in vault.load_all() if not i.suspended and is_mastered(i)]
    selected = mastered if kind == "both" else [i for i in mastered if i.kind == kind]
    selected.sort(key=lambda i: i.memory.stability, reverse=True)
    pool = [to_queue_entry(i, today) for i in selected[:limit]]

    return PracticePoolResponse(
        generated_at=today,
        total_mastered=len(mastered),
        returned=len(pool),
        pool=pool,
    )


@mcp.tool()
def get_item(item_id: str) -> ItemDetail:
    """Get everything known about one Japanese grammar point or word,
    including the learner's own notes from their vault."""
    detail = vault.get_detail(item_id)
    if detail is None:
        raise ItemNotFound(
            f"No item with id {item_id!r}. Call get_review_queue to see valid ids."
        )
    item, body = detail
    entry = to_queue_entry(item, date.today())
    return ItemDetail(**entry.model_dump(), body=body)


@mcp.tool()
def submit_grades(grades: list[Grade]) -> GradeReport:
    """Record how the learner performed on items during a review. Call this
    at the END of a review session, once, with every item you observed.
    Grade 1 = could not recall or used it wrong, 2 = struggled, 3 = correct,
    4 = effortless. Include a one-sentence error_note when they got it
    wrong, describing the specific mistake."""
    today = date.today()
    updated = 0
    unknown_ids: list[str] = []
    summary: list[str] = []

    for grade in grades:
        item = vault.get(grade.item_id)
        if item is None:
            unknown_ids.append(grade.item_id)
            continue

        new_state = apply_grade(item.memory, grade.grade, today, grade.error_note)
        vault.write_memory(item.id, new_state)
        updated += 1

        if grade.grade == 1:
            summary.append(f"{item.surface}: marked as a lapse, will resurface soon")
        else:
            summary.append(f"{item.surface}: next review in ~{round(new_state.stability)} days")

    return GradeReport(updated=updated, unknown_ids=unknown_ids, summary=summary)


@mcp.tool()
def add_item(
    surface: str,
    kind: Kind,
    reading: str | None = None,
    meaning: str | None = None,
    level: Level | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    progress: Progress | None = None,
) -> AddReport:
    """Add a Japanese grammar point or word the learner has just
    encountered. Fill in the reading, meaning, and JLPT level yourself from
    your own knowledge of Japanese — do not ask the learner for them unless
    the word is genuinely ambiguous. Put any context the learner gave you
    (where they met it, what confused them) into `note`. If the learner
    says they already partly know this word (e.g. "I'm Adept on this" or
    quotes a Bunpro SRS stage), pass that bucket as `progress` — one of
    Beginner, Adept, Seasoned, Expert, Master — so the review schedule
    starts from their actual familiarity instead of treating it as brand
    new. Leave `progress` unset for something they are meeting for the
    first time."""
    today = date.today()
    candidate = make_item(
        surface, kind, today, reading=reading, meaning=meaning, level=level, progress=progress
    )
    if tags:
        candidate.tags = tags

    existing = vault.get(candidate.id)
    if existing is not None:
        return AddReport(created=False, already_exists=True, item=existing)

    vault.upsert(candidate, body=note)
    created_item = vault.get(candidate.id)
    assert created_item is not None
    return AddReport(created=True, already_exists=False, item=created_item)


@mcp.tool()
def import_export(csv_path: str, dry_run: bool = True) -> ImportReport:
    """Import a Bunpro CSV export into the vault. ALWAYS run with
    dry_run=true first and show the learner the report before running for
    real."""
    today = date.today()
    return ingest.import_export(vault, Path(csv_path), today, dry_run)


if __name__ == "__main__":
    mcp.run()
