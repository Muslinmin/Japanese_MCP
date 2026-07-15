# Bunpro MCP server — architecture (current state)

**Status: built and running.** This is a reference for an agent picking up work on this codebase, not a build guide. `CONTRACT.md` is the source of truth for data shapes and tool behaviour — when this file and that file disagree, `CONTRACT.md` wins.

---

## 1. What this is

A single Python program launched as a child process by Claude Desktop, speaking MCP over stdio. Five tools let Claude read a review queue, inspect an item, record review grades, add new items, and bulk-import a Bunpro CSV export. All state lives in plain `.md` files in an Obsidian vault folder — no database, no network service, no port.

It does not run continuously and does not bind a socket. Claude Desktop starts it, talks to it over stdio, kills it on exit.

**Never write to stdout.** stdout is the MCP protocol channel; a stray `print()` corrupts it and the server appears broken for reasons that make no sense. All logging goes to stderr — `server.py` sets this up before anything else. Grep for `print(` before touching anything if the server misbehaves.

---

## 2. Running it

- Interpreter/deps pinned via `uv` (`uv sync`, `uv run pytest`, `uv run mcp dev src/bunpro_mcp/server.py`). Never rely on system Python or an ambient venv — Claude Desktop launches the server from an unknown working directory.
- Config lives at `~/.config/Claude/claude_desktop_config.json`, under an `mcpServers.bunpro` key, with absolute paths for `command`, `--directory`, and `VAULT_PATH`. **That file also holds unrelated Claude Desktop app preferences (cowork settings, etc.) — never overwrite it wholesale, only add/edit the `mcpServers.bunpro` key.** After any change, Claude Desktop must be fully quit and relaunched, not just closed.
- Current real vault: `~/Obsidian/Japanese` (`Grammar/` and `Vocab/` subfolders), already populated from a real Bunpro export.
- Debug a dead connector via `~/.config/Claude/logs/`.

---

## 3. Dependencies

`mcp[cli]`, `pydantic` v2, `python-frontmatter`, `pyyaml`, plus `pytest` in the dev group. Deliberately not used: `pandas` (stdlib `csv` is enough and pandas is a slow import for a stdio server), any database driver, any HTTP client (the CSV arrives out of band via a browser userscript).

---

## 4. Layout and dependency direction

```
bunpro-mcp/
├── pyproject.toml
├── .agent/{ARCHITECTURE,CONTRACT,README}.md
├── src/bunpro_mcp/
│   ├── models.py    # data shapes; imports nothing local
│   ├── scoring.py   # pure maths; imports models only
│   ├── vault.py     # the ONLY module that touches the filesystem
│   ├── ingest.py    # CSV + single-item ingest; imports vault, models
│   └── server.py    # MCP tools; imports everything below; entry point
├── tests/{test_scoring,test_vault,test_ingest}.py
└── fixtures/sample_bunpro.csv
```

Dependencies point one direction only, down this list. `models` imports nothing local; `scoring` and `vault` import `models`; `ingest` imports `vault` and `models`; `server` imports all of them. An import pointing upward is a design error — fix the design.

This is why `scoring` tests with no disk, `vault` tests against a temp folder with no server, and `server` — the one file that's awkward to unit-test — holds almost no logic worth testing. Don't add logic to `server.py`; if you're writing an `if` about Japanese or memory decay there, it belongs in a lower module.

---

## 5. Module responsibilities

### `models.py`
Pure Pydantic data, no behaviour. `Kind`, `Source`, `Level`, `GradeVal`, `Progress` literals; `MemoryState`, `Item`, `QueueEntry`, `Grade`, plus the tool-response models (`QueueResponse`, `ItemDetail`, `GradeReport`, `AddReport`, `ImportReport`). `Item` (on-disk truth, carries `stability`/`difficulty`) and `QueueEntry` (derived view, does not) are deliberately separate — see `CONTRACT.md` §4.

### `scoring.py`
Pure functions, no filesystem or clock access — every date is an argument. `retrievability`, `priority`, `apply_grade`, `clamp`, and the assembler `to_queue_entry` (turns a stored `Item` into a scored `QueueEntry`; nothing it computes is persisted). Full formulas in `CONTRACT.md` §5.

**Not real FSRS** — a transparent stand-in with the right shape, kept because it's eyeball-checkable and has zero local dependencies, so swapping in real FSRS later touches nothing else. Do not swap it in without being asked.

### `vault.py`
The only module allowed to touch the filesystem. Builds an id→path index from note metadata at construction (trusts metadata, not filenames — renaming a file in Obsidian doesn't break anything). Key methods: `load_all`, `get`, `get_detail`, `upsert` (create-or-update, appends body under a heading rather than clobbering prose), `write_memory` (touches only memory-owned fields), `path_for`. Writes are atomic (temp file + `os.replace`). Full invariants in `CONTRACT.md` §1–3.

### `ingest.py`
All Bunpro-format knowledge lives here and nowhere else. The CSV importer is **vocab-only** — the real export (zyaga Bunpro Exporter userscript) has just `word`, `reading` (usually absent), `description`, `progress`. `BUCKET_SEED` maps a Bunpro `progress` bucket (Beginner…Master) to a starting `stability` — creation-only, never `difficulty`, shared between the CSV path (`parse_bunpro_csv`/`import_export`) and the conversational path (`make_item`, via `add_item`'s `progress` argument) through one helper, `_seed_memory_state`. Full contract in `CONTRACT.md` §7.

### `server.py`
Declares the five `@mcp.tool()` functions, reads `VAULT_PATH` from env and constructs `Vault` once at import time (fails loudly if unset/invalid, not on first call), stamps `today = date.today()` itself for grades/adds/imports (never accepted as a tool argument). Docstrings are copied verbatim from `CONTRACT.md` §6 — they're the interface Claude reads to decide when/how to call each tool, not decoration.

---

## 6. Manual verification

```bash
uv run pytest                                              # unit tests
VAULT_PATH=/tmp/test-vault uv run mcp dev src/bunpro_mcp/server.py   # Inspector, no model in the loop
```

For an end-to-end check without the Inspector, call tools directly against `server.mcp` (see git history for a working example script) — confirms zero stdout output and correct tool wiring before ever touching Claude Desktop.

---

## 7. Known deviations / open items

- **Grammar ids aren't true romaji.** No romanization library is in the dependency list (by design — keep deps short), so `add_item`'s grammar-slug fallback just slugifies the raw surface (`てしまう` → `grammar-てしまう`, not `grammar-te-shimau`). Fine for uniqueness/lookup, just not pretty. CSV-imported grammar would get real slugs from a Bunpro URL, but the importer is vocab-only right now so this path is currently manual-add only.
- **`BUCKET_SEED` constants are a guess**, same status as the rest of `scoring.py` — tune after eyeballing a real queue, not before.
- See `CONTRACT.md` §9 for data-model open questions (avoidance detection, item retirement).

---

## Appendix A — going remote (deferred, not built)

Out of scope for the current build: local/stdio/Claude-Desktop-only. Needed only to reach the web app or mobile app, since both are cloud clients requiring a publicly reachable server (plain Tailscale doesn't help; Tailscale Funnel or a VPS would). If this becomes a real ask: `models.py`/`scoring.py`/`vault.py`/`ingest.py` don't change, `server.py` swaps `mcp.run()` for `mcp.run(transport="streamable-http", host="127.0.0.1", port=8080)` behind a tunnel, and a shared-secret header check becomes non-negotiable once public. Full detail was previously spelled out here; re-derive it from the MCP SDK docs at build time rather than trusting a stale snippet, since the HTTP transport API moves between SDK releases.
