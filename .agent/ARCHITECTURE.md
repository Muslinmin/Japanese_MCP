# Bunpro MCP server — architecture and build guide

**Audience: the coding agent building this.** You are implementing a local MCP (Model Context Protocol) server that manages a Japanese-learning review queue backed by an Obsidian vault. This document tells you what to build, in what order, and the Ubuntu-specific details you need. Read it fully before writing code. The companion file `CONTRACT.md` is the source of truth for data shapes and tool behaviour — when this file and that file disagree, `CONTRACT.md` wins.

---

## 1. What this program is

A single Python program launched as a child process by Claude Desktop. It speaks MCP over stdio (standard input/output). It exposes five tools that let Claude read a review queue, inspect an item, record review grades, add new items, and bulk-import a Bunpro CSV export. All state lives in plain `.md` files in an Obsidian vault folder. There is no database, no network service, no account, and no port to listen on.

Read that again if it surprises you: **the server does not run continuously and does not bind a socket.** Claude Desktop starts it, talks to it over stdio, and kills it on exit. "Local MCP server" means exactly this.

### The rule that catches everyone

**Never write to stdout.** stdout is the protocol channel — Claude Desktop parses it as MCP messages. A stray `print()` corrupts the stream and the server appears broken for reasons that make no sense. All logging goes to **stderr** or a file. Set this up first, before anything else, so a debug print can't ruin your afternoon.

---

## 2. Environment (Ubuntu)

Assume a clean-ish Ubuntu 22.04 or 24.04 machine. Do not assume the right Python is present or that the user wants packages installed globally.

### Python

Target **Python 3.11+**. Ubuntu 24.04 ships 3.12, which is fine. Ubuntu 22.04 ships 3.10, which is **too old** for some of the typing used here — check with `python3 --version` and if it's below 3.11, do not fight the system Python. Use `uv` (below) to fetch a suitable interpreter.

### Use `uv`, not pip-into-system-python

`uv` is a fast Python package and environment manager. It solves the exact problem this project has on Ubuntu: Claude Desktop launches the server from an unknown working directory, so the server must run against a pinned interpreter and a pinned set of packages with zero reliance on the user's shell, `PATH`, or an activated virtualenv.

Install it (rootless, no sudo):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

It lands at `~/.local/bin/uv`. Confirm `~/.local/bin` is on `PATH` for interactive use, but **do not rely on `PATH` in the Claude Desktop config** — use the absolute path `~/.local/bin/uv` there (expanded to `/home/<user>/.local/bin/uv`).

Do **not** use `pip install --user` or a manually-activated venv as the mechanism the server depends on. It will work when you test it in a terminal and fail when Claude Desktop launches it, and you will lose an hour to that. `uv run` is what makes launch reproducible.

### Claude Desktop on Linux

Confirm with the user that they have Claude Desktop installed and that it supports local MCP servers on their build. Local servers configured via `claude_desktop_config.json` are Claude-Desktop-only — they are **not** available in the web app or the mobile apps. If the user is on Linux and Claude Desktop's local-MCP support is unavailable or flaky on their distro, flag it early rather than debugging a server that's fine.

The config file on Linux lives at:

```
~/.config/Claude/claude_desktop_config.json
```

Create it if absent. After any change to it, Claude Desktop must be **fully quit and relaunched** — closing the window is not enough. Tell the user this explicitly; they will forget.

---

## 3. Dependencies

Keep this list short. Every addition is a thing that can break on launch.

| Package              | Why | Notes |
|----------------------|-----|-------|
| `mcp[cli]`           | The MCP SDK; provides `FastMCP`. | Pin a known-good version. |
| `pydantic` (v2)      | Data models and argument validation. | Pulled in by `mcp` anyway; declare it explicitly. |
| `python-frontmatter` | Reads/writes the metadata block + prose body of a note without mangling either. | The whole storage layer leans on this. |
| `pyyaml`             | Backs the metadata block. | Transitive via `python-frontmatter`; declare it so behaviour is pinned. |

**Not** used, on purpose: no `pandas` (the stdlib `csv` module reads the export fine and pandas is a heavy, slow import for a stdio server that must start fast), no database driver, no HTTP client (the server never makes network calls — the Bunpro CSV arrives via the browser userscript, out of band).

`pyproject.toml` skeleton:

```toml
[project]
name = "bunpro-mcp"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "mcp[cli]>=1.2",
    "pydantic>=2.6",
    "python-frontmatter>=1.1",
    "pyyaml>=6.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Install and lock:

```bash
cd bunpro-mcp
uv sync
```

---

## 4. Layout and dependency direction

```
bunpro-mcp/
├── pyproject.toml
├── README.md            # human setup notes (config snippet, how to run tests)
├── src/
│   └── bunpro_mcp/
│       ├── __init__.py
│       ├── models.py    # data shapes; imports nothing local
│       ├── scoring.py   # pure maths; imports models only
│       ├── vault.py     # the ONLY file that touches the filesystem
│       ├── ingest.py    # CSV + single-item ingest; imports vault, models
│       └── server.py    # MCP tools; imports everything below; the entry point
└── tests/
    ├── test_scoring.py
    ├── test_vault.py
    └── test_ingest.py
```

**Dependencies point one direction only — down this list, never up.** `models` imports nothing local. `scoring` imports `models`. `vault` imports `models`. `ingest` imports `vault` and `models`. `server` imports all of them. If you ever write an import that points upward, you have a design error; fix the design, don't add a workaround.

Why it matters: it makes the two hard-to-test files trivial to test and the one genuinely-hard-to-test file nearly logic-free. `scoring` tests with no disk. `vault` tests against a temp folder with no server. `server` — the piece that's awkward to test because exercising it means launching Claude Desktop — ends up holding almost no logic worth testing.

---

## 5. File-by-file spec

Signatures below are the contract. `CONTRACT.md` sections are cited for full behaviour; do not duplicate that logic here, implement against it.

### `models.py`

Pure data. Standard library + Pydantic only. No behaviour beyond validation. Implement exactly the types in `CONTRACT.md` §4: the `Literal` aliases (`Kind`, `Source`, `Level`, `GradeVal`), and the models `MemoryState`, `Item`, `QueueEntry`, `Grade`. Add the tool-response models that live at the boundary:

```python
class QueueResponse(BaseModel):
    generated_at: date
    total_items: int
    returned: int
    grammar_focus: list[str]
    queue: list[QueueEntry]

class ItemDetail(QueueEntry):
    body: str                      # the prose beneath the metadata block

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
```

Keep `Item` (on-disk truth, carries `stability`/`difficulty`) and `QueueEntry` (derived view, does NOT carry them) as separate types. This is deliberate — see `CONTRACT.md` §4.

### `scoring.py`

Pure functions. **No filesystem access, no clock reads** — the date is always an argument. Implement `CONTRACT.md` §5 exactly:

```python
def retrievability(stability: float, elapsed_days: float) -> float: ...

def priority(entry: QueueEntry, today: date) -> float: ...

def apply_grade(state: MemoryState, grade: GradeVal, today: date,
                error_note: str | None) -> MemoryState: ...

def clamp(x: float, lo: float, hi: float) -> float: ...
```

Also provide the assembler that turns a stored `Item` into a scored `QueueEntry`, since it's pure and belongs here:

```python
def to_queue_entry(item: Item, today: date) -> QueueEntry: ...
```

This computes `retrievability` and `priority` and the `days_since_*` fields. Nothing persists these — they are recomputed every read (`CONTRACT.md` §1, invariant 5; §3 "fields that deliberately do not exist").

**This is not real FSRS.** It is a transparent stand-in with the right shape. Do not replace it with real FSRS now; the point at this stage is that the ordering is eyeball-checkable. Because this file has no local dependencies, swapping in real FSRS later touches nothing else.

### `vault.py`

**The only module allowed to touch the filesystem.** Everything else asks it for items and hands it items back. It knows the folder layout, the id↔path index, and note serialisation.

```python
class Vault:
    def __init__(self, root: Path) -> None: ...
        # Validate root exists and is a directory. Raise at construction,
        # not at first tool call (CONTRACT.md §8). Build the id->path index
        # by reading the metadata of every .md file — trust metadata `id`,
        # not the filename (CONTRACT.md §2).

    def load_all(self) -> list[Item]: ...
        # Skip notes with malformed metadata: log to stderr, continue.
        # Two notes with the same id -> raise (corruption; CONTRACT.md §8).

    def get(self, item_id: str) -> Item | None: ...

    def get_detail(self, item_id: str) -> tuple[Item, str] | None: ...
        # Returns (item, prose_body). None if id unknown.

    def upsert(self, item: Item, body: str | None = None) -> bool: ...
        # Returns True if created, False if updated.
        # CRITICAL: when updating, write only the fields the caller owns.
        # Never overwrite the prose body unless `body` is explicitly given,
        # and even then append under a heading — never clobber (CONTRACT.md §3,
        # invariant 4).

    def write_memory(self, item_id: str, state: MemoryState) -> None: ...
        # Update ONLY the memory-owned fields in the metadata block. Touch
        # nothing else. This is what submit_grades calls.

    def path_for(self, item: Item) -> Path: ...
        # Derive filename from id/surface. On collision, append reading.
        # Do not strip Japanese characters from filenames — Ubuntu's
        # filesystem handles UTF-8 fine.
```

Use `python-frontmatter` for read/write. When writing back, load the existing note first, mutate only the owned keys in its metadata dict, and write it out — this is how you guarantee you never touch fields or prose you don't own. Do not serialise an `Item` straight to a fresh file on update; that would erase everything the model doesn't know about.

Write atomically: write to a temp file in the same directory, then `os.replace()` onto the target. A crash mid-write must not leave a half-written note. `os.replace` is atomic on Linux within one filesystem.

### `ingest.py`

Turns messy outside data into clean `Item`s, then hands them to `vault`. All Bunpro-format knowledge lives here and nowhere else, so when the export format changes there is exactly one file to fix (`CONTRACT.md` §7).

The real export (zyaga Bunpro Exporter userscript) is vocab-only with columns `word`, `reading`, `description`, `progress` — not the richer shape originally assumed here. See `IMPORT_FIX.md` for the full story if the signatures below look narrower than you expected.

```python
BUCKET_SEED: dict[str, float] = { ... }   # bucket -> stability_days, CONTRACT.md §7
DEFAULT_SEED: float = 1.0                 # unknown or blank bucket

def _seed_memory_state(progress: str | None, today: date) -> MemoryState: ...
    # Shared by parse_bunpro_csv and make_item so an imported word and a
    # manually-added word stated at the same bucket land on the identical
    # schedule. Seeds stability + last_review only — NEVER difficulty,
    # which stays at its neutral default no matter the bucket (CONTRACT.md
    # §7). Creation-only; neither caller invokes this on an update.

def parse_bunpro_csv(csv_path: Path, today: date | None = None,
                     ) -> tuple[list[Item], list[str], list[str]]: ...
    # Returns (items, skipped, errors). Uses stdlib csv. Every row becomes
    # kind="vocab" — the export carries no grammar. Maps columns per
    # CONTRACT.md §7: word->surface (required), reading->reading (falls
    # back to surface if the column or cell is blank, logged in `errors`),
    # description->meaning, progress->_seed_memory_state(). Unknown columns
    # ignored silently; missing expected column is a warning in `errors`,
    # not a crash. Rows missing `word` go in `skipped` with a reason.
    # `today` is optional so tests can pin it; production code leaves it
    # unset and gets date.today().

def import_export(vault: Vault, csv_path: Path, today: date,
                  dry_run: bool) -> ImportReport: ...
    # dry_run=True: compute would_create / would_update, write nothing.
    # On creation: writes the parsed Item as-is, memory state already
    # seeded from its Bunpro SRS bucket by parse_bunpro_csv.
    # On update: touch only import-owned fields (reading, meaning). NEVER
    # memory state, never `first_seen` on an existing item, never prose
    # (CONTRACT.md §7 rules) — a re-import must not erase real review
    # history. first_seen on creation is always today; the export has no
    # created_at column to prefer over it.

def make_item(surface: str, kind: Kind, today: date,
              reading: str | None, meaning: str | None,
              level: Level | None, progress: Progress | None = None) -> Item: ...
    # The single-item constructor. source="manual", first_seen=today.
    # Shared by add_item so the two ingest paths can't drift apart.
    # `progress` unset (the common case): default MemoryState — genuinely
    # new, no last_review. `progress` given: seeds via the same
    # _seed_memory_state() the CSV path uses, so a manual add and an
    # import from the same bucket agree.
```

`make_item` is deliberately shared between the bulk path and the conversational `add_item` path. Do not duplicate item-construction logic in `server.py`.

### `server.py`

The front door. Should be the dumbest file in the project: declare tools, write the descriptions Claude reads, validate, hand off. **If you find yourself writing an `if` about Japanese or about memory decay in here, it belongs in a lower module.**

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("bunpro")
# Read VAULT_PATH from env at startup; construct Vault once; fail loudly
# if unset or not a directory (CONTRACT.md §8).

@mcp.tool()
def get_review_queue(limit: int = 20,
                     kind: Literal["grammar", "vocab", "both"] = "both") -> QueueResponse: ...

@mcp.tool()
def get_item(item_id: str) -> ItemDetail: ...

@mcp.tool()
def submit_grades(grades: list[Grade]) -> GradeReport: ...

@mcp.tool()
def add_item(surface: str, kind: Kind, reading: str | None = None,
             meaning: str | None = None, level: Level | None = None,
             note: str | None = None, tags: list[str] | None = None,
             progress: Progress | None = None) -> AddReport: ...

@mcp.tool()
def import_export(csv_path: str, dry_run: bool = True) -> ImportReport: ...

if __name__ == "__main__":
    mcp.run()   # stdio transport by default
```

**The docstrings are not documentation — they are the interface.** Claude reads them to decide when and how to call each tool. Copy the docstring text from `CONTRACT.md` §6 verbatim; it is worded carefully. In particular `submit_grades` must be forceful about being called once, at the end of a session, with every observed item — or the model forgets and the loop never closes. And `add_item` must instruct Claude to fill in reading/meaning/level from its own Japanese knowledge rather than asking the user, because that is the entire ergonomic point.

The server stamps dates itself (`today = date.today()`) for grades, adds, and imports. **Never accept the date as a tool argument** (`CONTRACT.md` §6, `submit_grades`).

---

## 6. Build order

Build and test bottom-up. Do not write `server.py` until the layers under it pass tests.

1. **`models.py`** — types compile, validation works. Trivial.
2. **`scoring.py` + `test_scoring.py`** — feed it hand-built states and dates, assert retrievability falls with elapsed time, `apply_grade(1)` shrinks stability and bumps lapses, ordering of a synthetic set looks sane. No disk involved, so this is fast and is where you build confidence in the maths.
3. **`vault.py` + `test_vault.py`** — use pytest's `tmp_path` for a throwaway vault. Assert round-trip (write an item, read it back, fields match), assert prose survives an update untouched, assert `write_memory` changes only memory fields, assert malformed notes are skipped not fatal, assert duplicate ids raise.
4. **`ingest.py` + `test_ingest.py`** — feed it a small fake CSV (create one in the test). Assert new rows create, existing rows update only owned fields, `first_seen` is not rewritten on update, missing-required rows are skipped with reasons, `dry_run` writes nothing.
5. **`server.py`** — wire the tools to the tested modules. Minimal logic. Then integration-test by hand (§7).

Ship a `fixtures/sample_bunpro.csv` so `import_export` can be exercised end-to-end before the user ever points it at real Bunpro data.

---

## 7. Manual verification on Ubuntu

Before touching Claude Desktop, prove the server starts and lists its tools over stdio:

```bash
cd bunpro-mcp
VAULT_PATH=/tmp/test-vault uv run mcp dev src/bunpro_mcp/server.py
```

`mcp dev` opens the MCP Inspector — a local UI to call each tool by hand with no model in the loop. Create `/tmp/test-vault` first (`mkdir`), import the fixture CSV, then call `get_review_queue` and confirm the ordering. This catches almost everything before Claude Desktop is involved.

Then wire it into Claude Desktop. Config at `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "bunpro": {
      "command": "/home/<user>/.local/bin/uv",
      "args": ["--directory", "/home/<user>/code/bunpro-mcp", "run", "src/bunpro_mcp/server.py"],
      "env": { "VAULT_PATH": "/home/<user>/Obsidian/Japanese" }
    }
  }
}
```

Replace `<user>` with the real username; use absolute paths throughout (`command`, `--directory`, `VAULT_PATH`) — the server is launched from an unknown working directory, so nothing relative is safe. Fully quit and relaunch Claude Desktop. The tools appear under the connectors/tools menu.

If the server fails to appear: check Claude Desktop's MCP logs (under `~/.config/Claude/logs/`), and the usual culprits are a wrong absolute path, a stray stdout write, or a startup exception because `VAULT_PATH` points nowhere.

---

## 8. Definition of done

- All four lower modules have passing tests; `scoring` and `vault` have real coverage of the invariants in `CONTRACT.md` §1.
- The server starts under `mcp dev` and every tool is callable from the Inspector.
- `import_export(dry_run=True)` on the fixture reports counts and writes nothing; `dry_run=False` creates the notes; a second run updates without duplicating and without touching memory state or prose.
- A full round trip works from Claude Desktop: import → `get_review_queue` → a review conversation → `submit_grades` → the next `get_review_queue` reflects the grades.
- No `print()` to stdout anywhere in the codebase. Grep for it before declaring done.

---

## 9. Things not to do

- Do not add a database. The vault is the store (`CONTRACT.md` §1).
- Do not let any module except `vault.py` open a file.
- Do not read the clock inside `scoring.py`.
- Do not store `retrievability` or `priority`.
- Do not rewrite `first_seen` on import.
- Do not let `import` write memory fields, or `grade` write Bunpro fields.
- Do not print to stdout.
- Do not swap the placeholder scoring for real FSRS in this pass — get the loop working first.

---

## Appendix A — going remote (deferred; do not build in this pass)

This pass is **local, stdio, Claude Desktop only**. This appendix exists so the future move to remote access is captured, not so you build it now. Skip it unless the user explicitly asks for remote.

### Why it's needed at all

Remote is the *only* way to reach two things the local build can't: the **web app** (claude.ai in a browser) and the **Claude mobile app**. Both are cloud clients — when they call a connector, the request originates from Anthropic's cloud, not from the user's device. This is true even on the desktop and mobile apps. Consequence: the server must be reachable from the **public internet**. Localhost and a private mesh are not enough, because Anthropic's cloud is not on the user's private network.

**Tailscale note:** the user has Tailscale, and their laptop and phone are on their tailnet. This does *not* serve the Claude mobile app — the app talks to Anthropic's cloud, which is not on the tailnet. Plain Tailscale (the private mesh) cannot help here. Only **Tailscale Funnel**, which publishes a tailnet service to the *public* internet over HTTPS, bridges the gap. The alternative is any always-on public host (a small VPS).

**The tradeoff to make explicit to the user before building this:** with Funnel, their Ubuntu machine must be **awake and running the server** whenever they want to use the connector, including from the phone. If the laptop sleeps, the connector is dead. A VPS avoids this but puts the vault on a cloud box they maintain.

### What changes in the code

Almost nothing, by design. `models.py`, `scoring.py`, `vault.py`, and `ingest.py` do **not** change. Only `server.py`'s transport and the deployment around it change.

1. **Transport.** FastMCP runs stdio by default; for remote it runs Streamable HTTP. Concretely, swap the run call:

   ```python
   # local (this pass):
   mcp.run()

   # remote:
   mcp.run(transport="streamable-http", host="127.0.0.1", port=8080)
   ```

   Bind to `127.0.0.1`, not `0.0.0.0` — the tunnel (or a reverse proxy) is what faces the internet, and the server itself should only ever accept connections from the local tunnel process. Confirm the exact transport argument against the installed `mcp` SDK version; the API around HTTP transport has moved between releases.

2. **Auth — non-negotiable once public.** A public URL to the vault needs a shared secret. The connector supports request-header auth: the user configures a header (e.g. `x-api-key: <secret>`) in the custom-connector dialog, and Claude sends it on every request. Implement a check in `server.py` that rejects requests whose header doesn't match a secret read from the environment (`MCP_SHARED_SECRET`), and fail closed if the env var is unset. Do not log the secret.

3. **Exposure — pick one:**
   - **Tailscale Funnel:** `tailscale funnel 8080` publishes the local server at a `*.ts.net` HTTPS URL. No router config, no Cloudflare. Laptop must stay awake.
   - **VPS:** run the server on an always-on host behind a reverse proxy with TLS; the vault lives there too.

4. **Register the connector.** In Claude (web or desktop): Settings → Connectors → add a custom connector, paste the public HTTPS URL, add the auth header under advanced/request-header settings. Free plan allows one custom connector; Pro/Max/Team/Enterprise allow more.

### What does NOT change

The five tools, their signatures, every docstring, the whole `CONTRACT.md`, the vault format, and the four lower modules. If building remote later means editing anything below `server.py`, something has gone wrong — stop and reconsider.

### Recommendation baked in

Build local first, prove the review loop feels good, and only then decide whether web/mobile access justifies either an always-awake laptop (Funnel) or a maintained host (VPS). Going remote is then a small, contained follow-up — this appendix plus a transport swap — not a rewrite.
