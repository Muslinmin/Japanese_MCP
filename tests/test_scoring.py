from datetime import date, timedelta

from bunpro_mcp.models import Item, MemoryState, QueueEntry
from bunpro_mcp.scoring import (
    MASTERY_STABILITY_DAYS,
    apply_grade,
    clamp,
    is_mastered,
    priority,
    retrievability,
    to_queue_entry,
)

TODAY = date(2026, 7, 15)


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_retrievability_is_one_at_zero_elapsed():
    assert retrievability(stability=10.0, elapsed_days=0) == 1.0


def test_retrievability_falls_with_elapsed_time():
    r1 = retrievability(stability=10.0, elapsed_days=1)
    r5 = retrievability(stability=10.0, elapsed_days=5)
    r30 = retrievability(stability=10.0, elapsed_days=30)
    assert 1.0 > r1 > r5 > r30 > 0.0


def test_retrievability_rises_with_stability():
    low = retrievability(stability=1.0, elapsed_days=5)
    high = retrievability(stability=50.0, elapsed_days=5)
    assert high > low


def test_apply_grade_again_shrinks_stability_and_bumps_lapses():
    state = MemoryState(stability=10.0, difficulty=5.0, reps=3, lapses=1)
    new_state = apply_grade(state, 1, TODAY, "used it wrong")

    assert new_state.stability < state.stability
    assert new_state.stability == max(0.5, state.stability * 0.4)
    assert new_state.lapses == state.lapses + 1
    assert new_state.difficulty > state.difficulty
    assert new_state.reps == state.reps + 1
    assert new_state.last_review == TODAY
    assert new_state.last_error == "used it wrong"


def test_apply_grade_again_floors_stability_at_half_day():
    state = MemoryState(stability=1.0, difficulty=5.0)
    new_state = apply_grade(state, 1, TODAY, None)
    assert new_state.stability == 0.5


def test_apply_grade_easy_grows_stability_and_lowers_difficulty():
    state = MemoryState(stability=10.0, difficulty=5.0, reps=3, lapses=1)
    new_state = apply_grade(state, 4, TODAY, None)

    assert new_state.stability > state.stability
    assert new_state.difficulty < state.difficulty
    assert new_state.lapses == state.lapses
    assert new_state.last_error is None


def test_apply_grade_difficulty_is_clamped():
    state = MemoryState(stability=10.0, difficulty=1.0)
    new_state = apply_grade(state, 4, TODAY, None)
    assert 1.0 <= new_state.difficulty <= 10.0

    state2 = MemoryState(stability=10.0, difficulty=10.0)
    new_state2 = apply_grade(state2, 1, TODAY, None)
    assert 1.0 <= new_state2.difficulty <= 10.0


def test_apply_grade_stability_is_clamped_to_365():
    state = MemoryState(stability=300.0, difficulty=1.0)
    new_state = apply_grade(state, 4, TODAY, None)
    assert new_state.stability <= 365.0


def _item(
    id_: str,
    stability: float = 1.0,
    difficulty: float = 5.0,
    last_review: date | None = None,
    lapses: int = 0,
    first_seen: date = date(2026, 1, 1),
) -> Item:
    return Item(
        id=id_,
        kind="vocab",
        surface=id_,
        reading=id_,
        source="manual",
        first_seen=first_seen,
        memory=MemoryState(
            stability=stability,
            difficulty=difficulty,
            last_review=last_review,
            lapses=lapses,
        ),
    )


def test_to_queue_entry_never_reviewed_uses_days_since_first_seen():
    item = _item("vocab-x-x", first_seen=TODAY - timedelta(days=2))
    entry = to_queue_entry(item, TODAY)

    assert entry.days_since_review is None
    assert entry.days_since_first_seen == 2
    assert entry.retrievability == retrievability(1.0, 2)


def test_to_queue_entry_reviewed_uses_days_since_review():
    item = _item(
        "vocab-x-x",
        stability=8.0,
        last_review=TODAY - timedelta(days=3),
        first_seen=TODAY - timedelta(days=100),
    )
    entry = to_queue_entry(item, TODAY)

    assert entry.days_since_review == 3
    assert entry.retrievability == retrievability(8.0, 3)


def test_is_mastered_at_and_above_threshold():
    assert is_mastered(_item("vocab-a-a", stability=MASTERY_STABILITY_DAYS)) is True
    assert is_mastered(_item("vocab-a-a", stability=MASTERY_STABILITY_DAYS + 100)) is True


def test_is_not_mastered_below_threshold():
    assert is_mastered(_item("vocab-a-a", stability=MASTERY_STABILITY_DAYS - 0.1)) is False


def test_never_reviewed_default_item_is_not_mastered():
    assert is_mastered(_item("vocab-a-a")) is False  # default stability 1.0


def _entry(**overrides) -> QueueEntry:
    defaults = dict(
        id="vocab-x-x",
        kind="vocab",
        surface="x",
        reading="x",
        meaning=None,
        level=None,
        retrievability=0.5,
        priority=0.0,
        days_since_review=None,
        days_since_first_seen=100,
        lapses=0,
        last_error=None,
    )
    defaults.update(overrides)
    return QueueEntry(**defaults)


def test_priority_weights_new_item_higher():
    """Isolate w_new: same retrievability/lapses/cooldown, differ only in age."""
    new_entry = _entry(days_since_first_seen=1)
    old_entry = _entry(days_since_first_seen=100)

    assert priority(new_entry, TODAY) > priority(old_entry, TODAY)


def test_priority_weights_leech_higher():
    """Isolate w_leech: same retrievability/age/cooldown, differ only in lapses."""
    clean_entry = _entry(lapses=0)
    leech_entry = _entry(lapses=4)

    assert priority(leech_entry, TODAY) > priority(clean_entry, TODAY)


def test_priority_pushes_down_just_reviewed():
    """Isolate w_cool: same retrievability/age/lapses, differ only in recency."""
    stale_entry = _entry(days_since_review=10)
    fresh_entry = _entry(days_since_review=1)

    assert priority(fresh_entry, TODAY) < priority(stale_entry, TODAY)


def test_synthetic_ordering_looks_sane():
    """Leeches and brand-new items should float to the top; a just-reviewed
    easy item should sink to the bottom."""
    leech = to_queue_entry(
        _item(
            "vocab-leech-leech",
            stability=2.0,
            lapses=5,
            last_review=TODAY - timedelta(days=20),
            first_seen=TODAY - timedelta(days=200),
        ),
        TODAY,
    )
    brand_new = to_queue_entry(
        _item("vocab-new-new", first_seen=TODAY - timedelta(days=1)),
        TODAY,
    )
    just_reviewed = to_queue_entry(
        _item(
            "vocab-fresh-fresh",
            stability=50.0,
            lapses=0,
            last_review=TODAY - timedelta(days=1),
            first_seen=TODAY - timedelta(days=200),
        ),
        TODAY,
    )

    entries = sorted([leech, brand_new, just_reviewed], key=lambda e: priority(e, TODAY), reverse=True)
    assert entries[-1].id == "vocab-fresh-fresh"
