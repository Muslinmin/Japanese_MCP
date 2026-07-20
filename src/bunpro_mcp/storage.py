"""Storage primitives behind the vault.

`vault.py` owns all note semantics — the id index, frontmatter, the
append-under-`## Context` rule. This module owns only the four dumb
operations that semantics reduce to: list, read, write, exists. There is
deliberately no `delete` (CONTRACT 1.8).

A *key* is a vault-relative POSIX path, e.g. `"Vocab/面倒くさい.md"`. It is
derived from the item's surface but is never the item's identity — the
`id` in the note's frontmatter is (CONTRACT 2). Backends may therefore
rekey freely as long as the id travels with the note.

Writes carry a *generation*: the version of the object the caller last
read. The backend compares it and refuses the write if the object moved
underneath. The generation is passed through by the caller and is never
cached in a backend, so there is no hidden state to go stale and no
ambiguity between "never read this key" (`if_generation=None`, meaning
create-only) and "read it at version 0".
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

NOTE_ID_METADATA_KEY = "note_id"


class PreconditionFailed(Exception):
    """The object changed between the caller's read and its write.

    Raised, never retried, by the backend: only the caller still holds the
    modification that produced the text, so only the caller can redo the
    read-modify-write against the newer version. Retrying down here would
    re-upload a stale snapshot and silently drop the other writer's change.
    """


@runtime_checkable
class VaultBackend(Protocol):
    def list(self) -> Iterable[tuple[str, str | None]]:
        """Yield `(key, note_id)`. `note_id` is `None` when the backend
        cannot supply it without fetching the body; the caller then falls
        back to parsing."""
        ...

    def read(self, key: str) -> tuple[str, int | None]:
        """Return `(text, generation)`. `generation` is `None` for backends
        with no versioning; it is opaque to the caller apart from being
        handed back to `write`."""
        ...

    def write(
        self,
        key: str,
        text: str,
        note_id: str,
        if_generation: int | None,
    ) -> None:
        """Write `text` at `key`.

        `if_generation` is the generation from the `read` that produced
        `text`, or `None` to mean "this is a new object; fail if one already
        exists". Raises `PreconditionFailed` if the precondition does not
        hold.
        """
        ...

    def exists(self, key: str) -> bool: ...


class LocalFSBackend:
    """Today's behaviour, unchanged: a directory of `.md` files.

    The filesystem has no generations, so `read` reports `None` and `write`
    ignores the precondition — `os.replace` is already atomic, which is the
    property the generation check exists to recover on GCS.
    """

    def __init__(self, root: Path) -> None:
        root = Path(root)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(
                f"Vault path does not exist or is not a directory: {root}"
            )
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def list(self) -> Iterable[tuple[str, str | None]]:
        for path in sorted(self.root.rglob("*.md")):
            yield path.relative_to(self.root).as_posix(), None

    def read(self, key: str) -> tuple[str, int | None]:
        return self._path(key).read_text(encoding="utf-8"), None

    def write(
        self,
        key: str,
        text: str,
        note_id: str,
        if_generation: int | None = None,
    ) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            os.unlink(tmp_name)
            raise

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class GCSBackend:
    """Google Cloud Storage, one object per note.

    Each object carries the note's id in custom metadata, so building the
    index costs one `list_blobs` call and zero body downloads. Objects
    without that metadata (a manual `gsutil cp`) still work — the caller
    parses the body to recover the id — they are just slower.
    """

    def __init__(self, bucket_name: str, client=None) -> None:
        from google.cloud import storage  # imported lazily: local dev has no GCS

        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self.bucket_name = bucket_name

    def list(self) -> Iterable[tuple[str, str | None]]:
        for blob in self._client.list_blobs(self._bucket):
            if not blob.name.endswith(".md"):
                continue
            metadata = blob.metadata or {}
            yield blob.name, metadata.get(NOTE_ID_METADATA_KEY)

    def read(self, key: str) -> tuple[str, int | None]:
        blob = self._bucket.get_blob(key)
        if blob is None:
            raise FileNotFoundError(f"No object at key: {key}")
        return blob.download_as_text(encoding="utf-8"), blob.generation

    def write(
        self,
        key: str,
        text: str,
        note_id: str,
        if_generation: int | None = None,
    ) -> None:
        from google.api_core import exceptions as gcs_exceptions

        blob = self._bucket.blob(key)
        blob.metadata = {NOTE_ID_METADATA_KEY: note_id}
        # 0 is GCS's "only if this object does not exist yet".
        precondition = 0 if if_generation is None else if_generation
        try:
            blob.upload_from_string(
                text,
                content_type="text/markdown; charset=utf-8",
                if_generation_match=precondition,
            )
        except gcs_exceptions.PreconditionFailed as exc:
            raise PreconditionFailed(
                f"{key} changed underneath us (expected generation {precondition})"
            ) from exc

    def exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists(self._client)
