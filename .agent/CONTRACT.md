# Bunpro MCP server — data models and contract

Version 0.1. This document is the agreement between the parts. If the code and this file disagree, one of them is a bug.

---

## 1. Invariants

These are the rules the whole system leans on. Break one and something else quietly breaks later.

1. **The vault is the only store.** There is no database. Every fact the system knows lives in a `.md` file in the vault folder. If you delete the vault, the system knows nothing.
2. **Only `vault.py` touches the filesystem.** No other module opens, reads, or writes a file.
3. **`scoring.py` never touches the filesystem or the clock.** Every input, including today's date, is passed in as an argument.
4. **Prose below the metadata block is yours and code never writes it.** Code may read it; code may append to a dedicated section; code may never rewrite or delete what you wrote.
5. **Freshness is never stored.** `retrievability` and `priority` are computed on every read and thrown away. A stored freshness value is a stale freshness value.
6. **`id` is immutable.** Once a note exists, its `id` never changes. The filename may change; the `id` may not.
7. **Import is a top-up, not a sync.** Bulk import may only write fields it owns (section 3). It may never touch memory state, and it may never delete a note.
8. **Nothing in this system ever deletes a note.** Retirement is a flag, not a deletion.

---

## 2. Identity

The `id` is the primary key. It is a string, it is stable forever, and it is what every tool takes and returns.

```
grammar : grammar-{bunpro_slug}          e.g. grammar-te-shimau
vocab   : vocab-{surface}-{reading}      e.g. vocab-面倒くさい-めんどくさい
```

Grammar uses Bunpro's own slug because Bunpro is the authority on what counts as one grammar point, and its slugs are already unique and stable. When a grammar point is added manually and has no slug, generate one by romanising the surface form and slugging it.

Vocab includes the reading in the key, and this is not optional. 人気 is `にんき` (popularity) or `ひとけ` (a sign of life) and they are different words that happen to share a spelling. Keying on the surface alone would silently merge them and you would never notice. If a vocab item genuinely has no reading — a katakana loanword, say — repeat the surface in the reading position.

**Filenames** are derived from the `id` but are not the `id`. On load, `vault.py` builds an index from `id` to file path by reading the metadata of every note, and it trusts the metadata, not the filename. This means you can rename a file in Obsidian without breaking anything, which you will eventually do by accident.

Suggested layout:

```
{vault}/Japanese/Grammar/て しまう.md
{vault}/Japanese/Vocab/面倒くさい.md
```

On a filename collision, append the reading. Do not sanitise the Japanese out of filenames — Obsidian and every modern filesystem handle it fine.

---

## 3. The note file

A note is a plain text file. It begins with a block of `key: value` lines fenced by three dashes. Everything after the closing dashes is your prose and the code does not own it.

```
---
id: vocab-面倒くさい-めんどくさい
kind: vocab
surface: 面倒くさい
reading: めんどくさい
meaning: troublesome, a pain
level: N4
source: bunpro
bunpro_srs: 4
bunpro_url: https://bunpro.jp/vocabs/...
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

Every field has exactly one owner. Nothing else writes it. This table is the most important thing in this document.

| Field         | Type              | Owner    | Required | Notes |
|---------------|-------------------|----------|----------|-------|
| `id`          | string            | creation | yes      | Immutable. Set once, never rewritten. |
| `kind`        | `grammar`\|`vocab`| creation | yes      | Immutable. |
| `surface`     | string            | creation | yes      | The written form. |
| `reading`     | string            | import   | vocab only | Kana. Null is allowed for grammar. |
| `meaning`     | string            | import   | no       | Short English gloss. One line. |
| `level`       | `N5`…`N1`\|null   | import   | no       | |
| `source`      | `bunpro`\|`manual`| creation | yes      | Where the item first came from. |
| `bunpro_srs`  | int \| null       | import   | no       | Bunpro's own numeric SRS stage. Read-only signal, never used to overwrite our state. The real export (§7) carries no numeric stage, only a bucket label — this field is always null on import for now. |
| `bunpro_url`  | string \| null    | import   | no       | Not present in the real export (§7); always null on import for now. |
| `first_seen`  | date              | creation | yes      | ISO `YYYY-MM-DD`. |
| `last_review` | date \| null      | system   | no       | Null means never reviewed by us. Creation is allowed to set this to today, but **only** when seeding from a stated Bunpro bucket (§7, `BUCKET_SEED`) — via CSV import or `add_item`'s `progress` argument. Otherwise stays null until the first real grade. Never touched on update. |
| `stability`   | float             | system   | yes      | Days. See section 5. Creation is allowed to seed this from a stated Bunpro bucket (§7), same two callers as above. Never touched on update. |
| `difficulty`  | float             | system   | yes      | 1.0–10.0. **Never seeded, by anything, ever** — always starts at the neutral default and moves only from real review grades (§5). Not even the Bunpro bucket touches this one; see §7 for why. |
| `reps`        | int               | system   | yes      | Successful and unsuccessful reviews, total. |
| `lapses`      | int               | system   | yes      | Reviews graded `again`. |
| `last_error`  | string \| null    | system   | no       | One sentence, plain English, written by Claude. |
| `suspended`   | bool              | you      | yes      | If true, excluded from the queue entirely. |
| `tags`        | list of string    | you      | no       | Obsidian tags. Code reads, never writes. |

Read that table as three sets of hands. **Import** writes the Bunpro facts and only those. **System** writes the memory state and only that — with one narrow exception: on **creation**, `stability` and `last_review` may be seeded from a stated Bunpro bucket (§7, `BUCKET_SEED`), because a freshly-imported "Master" word and a freshly-imported "Beginner" word are not equally fresh in the learner's head, and treating them as identical floods the first sessions with reviews of words already known cold. Two callers can trigger this seeding — CSV import and `add_item`'s `progress` argument (§6) — sharing the exact same table so they agree. `difficulty` is excluded from that exception; it is never seeded by anything. And the exception itself is creation-only — an import or `add_item` call touching memory state on an *existing* item is a bug, full stop. A grader that touches `meaning` is a bug.

### Fields that deliberately do not exist

`retrievability` and `priority` are not stored anywhere. They are computed on every read from `stability`, `difficulty`, `last_review`, and today's date. If you ever find yourself wanting to cache them, you have made an ordering bug for yourself.

---

## 4. Python models

In `models.py`. No imports beyond the standard library and Pydantic. No behaviour beyond validation.

```python
Kind      = Literal["grammar", "vocab"]
Source    = Literal["bunpro", "manual"]
Level     = Literal["N5", "N4", "N3", "N2", "N1"]
GradeVal  = Literal[1, 2, 3, 4]   # 1 again, 2 hard, 3 good, 4 easy
Progress  = Literal["Beginner", "Adept", "Seasoned", "Expert", "Master"]  # Bunpro's own SRS bucket


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
    error_note: str | None = None   # one plain sentence, or null
```

`Item` is the on-disk truth. `QueueEntry` is the derived view Claude sees. They are separate types on purpose: `QueueEntry` carries no `stability` or `difficulty`, because those are implementation detail and putting them in front of the model just invites it to reason about numbers it should be ignoring.

---

## 5. Scoring contract

Pure functions in `scoring.py`. Every one of these takes the date as an argument and reads no clock.

### Retrievability

How likely you are to recall the item today, from 0 to 1.

```python
def retrievability(stability: float, elapsed_days: float) -> float:
    F, D = 19 / 81, -0.5
    return (1 + F * elapsed_days / stability) ** D
```

An item never reviewed (`last_review is None`) uses `elapsed_days = days since first_seen` and the default stability of 1.0, which puts it near the top of the queue within a day or two of being added. That is intended: you meet a word, and it comes back at you almost immediately.

### Priority

```python
def priority(entry, today) -> float:
    base = 1.0 - retrievability(...)

    w_new   = 1.3 if entry.days_since_first_seen < 14 else 1.0
    w_leech = 1.0 + 0.1 * min(entry.lapses, 5)
    w_cool  = 0.5 if (entry.days_since_review or 99) < 2 else 1.0

    return base * w_new * w_leech * w_cool
```

Three deliberate distortions on top of pure forgetting. `w_new` pushes recently-met items up, because consolidation in the first fortnight is worth more than a marginal review of something old. `w_leech` floats your persistent failures up. `w_cool` pushes down anything you reviewed in the last two days, and it matters more than it looks — without it the queue collapses onto the same five leeches forever and the conversational reviews get tedious.

Suspended items are excluded before scoring, not down-weighted.

### Grade to new state

```python
def apply_grade(state: MemoryState, grade: GradeVal, today: date) -> MemoryState:
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
        last_error=...,
    )
```

**This is not FSRS and does not pretend to be.** It is a transparent stand-in with the same shape, chosen because you can read it and predict what it does. The only thing that matters at this stage is whether the resulting *ordering* looks sane when you eyeball a hundred items. Tune the constants once you have real data; swap in real FSRS later if you care, and nothing else in the system has to change, because this file has no dependencies.

---

## 6. Tool contract

Five tools in `server.py`. The docstrings shown here are not documentation — they are the text Claude reads to decide whether to call the tool, so they are part of the interface and vague wording here produces a tool that gets ignored or misused.

### `get_review_queue`

```python
def get_review_queue(
    limit: int = 20,
    kind: Literal["grammar", "vocab", "both"] = "both",
) -> QueueResponse
```

> Get the Japanese items most in need of review right now, ordered by priority. Call this at the start of a review session. Returns grammar points and vocabulary with a freshness score for each, plus a note on how the learner last got it wrong.

Returns:

```python
class QueueResponse(BaseModel):
    generated_at: date
    total_items: int          # everything in the vault, not just returned
    returned: int
    grammar_focus: list[str]  # top 2–3 grammar ids, for the deep dive
    queue: list[QueueEntry]
```

`grammar_focus` exists so Claude does not have to decide what to concentrate on. Two or three grammar points is a session; twenty is a slog.

Errors: none. An empty vault returns an empty queue, not an error.

### `get_item`

```python
def get_item(item_id: str) -> ItemDetail
```

> Get everything known about one Japanese grammar point or word, including the learner's own notes from their vault.

`ItemDetail` is a `QueueEntry` plus the prose body of the note. This is how Claude sees your mnemonics and mined sentences.

Errors: `ItemNotFound` if the id does not exist. The message must include the id and suggest calling `get_review_queue` — an error a model can act on is worth ten that just say "not found".

### `submit_grades`

```python
def submit_grades(grades: list[Grade]) -> GradeReport
```

> Record how the learner performed on items during a review. Call this at the END of a review session, once, with every item you observed. Grade 1 = could not recall or used it wrong, 2 = struggled, 3 = correct, 4 = effortless. Include a one-sentence error_note when they got it wrong, describing the specific mistake.

**This is the tool that closes the loop.** Without it, memory state never updates, the queue never changes, and the whole thing is an expensive way to print a list. Its description has to be forceful about being called at the end of the session or the model will forget.

Behaviour: for each grade, load the item, apply `apply_grade`, write the new memory state back. Unknown ids are collected and returned rather than raising, because one bad id should not discard nineteen good ones.

```python
class GradeReport(BaseModel):
    updated: int
    unknown_ids: list[str]
    summary: list[str]     # e.g. "面倒くさい: next review in ~3 days"
```

Note what is *not* an argument here: the date. The server stamps `last_review` with its own today. Never let the model supply the date.

### `add_item`

```python
def add_item(
    surface: str,
    kind: Kind,
    reading: str | None = None,
    meaning: str | None = None,
    level: Level | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    progress: Progress | None = None,
) -> AddReport
```

> Add a Japanese grammar point or word the learner has just encountered. Fill in the reading, meaning, and JLPT level yourself from your own knowledge of Japanese — do not ask the learner for them unless the word is genuinely ambiguous. Put any context the learner gave you (where they met it, what confused them) into `note`. If the learner says they already partly know this word (e.g. "I'm Adept on this" or quotes a Bunpro SRS stage), pass that bucket as `progress` — one of Beginner, Adept, Seasoned, Expert, Master — so the review schedule starts from their actual familiarity instead of treating it as brand new. Leave `progress` unset for something they are meeting for the first time.

That instruction to Claude is doing real work. Every field but `surface` and `kind` is optional precisely so the model does the clerical filling-in. You say "add 面倒くさい" and a complete note appears. You are not typing metadata into a form; that is the entire ergonomic argument for building this as an MCP tool rather than an Obsidian plugin.

Behaviour: sets `source: manual`, `first_seen: today`. `note` is written **below** the metadata block, under a `## Context` heading, never into a metadata field.

Memory state: if `progress` is unset (the common case — a word genuinely met for the first time), the item gets the default `MemoryState()` — stability 1.0, no `last_review`. If `progress` is given, it seeds `stability` through the exact same `BUCKET_SEED` table the CSV importer uses (§7) and stamps `last_review` to today, so a manually-added "Adept" word and an imported "Adept" word start on the identical schedule. Either way `difficulty` is left at its neutral default (5.0) — never seeded, only ever moved by real review grades.

If the id already exists, do not overwrite and do not error. Return `already_exists: true` with the existing item, and let Claude tell you.

### `import_export`

```python
def import_export(csv_path: str, dry_run: bool = True) -> ImportReport
```

> Import a Bunpro CSV export into the vault. ALWAYS run with dry_run=true first and show the learner the report before running for real.

```python
class ImportReport(BaseModel):
    dry_run: bool
    rows_read: int
    would_create: int      # or created
    would_update: int
    skipped: list[str]     # with reasons
    errors: list[str]
```

The `dry_run` default of `true` is not politeness. The first time you point this at a real export it will either create nine hundred notes or make a mess of your vault, and you want to read the summary before it commits.

Behaviour on update: touch **only** the import-owned fields from section 3. Never `stability`, never `difficulty`, never `suspended`, never the prose.

---

## 7. CSV ingest contract

We do not control this format and it will change without warning. Everything that knows about Bunpro's column names lives in `ingest.py` and nowhere else, so when the export breaks there is exactly one file to fix. This section reflects the *actual* export produced by the zyaga Bunpro Exporter userscript, not the richer shape originally assumed — see `IMPORT_FIX.md` for the full history if you're wondering why this looks different from an earlier version of this file.

**The export is vocab-only.** The userscript hardcodes `TYPE = "Vocab"`; grammar never appears in it. Every row the importer produces is `kind="vocab"`. If a grammar CSV export ever exists, it will need a different parser — do not try to make one function read both shapes.

Expected columns, in the shape the importer wants after normalising:

| CSV column    | Maps to      | Required | If missing |
|---------------|--------------|----------|------------|
| `word`        | `surface`    | yes      | Row skipped, logged in `skipped`. |
| `reading`     | `reading`    | no       | Falls back to repeating `surface` as the reading, logged in `errors` (not fatal — see below). |
| `description` | `meaning`    | no       | Left null. |
| `progress`    | *(seeding)*  | no       | Blank or unrecognised bucket falls back to `DEFAULT_SEED`. Not an error. |

Fields with no source column in this export — `level`, `bunpro_srs`, `bunpro_url` — are always left null on import. `first_seen` has no source column either; on creation it is set to today, exactly as the "falls back to today" rule always intended, just with no CSV value to prefer over it.

### Seeding memory state from `progress`

`progress` is one of `Beginner`, `Adept`, `Seasoned`, `Expert`, `Master` — Bunpro's own SRS bucket for that word, not a numeric stage. **On creation only**, it seeds `stability` and `last_review` so a word the learner already knows cold doesn't look identical to one met this morning:

```python
BUCKET_SEED = {
    # bucket:      stability_days
    "Beginner":  2.0,
    "Adept":     7.0,
    "Seasoned":  21.0,
    "Expert":    60.0,
    "Master":    150.0,
}
DEFAULT_SEED = 1.0   # unknown or blank bucket
```

**`difficulty` is never seeded, from any bucket, at any time.** It always starts at its neutral default (5.0) and moves only from the learner's own review grades (§5). Pre-judging how hard a word is before it has ever been reviewed is guesswork the system shouldn't bake in — a "Master" word that turns out to be a leech should still be free to reveal that through real grades, not start out looking easy.

`last_review` is set to the import date at the same time as `stability` — seeding stability without also starting the forgetting clock from today would leave `elapsed_days` computed from `first_seen` instead and skew the ordering. These numbers are a starting guess, same status as the rest of `scoring.py`: eyeball the resulting queue after a real import and adjust the table, not the rest of the system, if Master-bucket words are still flooding the top.

**This seeding never happens on update.** Re-importing the same CSV after the learner has done real reviews must not reset their memory state back to a bucket guess — that would silently erase review history. See the field-ownership table in section 3.

**This same table is shared with `add_item`'s `progress` parameter (§6).** A manually-added word stated at a bucket and an imported word from the same bucket must start on the identical schedule — one `BUCKET_SEED`, two callers, so `ingest.py` is still the only file that knows Bunpro's bucket names.

### The reading fallback

The exporter is expected to include a `reading` column (see `IMPORT_FIX.md` Change 2), but if that column is missing entirely, or a specific row's cell is blank, the importer repeats `surface` as the reading rather than skipping the row. This is a last resort — a real reading is always better, because it participates in the vocab `id` — so every row that falls back to it is logged in `errors` (e.g. `"no reading available, used surface as reading: 面倒くさい"`), not silently accepted.

Rules:

- **Unknown columns are ignored silently.** Bunpro adding a column must not break the import.
- **A missing expected column is a warning, not a crash.** Import what you can and list the rest in `errors`.
- **`first_seen` is only ever set on creation.** If the row already exists, its original `first_seen` stands, even if the CSV disagrees. Bunpro's idea of when you met a word is less trustworthy than the vault's, and rewriting it would corrupt every freshness calculation downstream.
- The importer never sets memory state on an existing item, under any circumstance — including the bucket seeding above, which is creation-only by definition.

If the real export changes shape again, change this table and the mapping function. Nothing else in the project should need to know.

---

## 8. Errors

MCP surfaces a raised exception to Claude as a tool error, and Claude will read the message and often recover on its own. So the messages are for a reader, not a log file.

| Condition | Behaviour |
|---|---|
| Vault path not set or not a folder | Raise at startup, not at first call. Fail loudly and early. |
| Note has malformed metadata | Skip that note, log to stderr, continue. One broken file must not take down the queue. |
| Unknown `item_id` in `get_item` | Raise, with the id in the message. |
| Unknown `item_id` in `submit_grades` | Collect, return in `unknown_ids`, process the rest. |
| CSV file not found | Raise, with the path in the message. |
| Two notes claim the same `id` | Raise at load. This is corruption and silence would make it worse. |

**Logging goes to stderr. Never to stdout.** Claude Desktop reads your program's standard output as protocol messages, so a stray `print()` injects garbage into the conversation and the server appears broken in a way that makes no sense at all. This will happen to you once.

---

## 9. Open questions

Things deliberately not decided yet.

- **Avoidance.** If a conversational review gives you an opening for ば and you reach for たら instead, that is a signal — arguably a lapse. Nothing in the current `Grade` model can express it. Worth a fifth grade value, or a separate flag, once we see whether Claude reliably notices.
- **Retiring items.** `suspended` is a blunt instrument. Something you have genuinely mastered should probably leave the queue on its own rather than needing a flag.
- **`BUCKET_SEED`'s constants are a guess.** Section 7 seeds `stability`/`difficulty` from Bunpro's SRS bucket on import, chosen to make the first real queue *feel* right rather than derived from anything principled. Revisit after a real import once there's a queue to eyeball.

Resolved: **Bunpro's SRS stage**, previously an open question about whether to use `bunpro_srs` to seed `stability` on first import — now moot, since the real export (§7) never populates `bunpro_srs` at all. It carries a `progress` bucket label instead, which section 7's `BUCKET_SEED` now uses for exactly that seeding.
