# bunpro-mcp

An MCP server that manages a Japanese-learning review queue backed by a folder of Markdown notes. It runs on Cloud Run and connects to Claude on web, mobile, and desktop — so reviews work from a phone — while the notes stay plain `.md` files you can read in Obsidian.

For contributors: see [.agent/ARCHITECTURE.md](.agent/ARCHITECTURE.md) for the build rationale and [.agent/CONTRACT.md](.agent/CONTRACT.md) for the data shapes and tool behaviour.

## What you can do with it

Ask Claude in plain language — it picks the right tool:

- **Review what you're forgetting** — "quiz me on what's due" pulls the items most in need of review and updates your schedule based on how you do.
- **Practice words you've already mastered** — "test me on food words I already know" gathers solid items around a theme, proposes a short practice conversation, runs it, then records how it went.
- **Add something new** — "I just learned 〜てしまう" files it, filling in the reading, meaning, and JLPT level for you.
- **Import your Bunpro export** — paste in a CSV export and it bulk-loads it into the vault.

The six tools are `get_review_queue`, `get_practice_pool`, `get_item`, `submit_grades`, `add_item`, and `import_export`.

---

## Configuration

Four environment variables. Copy [.env.example](.env.example) to `.env` for local runs.

| Variable | Required | What it does |
|---|---|---|
| `VAULT_BUCKET` | one of these two | GCS bucket holding the notes. Wins if both are set. |
| `VAULT_PATH` | one of these two | Local folder of notes. The local-development option. |
| `MCP_AUTH_TOKEN` | yes | The one secret. See below. |
| `MCP_PUBLIC_URL` | in production | The externally reachable base URL. Defaults to `http://localhost:$PORT`. |
| `PORT` | no | Injected by Cloud Run. Defaults to `8080`. |
| `TZ` | no | Defaults to `Asia/Singapore`. |

**`MCP_AUTH_TOKEN` does three jobs:** it's the password on the OAuth login page, the key every issued token is signed with, and a bearer token accepted directly by clients that can set a header. Rotating it invalidates every token already issued — intended, but it means reconnecting afterwards.

**`TZ` is a correctness setting, not a display one.** The container runs UTC. Without it set to your timezone, an evening review lands on tomorrow's date and the whole review schedule drifts a day.

---

## Running it locally

### 1. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install dependencies

```bash
cd bunpro-mcp
uv sync
```

### 3. Run against a local folder

```bash
mkdir -p /tmp/test-vault
VAULT_PATH=/tmp/test-vault MCP_AUTH_TOKEN=dev uv run bunpro-mcp
```

Serves on `http://localhost:8080`. `Grammar/` and `Vocab/` subfolders are created on first write.

```bash
curl localhost:8080/health                                    # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/mcp   # 401 — auth works
```

### Tests

```bash
uv run pytest
```

### Poking at the tools by hand

The MCP Inspector talks to the tools directly, with no model in the loop:

```bash
VAULT_PATH=/tmp/test-vault uv run mcp dev src/bunpro_mcp/server.py
```

Call `import_export` with the contents of `fixtures/sample_bunpro.csv` (dry run first, then for real), then `get_review_queue`, `get_item`, and `submit_grades`, and confirm the queue ordering changes after grading.

To try `get_practice_pool`, first give it some well-known items — import the sample telling it you already know those words, or grade a few items `4` a couple of times. Then confirm it returns those mastered items, strongest-first, with weaker ones absent.

---

## Deploying to Cloud Run

### The scripted path

```bash
./deploy.sh --seed ~/Obsidian/Japanese --dry-run-seed   # check what would upload
./deploy.sh --seed ~/Obsidian/Japanese                  # seed, then deploy
./deploy.sh                                             # redeploy later
```

It's idempotent — safe to re-run. It enables the required APIs, creates the bucket (with versioning) and the secret if they don't exist, optionally seeds the bucket from a local vault, deploys, sets `MCP_PUBLIC_URL` to the service URL, scopes the service account to that one bucket, and health-checks the result. An existing secret's value is left alone.

Config via env vars, defaults shown: `PROJECT` (current gcloud project), `REGION` (`asia-southeast1`), `VAULT_BUCKET` (`<project>-bunpro-vault`), `SERVICE` (`bunpro-mcp`), `SECRET` (`bunpro-mcp-token`), `TZ_VALUE` (`Asia/Singapore`).

### The manual equivalent

```bash
gsutil mb -l asia-southeast1 gs://$VAULT_BUCKET
gsutil versioning set on gs://$VAULT_BUCKET

# tr -d '\n' matters: openssl appends a newline, and a secret with a trailing
# newline can never be typed into the login form.
openssl rand -base64 32 | tr -d '\n' | gcloud secrets create bunpro-mcp-token --data-file=-

python scripts/seed_bucket.py ~/Obsidian/Japanese --bucket $VAULT_BUCKET

gcloud run deploy bunpro-mcp --source . --region asia-southeast1 \
  --allow-unauthenticated --max-instances=1 --memory 512Mi --timeout 300 \
  --set-env-vars VAULT_BUCKET=$VAULT_BUCKET,TZ=Asia/Singapore \
  --set-secrets MCP_AUTH_TOKEN=bunpro-mcp-token:latest

# The URL only exists after the first deploy, so this is a second step.
URL=$(gcloud run services describe bunpro-mcp --region asia-southeast1 --format='value(status.url)')
gcloud run services update bunpro-mcp --region asia-southeast1 \
  --update-env-vars "MCP_PUBLIC_URL=$URL"
```

Grant the runtime service account `roles/storage.objectAdmin` on that one bucket only, and `roles/secretmanager.secretAccessor` on the secret.

Two things that look wrong and aren't:

- **`--allow-unauthenticated`** — Claude can't mint Google IAM tokens, so IAM can't be the gate. OAuth is. Every route except `/health` and `/login` returns 401 without a valid token.
- **`--max-instances=1`** — the container is the bucket's only writer, and keeping it to one instance keeps write conflicts to the rare deploy-overlap case rather than the normal path.

Check it came up:

```bash
curl https://<your-service-url>/health     # {"status":"ok"}
```

---

## Connecting Claude

Get the token — it's the password you'll type on the login page:

```bash
gcloud secrets versions access latest --secret=bunpro-mcp-token
```

### Claude on web or mobile

Settings → Connectors → **Add custom connector**:

- **URL**: `https://<your-service-url>/mcp`
- Leave the Advanced OAuth Client ID and Client Secret fields **empty** — the server registers Claude automatically.

Claude opens a login page. Paste the token, click Approve, and the six tools appear in the tools menu.

The connector authenticates by OAuth 2.1, which is the only method this dialog supports — there's no field for a fixed header outside enterprise-managed connectors. The server implements the full flow (dynamic registration, PKCE, authorization code, refresh) and issues its own tokens, so there's no third-party identity provider involved.

### Claude Code, and other header-capable clients

The server also accepts the token directly as a bearer credential, so any client that lets you set a request header can skip the OAuth flow entirely:

```
Authorization: Bearer <token>
```

In Claude Code that's roughly `claude mcp add --transport http bunpro https://<your-service-url>/mcp --header "Authorization: Bearer <token>"` — check `claude mcp add --help` for the flags your version uses, as they have changed between releases.

Claude Desktop can also use the connector UI above, which is the same OAuth flow and needs no config file.

You can confirm the header path works before wiring any client to it:

```bash
curl -s -X POST https://<your-service-url>/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

That should list all six tools. The same request without the header returns 401.

### Rotating the token

```bash
openssl rand -base64 32 | tr -d '\n' | gcloud secrets versions add bunpro-mcp-token --data-file=-
./deploy.sh
```

Cloud Run picks up the new version on the next revision. **Every issued token stops working**, because the signing key derives from this secret — so reconnect the web connector and update any header-based config. That's the intended behaviour: rotating the secret is how you revoke access.

---

## Reading your notes in Obsidian

Mirror the bucket down on a timer (`OnBootSec=1min`, `OnUnitActiveSec=2h`, or an equivalent crontab pair):

```bash
gsutil -m rsync -r gs://$VAULT_BUCKET/ ~/Obsidian/Japanese/
```

**Download only — never sync back up.** The container is the bucket's only writer, which is what keeps write conflicts rare; pushing local edits up would need real conflict resolution that isn't built. The consequence, knowingly accepted: prose you edit locally in Obsidian stays local. Also don't pass `-d` with a destination above the vault folder, or it will delete unrelated files.

---

## Troubleshooting

**The connector won't connect, with no useful error.** Almost always `MCP_PUBLIC_URL` not matching the URL Claude dialled — it's advertised as the OAuth issuer and clients compare it. Check it:

```bash
curl https://<your-service-url>/.well-known/oauth-authorization-server
```

The `issuer` must be your service URL. If it says `http://localhost:8080`, the variable was never set — run `./deploy.sh`, or set it manually as shown above.

**The login page rejects the correct token.** Check the stored secret for a trailing newline:

```bash
gcloud secrets versions access latest --secret=bunpro-mcp-token | xxd | tail -1
```

If it ends `0a`, it was created without `tr -d '\n'`. The server strips whitespace, so this should no longer bite, but a secret created before that fix and never rotated is worth checking.

**Tools don't appear after connecting.** Confirm the server sees the vault — `/health` returns 200 even when the bucket is unreachable, deliberately, so that a transient storage error doesn't take down a healthy revision. Check the logs:

```bash
gcloud run services logs read bunpro-mcp --region asia-southeast1 --limit 50
```

**Reviews land on the wrong day.** `TZ` isn't set. See Configuration.

---

## Notes

- **Nothing is ever deleted.** Items you're done with are suspended, never removed from the vault. Bucket versioning is on as a backstop.
- **Revocation is rotation.** The server issues signed, self-contained tokens with no revocation list — `/revoke` is advertised for spec compliance but can't invalidate a token early. Rotating `MCP_AUTH_TOKEN` invalidates all of them at once.
