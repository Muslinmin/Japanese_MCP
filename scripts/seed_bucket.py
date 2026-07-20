#!/usr/bin/env python3
"""One-shot: upload an existing local vault into the GCS bucket.

Run once, before the first deploy. Sets each object's `note_id` metadata
from the note's frontmatter, so the server's first cold start builds its
index from one list call instead of downloading and parsing every note.

Idempotent — re-running overwrites objects with the same local content.
Deliberately outside the package: it is not imported by the server, not
shipped in the image, and is the only writer besides the container that
should ever touch the bucket.

    python scripts/seed_bucket.py ~/Obsidian/Japanese --bucket my-vault
    python scripts/seed_bucket.py ~/Obsidian/Japanese --bucket my-vault --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import frontmatter
from google.cloud import storage

NOTE_ID_METADATA_KEY = "note_id"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path, help="local folder of .md notes")
    parser.add_argument("--bucket", required=True, help="destination GCS bucket")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be uploaded, write nothing",
    )
    args = parser.parse_args()

    root: Path = args.vault.expanduser()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 1

    bucket = storage.Client().bucket(args.bucket) if not args.dry_run else None

    uploaded = 0
    skipped: list[str] = []
    seen_ids: dict[str, str] = {}

    for path in sorted(root.rglob("*.md")):
        key = path.relative_to(root).as_posix()
        try:
            note_id = frontmatter.load(path).metadata["id"]
        except Exception as exc:
            skipped.append(f"{key}: no usable id ({exc})")
            continue

        # The server raises on a duplicate id at load, so catch it here
        # rather than after the bucket is already half-populated.
        if note_id in seen_ids:
            skipped.append(f"{key}: duplicate id {note_id!r}, also in {seen_ids[note_id]}")
            continue
        seen_ids[note_id] = key

        if args.dry_run:
            print(f"would upload {key}  (note_id={note_id})")
        else:
            blob = bucket.blob(key)
            blob.metadata = {NOTE_ID_METADATA_KEY: note_id}
            blob.upload_from_string(
                path.read_text(encoding="utf-8"),
                content_type="text/markdown; charset=utf-8",
            )
        uploaded += 1

    verb = "would upload" if args.dry_run else "uploaded"
    print(f"\n{verb} {uploaded} notes to gs://{args.bucket}")
    if skipped:
        print(f"skipped {len(skipped)}:", file=sys.stderr)
        for reason in skipped:
            print(f"  {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
