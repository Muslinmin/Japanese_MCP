"""The only module allowed to touch storage.

Knows the vault folder layout, the id -> key index, and note
serialisation. Everything else asks it for Items and hands Items back.

Storage itself lives behind `storage.VaultBackend` — a local directory or
a GCS bucket. Which one is in use is invisible above this module.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import frontmatter

from bunpro_mcp.models import Item, Kind, MemoryState
from bunpro_mcp.storage import (
    LocalFSBackend,
    PreconditionFailed,
    VaultBackend,
)

logger = logging.getLogger(__name__)

_KIND_TO_FOLDER: dict[Kind, str] = {"grammar": "Grammar", "vocab": "Vocab"}

# A write is read-modify-write, so two of them interleaving can lose an
# update. The tools are sync and today run inline on one event loop, so
# this lock is rarely contended — it earns its keep the moment the
# deployment runs more than one worker. Across *processes* (two Cloud Run
# revisions overlapping during a deploy) a lock is no help at all; that is
# what the generation preconditions below are for.
_WRITE_LOCK = threading.Lock()

MAX_WRITE_ATTEMPTS = 3


class VaultError(Exception):
    """Raised on vault-level invariant violations: bad root, corruption."""


def _item_to_metadata(item: Item) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "surface": item.surface,
        "reading": item.reading,
        "meaning": item.meaning,
        "level": item.level,
        "source": item.source,
        "bunpro_srs": item.bunpro_srs,
        "bunpro_url": item.bunpro_url,
        "first_seen": item.first_seen,
        "last_review": item.memory.last_review,
        "stability": item.memory.stability,
        "difficulty": item.memory.difficulty,
        "reps": item.memory.reps,
        "lapses": item.memory.lapses,
        "last_error": item.memory.last_error,
        "suspended": item.suspended,
        "tags": item.tags,
    }


def _metadata_to_item(metadata: dict) -> Item:
    memory = MemoryState(
        last_review=metadata.get("last_review"),
        stability=metadata.get("stability", 1.0),
        difficulty=metadata.get("difficulty", 5.0),
        reps=metadata.get("reps", 0),
        lapses=metadata.get("lapses", 0),
        last_error=metadata.get("last_error"),
    )
    return Item(
        id=metadata["id"],
        kind=metadata["kind"],
        surface=metadata["surface"],
        reading=metadata.get("reading"),
        meaning=metadata.get("meaning"),
        level=metadata.get("level"),
        source=metadata["source"],
        bunpro_srs=metadata.get("bunpro_srs"),
        bunpro_url=metadata.get("bunpro_url"),
        first_seen=metadata["first_seen"],
        memory=memory,
        suspended=metadata.get("suspended", False),
        tags=metadata.get("tags") or [],
    )


def _dumps(post: frontmatter.Post) -> str:
    return frontmatter.dumps(post) + "\n"


class Vault:
    def __init__(self, root: Path | None = None, backend: VaultBackend | None = None) -> None:
        if backend is None:
            if root is None:
                raise VaultError("Vault needs either a root path or a backend")
            try:
                backend = LocalFSBackend(Path(root))
            except FileNotFoundError as exc:
                raise VaultError(str(exc)) from exc
        self._backend = backend
        self._index: dict[str, str] = {}
        self._build_index()

    def _build_index(self) -> None:
        index: dict[str, str] = {}
        for key, note_id in self._backend.list():
            if note_id is None:
                # No id in the object's metadata — hand-uploaded, or a local
                # file, which has nowhere to put it. Pay for a parse.
                try:
                    text, _ = self._backend.read(key)
                    note_id = _metadata_to_item(frontmatter.loads(text).metadata).id
                except Exception as exc:
                    logger.warning("Skipping note with malformed metadata %s: %s", key, exc)
                    continue
            if note_id in index:
                raise VaultError(f"Duplicate id {note_id!r}: {index[note_id]} and {key}")
            index[note_id] = key
        self._index = index

    def _load(self, key: str) -> frontmatter.Post:
        text, _ = self._backend.read(key)
        return frontmatter.loads(text)

    def load_all(self) -> list[Item]:
        items: list[Item] = []
        for item_id, key in self._index.items():
            try:
                items.append(_metadata_to_item(self._load(key).metadata))
            except Exception as exc:
                logger.warning("Skipping note with malformed metadata %s: %s", key, exc)
        return items

    def get(self, item_id: str) -> Item | None:
        key = self._index.get(item_id)
        if key is None:
            return None
        try:
            return _metadata_to_item(self._load(key).metadata)
        except Exception as exc:
            logger.warning("Skipping note with malformed metadata %s: %s", key, exc)
            return None

    def get_detail(self, item_id: str) -> tuple[Item, str] | None:
        key = self._index.get(item_id)
        if key is None:
            return None
        post = self._load(key)
        return _metadata_to_item(post.metadata), post.content

    def upsert(self, item: Item, body: str | None = None) -> bool:
        """Create or update a note. Returns True if it was created.

        An update never rewrites prose; `body`, when given, is appended
        under a `## Context` heading (CONTRACT 1.4).
        """
        metadata = _item_to_metadata(item)

        with _WRITE_LOCK:
            if self._index.get(item.id) is not None:
                self._retrying_write(
                    item.id,
                    lambda post: self._apply_upsert(post, metadata, body),
                )
                return False

            # New note. Each attempt recomputes the key, because a 412 here
            # means someone claimed that key in between — the collision
            # suffix has to be worked out again against the new state.
            for attempt in range(MAX_WRITE_ATTEMPTS):
                key = self.path_for(item)
                content = f"## Context\n\n{body}\n" if body else ""
                post = frontmatter.Post(content, **metadata)
                try:
                    self._backend.write(key, _dumps(post), note_id=item.id, if_generation=None)
                except PreconditionFailed:
                    logger.warning(
                        "Key %s was claimed concurrently, retrying (%d/%d)",
                        key,
                        attempt + 1,
                        MAX_WRITE_ATTEMPTS,
                    )
                    continue
                self._index[item.id] = key
                return True

            raise VaultError(
                f"Could not create a note for {item.id!r} after "
                f"{MAX_WRITE_ATTEMPTS} attempts: the key kept being taken"
            )

    @staticmethod
    def _apply_upsert(post: frontmatter.Post, metadata: dict, body: str | None) -> None:
        post.metadata.update(metadata)
        if body:
            existing_prose = post.content.rstrip("\n")
            addition = f"## Context\n\n{body}\n"
            post.content = f"{existing_prose}\n\n{addition}" if existing_prose else addition

    def write_memory(self, item_id: str, state: MemoryState) -> None:
        def apply(post: frontmatter.Post) -> None:
            post.metadata["last_review"] = state.last_review
            post.metadata["stability"] = state.stability
            post.metadata["difficulty"] = state.difficulty
            post.metadata["reps"] = state.reps
            post.metadata["lapses"] = state.lapses
            post.metadata["last_error"] = state.last_error

        with _WRITE_LOCK:
            self._retrying_write(item_id, apply)

    def _retrying_write(self, item_id: str, modify) -> None:
        """Read, apply `modify`, write — retrying the *whole* cycle if the
        object moved underneath us.

        The re-read is the point: a stale snapshot re-sent against a fresh
        generation would succeed and quietly erase whatever the other
        writer had just put there. Reapplying `modify` to the newer text
        merges instead.
        """
        key = self._index.get(item_id)
        if key is None:
            raise VaultError(f"Unknown item id: {item_id!r}")

        for attempt in range(MAX_WRITE_ATTEMPTS):
            text, generation = self._backend.read(key)
            post = frontmatter.loads(text)
            modify(post)
            try:
                self._backend.write(
                    key, _dumps(post), note_id=item_id, if_generation=generation
                )
                return
            except PreconditionFailed:
                logger.warning(
                    "Concurrent write to %s, retrying (%d/%d)",
                    key,
                    attempt + 1,
                    MAX_WRITE_ATTEMPTS,
                )

        raise VaultError(
            f"Gave up writing {key!r} after {MAX_WRITE_ATTEMPTS} attempts: "
            "another writer kept winning the race"
        )

    def path_for(self, item: Item) -> str:
        """The storage key for an item, as a vault-relative POSIX path.

        For a known item this is wherever the note currently lives, even if
        it has been renamed — the id, not the filename, is the identity.
        """
        existing_key = self._index.get(item.id)
        if existing_key is not None:
            return existing_key

        folder = _KIND_TO_FOLDER[item.kind]
        candidate = f"{folder}/{item.surface}.md"
        if self._backend.exists(candidate):
            reading_part = f"-{item.reading}" if item.reading else ""
            candidate = f"{folder}/{item.surface}{reading_part}.md"
        return candidate
