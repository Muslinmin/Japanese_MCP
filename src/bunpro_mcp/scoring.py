"""Pure scoring functions. No filesystem access, no clock reads.

This is not real FSRS. It is a transparent stand-in with the right shape,
chosen so the ordering it produces is eyeball-checkable. Swapping in real
FSRS later should only touch this file.
"""

from __future__ import annotations

from datetime import date

from bunpro_mcp.models import GradeVal, Item, MemoryState, QueueEntry


MASTERY_STABILITY_DAYS = 21.0  # Bunpro "Seasoned" seed. A guess; tune after a real session.


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def is_mastered(item: Item) -> bool:
    """True if the item is retention-solid enough to practice rather than review."""
    return item.memory.stability >= MASTERY_STABILITY_DAYS


def retrievability(stability: float, elapsed_days: float) -> float:
    """How likely you are to recall the item today, from 0 to 1."""
    F, D = 19 / 81, -0.5
    return (1 + F * elapsed_days / stability) ** D


def priority(entry: QueueEntry, today: date) -> float:
    base = 1.0 - entry.retrievability

    w_new = 1.3 if entry.days_since_first_seen < 14 else 1.0
    w_leech = 1.0 + 0.1 * min(entry.lapses, 5)
    w_cool = 0.5 if (entry.days_since_review if entry.days_since_review is not None else 99) < 2 else 1.0

    return base * w_new * w_leech * w_cool


def apply_grade(
    state: MemoryState, grade: GradeVal, today: date, error_note: str | None
) -> MemoryState:
    d = clamp(state.difficulty + 0.6 * (3 - grade), 1.0, 10.0)

    if grade == 1:
        s = max(0.5, state.stability * 0.4)
        lapses = state.lapses + 1
    else:
        ease = {2: 1.2, 3: 1.9, 4: 2.6}[grade]
        s = state.stability * ease * (11 - d) / 10
        lapses = state.lapses

    return MemoryState(
        last_review=today,
        stability=clamp(s, 0.5, 365.0),
        difficulty=d,
        reps=state.reps + 1,
        lapses=lapses,
        last_error=error_note,
    )


def to_queue_entry(item: Item, today: date) -> QueueEntry:
    """Turn a stored Item into a scored QueueEntry. Nothing here persists."""
    days_since_first_seen = (today - item.first_seen).days
    days_since_review = (
        (today - item.memory.last_review).days
        if item.memory.last_review is not None
        else None
    )
    elapsed_days = days_since_review if days_since_review is not None else days_since_first_seen

    entry = QueueEntry(
        id=item.id,
        kind=item.kind,
        surface=item.surface,
        reading=item.reading,
        meaning=item.meaning,
        level=item.level,
        retrievability=retrievability(item.memory.stability, elapsed_days),
        priority=0.0,
        days_since_review=days_since_review,
        days_since_first_seen=days_since_first_seen,
        lapses=item.memory.lapses,
        last_error=item.memory.last_error,
    )
    entry.priority = priority(entry, today)
    return entry
