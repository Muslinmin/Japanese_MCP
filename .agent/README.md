# bunpro-mcp

A local MCP server that manages a Japanese-learning review queue backed by an Obsidian vault. It runs as a child process of Claude Desktop, speaking MCP over stdio — no database, no network service, no port.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the build rationale and [CONTRACT.md](CONTRACT.md) for the data shapes and tool behaviour.

## Setup

### 1. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

This installs to `~/.local/bin/uv`. Confirm `~/.local/bin` is on your `PATH` for interactive use — but note the Claude Desktop config below uses the absolute path instead, since Claude Desktop launches the server without your shell's `PATH`.

### 2. Install dependencies

```bash
cd bunpro-mcp
uv sync
```

This creates `.venv/` with a pinned Python 3.11+ interpreter and the exact locked dependencies (`mcp`, `pydantic`, `python-frontmatter`, `pyyaml`, plus `pytest` for the dev group).

### 3. Point it at a vault folder

The server needs a folder to store notes in — an existing Obsidian vault subfolder, or any empty directory to start fresh:

```bash
mkdir -p ~/Obsidian/Japanese
```

Suggested layout inside it (created automatically on first write):

```
Japanese/
├── Grammar/
└── Vocab/
```

## Running the tests

```bash
uv run pytest
```

41 tests cover `scoring.py` (pure maths, no disk) and `vault.py` / `ingest.py` (against temp vaults via `tmp_path`). `server.py` is intentionally thin and is checked by hand instead (see below).

## Manual verification (before wiring into Claude Desktop)

Start the server under the MCP Inspector — a local UI for calling tools by hand with no model involved:

```bash
mkdir -p /tmp/test-vault
VAULT_PATH=/tmp/test-vault uv run mcp dev src/bunpro_mcp/server.py
```

In the Inspector: call `import_export` with `csv_path` pointing at `fixtures/sample_bunpro.csv` (dry run first, then for real), then `get_review_queue`, `get_item`, and `submit_grades`, and confirm the queue ordering changes after grading.

## Wiring into Claude Desktop

Local MCP servers are configured via `claude_desktop_config.json`. On Linux:

```
~/.config/Claude/claude_desktop_config.json
```

Create it if it doesn't exist, and add (replacing `<user>` and the paths with your own):

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

Use absolute paths throughout — the server is launched from an unknown working directory, so nothing relative is safe.

**After editing the config, fully quit and relaunch Claude Desktop** (closing the window is not enough). The five tools — `get_review_queue`, `get_item`, `submit_grades`, `add_item`, `import_export` — then appear under the connectors/tools menu.

If the server doesn't show up, check Claude Desktop's MCP logs at `~/.config/Claude/logs/`. Usual culprits: a wrong absolute path, `VAULT_PATH` pointing nowhere, or a startup exception.

## Notes

- **This is not real FSRS.** The scoring in `scoring.py` is a transparent stand-in with the right shape, chosen so the review ordering is eyeball-checkable. Swapping in real FSRS later only touches that one file.
- **Nothing is ever deleted.** Retirement is the `suspended` flag, not file deletion.
- **Remote access (web/mobile) is deliberately out of scope for this pass** — see Appendix A of `ARCHITECTURE.md` if that's ever needed.
