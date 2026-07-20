"""A backend that behaves like GCS without needing one.

Exists to test the parts of the storage contract the local filesystem
cannot express: generation preconditions, id-in-metadata indexing, and the
lost-update race those two exist to prevent.
"""

from __future__ import annotations

from typing import Iterable

from bunpro_mcp.storage import PreconditionFailed


class FakeInMemoryBackend:
    def __init__(self) -> None:
        # key -> (text, generation, note_id)
        self._objects: dict[str, tuple[str, int, str | None]] = {}
        self._next_generation = 1
        # When set, the next write to this key fails its precondition once,
        # simulating another writer winning the race.
        self.fail_next_write_for: str | None = None
        self.write_count = 0

    def seed(self, key: str, text: str, note_id: str | None = None) -> None:
        """Put an object in place without going through `write` — stands in
        for a hand-uploaded file (no note_id) or a pre-existing note."""
        self._objects[key] = (text, self._next_generation, note_id)
        self._next_generation += 1

    def list(self) -> Iterable[tuple[str, str | None]]:
        for key in sorted(self._objects):
            _, _, note_id = self._objects[key]
            yield key, note_id

    def read(self, key: str) -> tuple[str, int | None]:
        if key not in self._objects:
            raise FileNotFoundError(key)
        text, generation, _ = self._objects[key]
        return text, generation

    def write(
        self,
        key: str,
        text: str,
        note_id: str,
        if_generation: int | None = None,
    ) -> None:
        self.write_count += 1

        if self.fail_next_write_for == key:
            self.fail_next_write_for = None
            raise PreconditionFailed(f"simulated conflict on {key}")

        existing = self._objects.get(key)
        if if_generation is None:
            if existing is not None:
                raise PreconditionFailed(f"{key} already exists")
        else:
            if existing is None:
                raise PreconditionFailed(f"{key} vanished")
            if existing[1] != if_generation:
                raise PreconditionFailed(
                    f"{key} is at generation {existing[1]}, caller had {if_generation}"
                )

        self._objects[key] = (text, self._next_generation, note_id)
        self._next_generation += 1

    def exists(self, key: str) -> bool:
        return key in self._objects
