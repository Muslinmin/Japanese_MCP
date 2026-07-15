# Bunpro MCP server — data models and contract

Current baseline. This document is the agreement between the parts — if the code and this file disagree, one of them is a bug.

---

## 1. Invariants

1. **The vault is the only store.** No database. Delete the vault, the system knows nothing.
2. **Only `vault.py` touches the filesystem.** No other module opens, reads, or writes a file.
3. **`scoring.py` never touches the filesystem or the clock.** Every input, including today's date, is passed in as an argument.
4. **Prose below the metadata block is yours and code never writes it.** Code may read it; code may append to a dedicated section; code may never rewrite or delete what you wrote.
5. **Freshness is never stored.** `retrievability` and `priority` are computed on every read and thrown away.
6. **`id` is immutable.** Once a note exists, its `id` never changes. The filename may change; the `id` may not.
7. **Import is a top-up, not a sync.** Bulk import may only write fields it owns (§3). It may never touch memory state, and it may never delete a note.
8. **Nothing in this system ever deletes a note.** Retirement is a flag, not a deletion.

---

## 2. Identity

```
grammar : grammar-{bunpro_slug}          e.g. grammar-te-shimau
vocab   : vocab-{surface}-{reading}      e.g. vocab-面倒くさい-めんどくさい
```

Grammar uses Bunpro's own slug (Bunpro is the authority on what counts as one grammar point). Manually-added grammar with no slug gets one generated from the surface — currently a plain slugify, not true romanization (no such library is in the dependency list); see `ARCHITECTURE.md` §7.

Vocab includes the reading in the key, not optional: 人気 is `にんき` (popularity) or `ひとけ` (a sign of life), different words that share a spelling. A vocab item with genuinely no reading (a katakana loanword) repeats the surface in the reading position.

**Filenames are derived from the `id` but are not the `id`.** `vault.py` builds an id→path index from note metadata, not filenames, at construction — renaming a file in Obsidian doesn't break anything. Suggested layout: `{vault}/Grammar/て しまう.md`, `{vault}/Vocab/面倒くさい.md`. On a filename collision, append the reading. Japanese characters in filenames are fine, don't sanitise them out.

---

## 3. The note file

```
---
id: vocab-面倒くさい-めんどくさい
kind: vocab
surface: 面倒くさい
reading: めんどくさい
meaning: troublesome, a pain
level: N4
source: bunpro
bunpro_srs: null
bunpro_url: null
first_seen: 2026-05-02
last_review: 2026-07-09
stability: 6.4
difficulty: 5.1
reps: 7
lapses: 2
last_error: read it as めんどうくさい
suspended: false
tags: [reading, leech]
---

## Notes

Anything here is mine. Mnemonics, mined sentences, links to 面倒.
The importer will not touch this. The grader will not touch this.
```

### Field ownership

| Field | Type | Owner | Required | Notes |
|---|---|---|---|---|
| `id` | string | creation | yes | Immutable. |
| `kind` | `grammar`\|`vocab` | creation | yes | Immutable. |
| `surface` | string | creation | yes | The written form. |
| `reading` | string | import | vocab only | Kana. Null allowed for grammar. Real CSV export has no reading column — falls back to repeating `surface` (§7). |
| `meaning` | string | import | no | Short English gloss. |
| `level` | `N5`…`N1`\|null | import | no | Not present in the real CSV export — always null via that path; settable manually. |
| `source` | `bunpro`\|`manual` | creation | yes | |
| `bunpro_srs` | int\|null | import | no | Not present in the real export (it carries a `progress` bucket label, not a numeric stage) — always null on import. |
| `bunpro_url` | string\|null | import | no | Not present in the real export — always null on import. |
| `first_seen` | date | creation | yes | ISO `YYYY-MM-DD`. |
| `last_review` | date\|null | system | no | Null = never reviewed by us. Creation-only exception below. |
| `stability` | float | system | yes | Days. §5. Creation-only exception below. |
| `difficulty` | float | system | yes | 1.0–10.0. **Never seeded by anything, ever** — starts neutral, moves only from real grades. |
| `reps` | int | system | yes | Total reviews. |
| `lapses` | int | system | yes | Reviews graded `again`. |
| `last_error` | string\|null | system | no | One sentence, written by Claude. |
| `suspended` | bool | you | yes | Excluded from the queue if true. |
| `tags` | list of string | you | no | Code reads, never writes. |

**The creation-only exception:** `stability` and `last_review` may be seeded from a stated Bunpro `progress` bucket (§7, `BUCKET_SEED`) — via CSV import or `add_item`'s `progress` argument, both going through the same helper so they agree. Never on update; an import or `add_item` call touching memory state on an *existing* item is a bug. `difficulty` is excluded from even this exception.

`retrievability` and `priority` are not stored anywhere — computed on every read from `stability`, `difficulty`, `last_review`, and today's date.

---

## 4. Python models (`models.py`)

```python
Kind      = Literal["grammar", "vocab"]
Source    = Literal["bunpro", "manual"]
Level     = Literal["N5", "N4", "N3", "N2", "N1"]
GradeVal  = Literal[1, 2, 3, 4]   # 1 again, 2 hard, 3 good, 4 easy
Progress  = Literal["Beginner", "Adept", "Seasoned", "Expert", "Master"]  # Bunpro's SRS bucket


class MemoryState(BaseModel):
    last_review: date | None = None
    stability: float = 1.0          # days
    difficulty: float = 5.0         # 1.0 – 10.0
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
    memory: MemoryState = MemoryState()
    suspended: bool = False
    tags: list[str] = []


class QueueEntry(BaseModel):
    """An Item plus the numbers computed for it right now. Never persisted."""
    id: str
    kind: Kind
    surface: str
    reading: str | None
    meaning: str | None
    level: Level | None
    retrievability: float           # 0.0 – 1.0
    priority: float
    days_since_review: int | None
    days_since_first_seen: int
    lapses: int
    last_error: str | None


class Grade(BaseModel):
    item_id: str
    grade: GradeVal
    error_note: str | None = None


class PracticePoolResponse(BaseModel):
    generated_at: date
    total_mastered: int      # everything over the mastery bar in the vault
    returned: int
    pool: list[QueueEntry]   # reuses QueueEntry; stability stays hidden
```

`Item` is on-disk truth; `QueueEntry` is the derived view Claude sees, deliberately without `stability`/`difficulty` (implementation detail Claude shouldn't reason about).

---

## 5. Scoring (`scoring.py`)

Pure functions, date always an argument, no clock reads.

```python
def retrievability(stability: float, elapsed_days: float) -> float:
    F, D = 19 / 81, -0.5
    return (1 + F * elapsed_days / stability) ** D
```
Never-reviewed item uses `elapsed_days = days since first_seen` and default stability 1.0 — puts it near the top of the queue within a day or two of being added, intentionally.

```python
def priority(entry, today) -> float:
    base = 1.0 - entry.retrievability
    w_new   = 1.3 if entry.days_since_first_seen < 14 else 1.0
    w_leech = 1.0 + 0.1 * min(entry.lapses, 5)
    w_cool  = 0.5 if (entry.days_since_review or 99) < 2 else 1.0
    return base * w_new * w_leech * w_cool
```
`w_new` favors recent items (consolidation window). `w_leech` floats persistent failures up. `w_cool` suppresses anything reviewed in the last two days — without it the queue collapses onto the same few leeches. Suspended items are excluded before scoring, not down-weighted.

```python
def apply_grade(state: MemoryState, grade: GradeVal, today: date, error_note: str | None) -> MemoryState:
    d = clamp(state.difficulty + 0.6 * (3 - grade), 1.0, 10.0)
    if grade == 1:
        s = max(0.5, state.stability * 0.4)
        lapses = state.lapses + 1
    else:
        ease = {2: 1.2, 3: 1.9, 4: 2.6}[grade]
        s = state.stability * ease * (11 - d) / 10
        lapses = state.lapses
    return MemoryState(last_review=today, stability=clamp(s, 0.5, 365.0), difficulty=d,
                        reps=state.reps + 1, lapses=lapses, last_error=error_note)
```

```python
MASTERY_STABILITY_DAYS = 21.0   # Bunpro "Seasoned" seed

def is_mastered(item: Item) -> bool:
    return item.memory.stability >= MASTERY_STABILITY_DAYS
```
The eligibility bar for themed practice (§6, `get_practice_pool`): stability alone, no date needed, no lapse guard. Same "starting guess, tune after a real session" status as `BUCKET_SEED` — nothing principled about 21.0 beyond matching the Seasoned bucket. Suspended-filtering is the caller's job, not this predicate's.

**Not real FSRS** — a transparent, predictable stand-in. Ordering should look sane on eyeball; tune constants with real data, swap in real FSRS later if desired — nothing else in the system has to change, since this file has no local dependencies.

---

## 6. Tool contract (`server.py`)

Docstrings shown are the actual interface text Claude reads — not documentation to gloss over.

### `get_review_queue(limit: int = 20, kind: Literal["grammar","vocab","both"] = "both") -> QueueResponse`
> Get the Japanese items most in need of review right now, ordered by priority. Call this at the start of a review session. Returns grammar points and vocabulary with a freshness score for each, plus a note on how the learner last got it wrong.

`QueueResponse`: `generated_at`, `total_items` (everything in vault, not just returned), `returned`, `grammar_focus` (top 2–3 grammar ids), `queue`. Empty vault → empty queue, not an error.

### `get_practice_pool(limit: int = 60, kind: Literal["grammar","vocab","both"] = "both") -> PracticePoolResponse`
> Get a pool of Japanese words and grammar the learner has ALREADY mastered, for active-use practice — the opposite of the review queue. Use this when the learner wants to be tested on or practice words they already know, not review what they're forgetting. Workflow: (1) ideate a concrete conversation theme or scenario (ordering at an izakaya, complaining about the weather, a job interview); (2) call this to get the mastered pool; (3) the pool is NOT pre-filtered by theme — from it, you pick the words and grammar that fit your theme, using each item's meaning; (4) propose the scenario and your chosen words to the learner and get their buy-in before starting; (5) run the practice conversation; (6) at the end, call submit_grades once for every item you practiced — grade fluent use 3 or 4, hesitation 2, and a blank or misuse 1 with a one-sentence error_note. Returns each item with its meaning and reading so you can select by theme.

The inverse of `get_review_queue`: pulls the *strongest* items, not the weakest. Eligibility is `is_mastered` (§5, `stability ≥ 21`); suspended items are excluded first. Sorted by `stability` descending (strongest first), capped at `limit`. `PracticePoolResponse`: `generated_at`, `total_mastered` (all mastered non-suspended items in the vault, kind-agnostic, like `total_items`), `returned`, `pool`. **Read-only** — this tool never writes; feedback flows back only through the model's follow-up `submit_grades` call. Empty pool (nothing mastered yet) → empty `pool`, not an error.

### `get_item(item_id: str) -> ItemDetail`
> Get everything known about one Japanese grammar point or word, including the learner's own notes from their vault.

`ItemDetail` = `QueueEntry` + prose `body`. Unknown id → raise, message includes the id and suggests calling `get_review_queue`.

### `submit_grades(grades: list[Grade]) -> GradeReport`
> Record how the learner performed on items during a review. Call this at the END of a review session, once, with every item you observed. Grade 1 = could not recall or used it wrong, 2 = struggled, 3 = correct, 4 = effortless. Include a one-sentence error_note when they got it wrong, describing the specific mistake.

**The tool that closes the loop** — without it, memory state never updates. For each grade: load item, `apply_grade`, write back. Unknown ids are collected in `unknown_ids`, not raised — one bad id shouldn't discard nineteen good ones. Date is never a tool argument; the server stamps its own `today`.

### `add_item(surface, kind, reading=None, meaning=None, level=None, note=None, tags=None, progress: Progress | None = None) -> AddReport`
> Add a Japanese grammar point or word the learner has just encountered. Fill in the reading, meaning, and JLPT level yourself from your own knowledge of Japanese — do not ask the learner for them unless the word is genuinely ambiguous. Put any context the learner gave you (where they met it, what confused them) into `note`. If the learner says they already partly know this word (e.g. "I'm Adept on this" or quotes a Bunpro SRS stage), pass that bucket as `progress` — one of Beginner, Adept, Seasoned, Expert, Master — so the review schedule starts from their actual familiarity instead of treating it as brand new. Leave `progress` unset for something they are meeting for the first time.

Sets `source: manual`, `first_seen: today`. `note` goes below the metadata block under `## Context`, never into a metadata field. `progress` unset → default `MemoryState()` (stability 1.0, no last_review). `progress` given → seeds `stability`/`last_review` via `BUCKET_SEED` (§7), `difficulty` still untouched. Existing id → no overwrite, no error, return `already_exists: true` with the existing item.

### `import_export(csv_path: str, dry_run: bool = True) -> ImportReport`
> Import a Bunpro CSV export into the vault. ALWAYS run with dry_run=true first and show the learner the report before running for real.

`ImportReport`: `dry_run`, `rows_read`, `would_create`, `would_update`, `skipped` (reasons), `errors`. On update: touches only import-owned fields (§3) — never `stability`, `difficulty`, `suspended`, prose.

---

## 7. CSV ingest contract (`ingest.py`)

**The real export (zyaga Bunpro Exporter userscript) is vocab-only**, columns `word`, `reading` (usually absent), `description`, `progress`. Every row → `kind="vocab"`. A grammar CSV export doesn't currently exist; if one shows up it needs a separate parser, not a shared function.

| CSV column | Maps to | Required | If missing |
|---|---|---|---|
| `word` | `surface` | yes | Row skipped, logged in `skipped`. |
| `reading` | `reading` | no | Falls back to repeating `surface`, logged in `errors` (non-fatal). |
| `description` | `meaning` | no | Left null. |
| `progress` | seeding | no | Blank/unrecognised → `DEFAULT_SEED`, not an error. |

`level`, `bunpro_srs`, `bunpro_url` have no source column — always null. `first_seen` has no source column — always today on creation.

### Seeding from `progress`

```python
BUCKET_SEED = {"Beginner": 2.0, "Adept": 7.0, "Seasoned": 21.0, "Expert": 60.0, "Master": 150.0}  # stability_days
DEFAULT_SEED = 1.0   # unknown or blank bucket
```

On **creation only**: seeds `stability` and stamps `last_review` to the import/add date (starting the forgetting clock from a real point, not `first_seen`). **`difficulty` is never seeded** — starts neutral, moves only from real grades; pre-judging difficulty from a Bunpro label before any review is guesswork the system shouldn't bake in. Numbers are a starting guess (same status as the rest of `scoring.py`) — adjust the table after eyeballing a real queue, not the rest of the system.

**Never on update.** Re-importing after real reviews must not reset memory state — that would erase review history.

**Shared with `add_item`'s `progress` argument (§6)** through one helper, `_seed_memory_state` — a manually-added word and an imported word from the same bucket land on the identical schedule.

Rules: unknown columns ignored silently; missing expected column is a warning in `errors`, not a crash; `first_seen` never rewritten on update regardless of what the CSV says; importer never touches memory state on an existing item, including the bucket seed above.

---

## 8. Errors

| Condition | Behaviour |
|---|---|
| Vault path not set or not a folder | Raise at startup, not at first call. |
| Note has malformed metadata | Skip that note, log to stderr, continue. |
| Unknown `item_id` in `get_item` | Raise, with the id in the message. |
| Unknown `item_id` in `submit_grades` | Collect in `unknown_ids`, process the rest. |
| CSV file not found | Raise, with the path in the message. |
| Two notes claim the same `id` | Raise at load — corruption, silence would make it worse. |

Logging goes to stderr, never stdout.

---

## 9. Open questions

- **Avoidance.** Reaching for たら when ば was the opening is arguably a lapse, but nothing in `Grade` can express it. Worth a fifth grade value or a separate flag once it's clear Claude reliably notices.
- **Retiring items.** `suspended` is a blunt instrument; something genuinely mastered should probably leave the queue on its own. *Partly addressed:* `get_practice_pool` (§6) gives mastered items a purpose beyond the review queue, but it doesn't yet remove them from it — a word can still surface in both.
- **`BUCKET_SEED` constants are a guess**, chosen to make the first real queue *feel* right rather than derived from anything principled. Revisit after eyeballing a real import.
