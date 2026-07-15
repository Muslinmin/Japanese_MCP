# Fix: real Bunpro export doesn't match the importer

**Audience: the coding agent.** The actual CSV produced by the zyaga Bunpro Exporter userscript does not match the column contract in `CONTRACT.md` §7. This document tells you exactly what to change. It amends `CONTRACT.md` §7 and its "open questions" note on Bunpro SRS stage — where this file and `CONTRACT.md` disagree, **this file wins for the import path only**; everything else in `CONTRACT.md` still governs.

Do not run a real import until all three changes below are in and `test_ingest.py` is updated and green.

---

## 1. What the export actually is

The userscript (`@version 1.0`, Greasy Fork script 553992) fetches the user's vocabulary from Bunpro's frontend API across five SRS buckets and writes a CSV with **exactly three columns**:

```
"word","description","progress"
```

| Actual column | Holds | Example |
|---------------|-------|---------|
| `word`        | The vocabulary surface form (kanji/kana as written) | 面倒くさい |
| `description` | The English meaning | troublesome, a pain |
| `progress`    | The SRS bucket label | Master |

`progress` is always one of: `Beginner`, `Adept`, `Seasoned`, `Expert`, `Master` (see the `LEVELS` array in the script).

### Consequences against the current contract

- **It is vocab only.** There is no `type` column because the script hardcodes `TYPE = "Vocab"`. Grammar never appears. The current importer keys `kind` off a `type` column, so as written **every row is skipped**.
- **There is no `reading`.** The script reads only `title` and `meaning` off each vocab object. The contract requires a reading for vocab or the row is skipped, and the vocab id is `vocab-{surface}-{reading}`. So again, **every row is skipped**.
- Two present columns are just renamed: `description` → `meaning`, `progress` → an SRS signal we currently ignore.

Net effect of running the import unchanged: `rows_read` > 0, `would_create` = 0, everything in `skipped`. That is the importer correctly refusing data it can't key. The dry-run rule is what surfaces this harmlessly — good.

There are **three** changes. Two make the import work at all. The third makes it not ruin your first two weeks of reviews.

---

## Change 1 — remap columns (in `ingest.py`)

Update `parse_bunpro_csv` (or wherever the Bunpro column knowledge lives — it must be this one file only, per `CONTRACT.md` §7) to the real shape:

| Real CSV column | Maps to field | Rule |
|-----------------|---------------|------|
| `word`          | `surface`     | Required. Missing → skip row with reason. |
| `description`   | `meaning`     | Optional. Blank allowed. |
| `progress`      | (see Change 3)| Drives seeded stability, not stored as-is. |
| *(none)*        | `kind`        | **Default to `"vocab"`** for this source. The export is vocab-only. |
| *(none)*        | `reading`     | Supplied by Change 2. |
| *(none)*        | `level`       | Not in this export. Leave null. Claude can backfill JLPT later via `get_item`/manual edit if wanted; do not block on it. |
| *(none)*        | `bunpro_url`  | Not in this export. Leave null. |
| *(none)*        | `first_seen`  | Not in this export. On create, use today (per `CONTRACT.md` §7 fallback). Never rewrite on update. |

Keep the existing "unknown columns ignored silently / missing expected column is a warning not a crash" behaviour. Since the whole column set changed, treat the three real columns as the expected set now.

Grammar is out of scope for this importer. If a grammar CSV is added later it will be a *different* parser (the pipe-separated `bunpro2csv` format) — do not try to make one function read both.

---

## Change 2 — get the reading, at the source

The importer cannot invent readings, and repeating the surface as the reading is a last resort that corrupts the id scheme and hurts conversational review later. The reading must come from the export. That means editing the **userscript**, not `ingest.py`.

### Step 2a — find the field (manual, one-time)

The script already holds each vocab object in `inc` (the `included` array) and reads only `title` and `meaning`:

```js
inc.map((v) => [ String(v.id), {
  title:   (v.attributes?.title   ?? "").trim(),
  meaning: (v.attributes?.meaning ?? "").trim(),
}])
```

Bunpro's vocab objects almost certainly carry a reading alongside `title` and `meaning`. To find its exact name, on the Bunpro **dashboard** page open the browser console and log one object mid-export, e.g. temporarily add `console.log(inc[0]?.attributes)` inside `fetchLevel` after `inc` is defined, run one export, and read the attribute keys. Likely candidates: `reading`, `yomikata`, `kana`, `furigana`. Use whatever is actually present.

### Step 2b — add a reading column (edit the userscript)

Once the field name is known, extend the map and the CSV writer. Add reading to the stored tuple:

```js
const vocabMap = new Map(inc.map((v) => [ String(v.id), {
  title:   (v.attributes?.title    ?? "").trim(),
  meaning: (v.attributes?.meaning  ?? "").trim(),
  reading: (v.attributes?.reading  ?? "").trim(),   // <-- real field name here
}]));
```

Carry it through the `all.set(...)` tuple and add it to the header and each row in the CSV builder:

```js
let csv = '"word","reading","description","progress"\n';
for (const [, [w, r, d, p]] of all)
  csv += `"${esc(w)}","${esc(r)}","${esc(d)}","${p}"\n`;
```

(where `esc(x) = x.replace(/"/g, '""')`). Then the CSV has a real `reading` column and Change 1's table gains a `reading → reading` row.

### Step 2c — fallback if the field genuinely isn't there

If no reading field exists on the object (unlikely), in `ingest.py` set `reading = surface` so the id still forms and the row imports. Log every such row into the `ImportReport.errors` list with a note like `"no reading available, used surface as reading: <word>"` so it's visible and fixable later, not silent. Do **not** make this the default path — try 2a/2b first.

---

## Change 3 — seed stability from `progress` (the one that matters)

**Why this is not optional.** Without it, every imported word arrives with the default `MemoryState` (stability 1.0, no `last_review`). That makes a word the learner has known cold for a year look identical to one met this morning. Import a few hundred learned words and the first weeks of `get_review_queue` are dominated by vocabulary the learner already knows perfectly. This is the single biggest threat to the tool being usable. `CONTRACT.md` filed SRS-stage seeding under "open questions"; the real export makes it required.

### The rule

On **creation only** (never on update of an existing item), map the `progress` bucket to a seeded memory state:

```python
BUCKET_SEED = {
    # bucket:      (stability_days, difficulty)
    "Beginner":  (2.0,   6.0),
    "Adept":     (7.0,   5.5),
    "Seasoned":  (21.0,  5.0),
    "Expert":    (60.0,  4.5),
    "Master":    (150.0, 4.0),
}
DEFAULT_SEED = (1.0, 5.0)   # unknown/blank bucket
```

Apply it when constructing the `Item`'s `MemoryState`:

```python
stability, difficulty = BUCKET_SEED.get(progress, DEFAULT_SEED)
memory = MemoryState(
    last_review=today,      # start the forgetting clock from import day
    stability=stability,
    difficulty=difficulty,
    reps=0,
    lapses=0,
)
```

Setting `last_review = today` is the other half of the fix: retrievability decays from *now* at the seeded stability, so a Master word sits quiet for months and a Beginner word resurfaces within days. Seeding stability without setting `last_review` would leave `elapsed_days` computed from `first_seen` and skew everything — set both.

### Boundaries

- **Creation only.** On update of an existing note, do not touch memory state — that invariant (`CONTRACT.md` §7, §3 ownership table) still holds absolutely. A re-import must never re-seed and wipe real review history.
- These numbers are a **starting guess**, same status as the placeholder scoring. The goal is that the first real queue *feels* right — known words scarce, newer words present. Eyeball it after import (see §4 below) and adjust the table, not the rest of the system.
- `difficulty` seeds are gentle (higher bucket = slightly easier) so leeches can still emerge from real review grades rather than being pre-judged.

---

## 4. Update tests, then verify

### `test_ingest.py`

Replace the fake CSV fixture with the real 3-column (soon 4-column) shape and assert:

- A well-formed vocab row **creates** a note with `kind="vocab"`, `meaning` from `description`, reading present.
- `progress="Master"` seeds high stability and `last_review=today`; `progress="Beginner"` seeds low stability. Assert the actual numbers from `BUCKET_SEED`.
- A blank/unknown `progress` falls back to `DEFAULT_SEED` and is not an error.
- A row missing `word` is skipped with a reason, not fatal.
- **Re-import idempotency with history:** create an item, run `apply_grade` on it to move its memory state, then re-import the same row and assert memory state is **unchanged** (no re-seed) and `first_seen` unchanged.
- Unknown extra columns are ignored.

### `fixtures/sample_bunpro.csv`

Rewrite to match reality:

```csv
"word","reading","description","progress"
"面倒くさい","めんどくさい","troublesome, a pain","Master"
"締め切り","しめきり","deadline","Adept"
"曖昧","あいまい","vague, ambiguous","Beginner"
```

(If Change 2 lands, include the `reading` column as above; if you're testing the 2c fallback path separately, add a row with an empty reading.)

### End-to-end check on real data

1. Export a real CSV from Bunpro (dashboard page, floating button).
2. `import_export(dry_run=True)` → confirm `would_create` is a healthy number and `skipped` is empty (or only genuinely bad rows). If everything is still skipped, Change 1 didn't take.
3. `import_export(dry_run=False)`.
4. `get_review_queue(limit=20, kind="vocab")` and **read the order by eye**. Master-bucket words should be largely absent from the top; Beginner/Adept words should dominate. If Master words are flooding the top, raise their seed stability in `BUCKET_SEED` and re-test on a fresh scratch vault.
5. Re-run the import and confirm counts show updates, not a second set of creates, and that a graded item's state survived.

---

## 5. Summary of files touched

| File | Change |
|------|--------|
| `bunpro-exporter.user.js` (userscript) | Add `reading` to the fetch map, tuple, CSV header and rows (Change 2). |
| `ingest.py` | Remap to `word`/`reading`/`description`/`progress`; default `kind="vocab"`; add `BUCKET_SEED` and seed memory on create only (Changes 1 & 3). |
| `tests/test_ingest.py` | New fixture shape + assertions for seeding, idempotency, skips. |
| `fixtures/sample_bunpro.csv` | Rewrite to the real columns. |

Nothing in `models.py`, `scoring.py`, `vault.py`, or `server.py` should need to change. If you find yourself editing those for this, stop — the leak has escaped `ingest.py` and the design is being violated.
