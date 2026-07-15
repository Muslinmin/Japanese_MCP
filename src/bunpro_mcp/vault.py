"""The only module allowed to touch the filesystem.

Knows the vault folder layout, the id -> path index, and note
serialisation. Everything else asks it for Items and hands Items back.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import frontmatter

from bunpro_mcp.models import Item, Kind, MemoryState

logger = logging.getLogger(__name__)

_KIND_TO_FOLDER: dict[Kind, str] = {"grammar": "Grammar", "vocab": "Vocab"}


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


class Vault:
    def __init__(self, root: Path) -> None:
        root = Path(root)
        if not root.exists() or not root.is_dir():
            raise VaultError(f"Vault path does not exist or is not a directory: {root}")
        self.root = root
        self._index: dict[str, Path] = {}
        self._build_index()

    def _build_index(self) -> None:
        index: dict[str, Path] = {}
        for path in sorted(self.root.rglob("*.md")):
            try:
                post = frontmatter.load(path)
                item = _metadata_to_item(post.metadata)
            except Exception as exc:
                logger.warning("Skipping note with malformed metadata %s: %s", path, exc)
                continue
            if item.id in index:
                raise VaultError(
                    f"Duplicate id {item.id!r}: {index[item.id]} and {path}"
                )
            index[item.id] = path
        self._index = index

    def load_all(self) -> list[Item]:
        items: list[Item] = []
        for item_id, path in self._index.items():
            try:
                post = frontmatter.load(path)
                items.append(_metadata_to_item(post.metadata))
            except Exception as exc:
                logger.warning("Skipping note with malformed metadata %s: %s", path, exc)
        return items

    def get(self, item_id: str) -> Item | None:
        path = self._index.get(item_id)
        if path is None:
            return None
        try:
            post = frontmatter.load(path)
            return _metadata_to_item(post.metadata)
        except Exception as exc:
            logger.warning("Skipping note with malformed metadata %s: %s", path, exc)
            return None

    def get_detail(self, item_id: str) -> tuple[Item, str] | None:
        path = self._index.get(item_id)
        if path is None:
            return None
        post = frontmatter.load(path)
        item = _metadata_to_item(post.metadata)
        return item, post.content

    def upsert(self, item: Item, body: str | None = None) -> bool:
        existing_path = self._index.get(item.id)
        metadata = _item_to_metadata(item)

        if existing_path is not None:
            post = frontmatter.load(existing_path)
            post.metadata.update(metadata)
            if body:
                existing_prose = post.content.rstrip("\n")
                addition = f"## Context\n\n{body}\n"
                post.content = f"{existing_prose}\n\n{addition}" if existing_prose else addition
            self._write_atomic(existing_path, post)
            return False

        path = self.path_for(item)
        content = f"## Context\n\n{body}\n" if body else ""
        post = frontmatter.Post(content, **metadata)
        self._write_atomic(path, post)
        self._index[item.id] = path
        return True

    def write_memory(self, item_id: str, state: MemoryState) -> None:
        path = self._index.get(item_id)
        if path is None:
            raise VaultError(f"Unknown item id: {item_id!r}")
        post = frontmatter.load(path)
        post.metadata["last_review"] = state.last_review
        post.metadata["stability"] = state.stability
        post.metadata["difficulty"] = state.difficulty
        post.metadata["reps"] = state.reps
        post.metadata["lapses"] = state.lapses
        post.metadata["last_error"] = state.last_error
        self._write_atomic(path, post)

    def path_for(self, item: Item) -> Path:
        existing_path = self._index.get(item.id)
        if existing_path is not None:
            return existing_path

        folder = self.root / _KIND_TO_FOLDER[item.kind]
        candidate = folder / f"{item.surface}.md"
        if candidate.exists():
            reading_part = f"-{item.reading}" if item.reading else ""
            candidate = folder / f"{item.surface}{reading_part}.md"
        return candidate

    def _write_atomic(self, path: Path, post: frontmatter.Post) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = frontmatter.dumps(post)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise
