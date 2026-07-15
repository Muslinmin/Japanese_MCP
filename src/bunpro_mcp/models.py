"""Pure data models. No imports beyond the standard library and Pydantic."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["grammar", "vocab"]
Source = Literal["bunpro", "manual"]
Level = Literal["N5", "N4", "N3", "N2", "N1"]
GradeVal = Literal[1, 2, 3, 4]  # 1 again, 2 hard, 3 good, 4 easy
Progress = Literal["Beginner", "Adept", "Seasoned", "Expert", "Master"]  # Bunpro's own SRS bucket


class MemoryState(BaseModel):
    last_review: date | None = None
    stability: float = 1.0  # days
    difficulty: float = 5.0  # 1.0 - 10.0
    reps: int = 0
    lapses: int = 0
    last_error: str | None = None


class Item(BaseModel):
    id: str
    kind: Kind
    surface: str
    reading: str | None = None
    meaning: str | None = None
    level: Level | None = None
    source: Source
    bunpro_srs: int | None = None
    bunpro_url: str | None = None
    first_seen: date
    memory: MemoryState = Field(default_factory=MemoryState)
    suspended: bool = False
    tags: list[str] = Field(default_factory=list)


class QueueEntry(BaseModel):
    """An Item plus the numbers computed for it right now. Never persisted."""

    id: str
    kind: Kind
    surface: str
    reading: str | None
    meaning: str | None
    level: Level | None
    retrievability: float  # 0.0 - 1.0
    priority: float
    days_since_review: int | None
    days_since_first_seen: int
    lapses: int
    last_error: str | None


class Grade(BaseModel):
    item_id: str
    grade: GradeVal
    error_note: str | None = None  # one plain sentence, or null


class ItemDetail(QueueEntry):
    body: str  # the prose beneath the metadata block


class QueueResponse(BaseModel):
    generated_at: date
    total_items: int
    returned: int
    grammar_focus: list[str]
    queue: list[QueueEntry]


class GradeReport(BaseModel):
    updated: int
    unknown_ids: list[str]
    summary: list[str]


class AddReport(BaseModel):
    created: bool
    already_exists: bool
    item: Item


class ImportReport(BaseModel):
    dry_run: bool
    rows_read: int
    would_create: int
    would_update: int
    skipped: list[str]
    errors: list[str]
