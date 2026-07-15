# bunpro-mcp

A local MCP server that manages a Japanese-learning review queue backed by an Obsidian vault. It runs as a child process of Claude Desktop, speaking MCP over stdio — no database, no network service, no port.

For contributors: see [.agent/ARCHITECTURE.md](.agent/ARCHITECTURE.md) for the build rationale and [.agent/CONTRACT.md](.agent/CONTRACT.md) for the data shapes and tool behaviour.

## What you can do with it

Once wired into Claude Desktop, ask Claude in plain language — it picks the right tool:

- **Review what you're forgetting** — "quiz me on what's due" pulls the items most in need of review and updates your schedule based on how you do.
- **Practice words you've already mastered** — "test me on food words I already know" gathers solid items around a theme, proposes a short practice conversation, runs it, then records how it went.
- **Add something new** — "I just learned 〜てしまう" files it, filling in the reading, meaning, and JLPT level for you.
- **Import your Bunpro export** — hand Claude a CSV export and it bulk-loads it into the vault.

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

## Manual verification (before wiring into Claude Desktop)

Start the server under the MCP Inspector — a local UI for calling tools by hand with no model involved:

```bash
mkdir -p /tmp/test-vault
VAULT_PATH=/tmp/test-vault uv run mcp dev src/bunpro_mcp/server.py
```

In the Inspector: call `import_export` with `csv_path` pointing at `fixtures/sample_bunpro.csv` (dry run first, then for real), then `get_review_queue`, `get_item`, and `submit_grades`, and confirm the queue ordering changes after grading.

To try `get_practice_pool` (the mastered-words practice mode), first give it some well-known items — import the sample telling it you already know those words well, or grade a few items `4` a couple of times. Then call `get_practice_pool` and confirm it returns those mastered items, strongest-first, with the weaker ones absent.

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

**After editing the config, fully quit and relaunch Claude Desktop** (closing the window is not enough). The six tools — `get_review_queue`, `get_practice_pool`, `get_item`, `submit_grades`, `add_item`, `import_export` — then appear under the connectors/tools menu.

If the server doesn't show up, check Claude Desktop's MCP logs at `~/.config/Claude/logs/`. Usual culprits: a wrong absolute path, `VAULT_PATH` pointing nowhere, or a startup exception.

## Notes

- **Nothing is ever deleted.** Items you're done with are suspended, never removed from the vault.
- **Remote access (web/mobile) is deliberately out of scope for this pass** — see Appendix A of [.agent/ARCHITECTURE.md](.agent/ARCHITECTURE.md) if that's ever needed.
