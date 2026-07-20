#!/usr/bin/env bash
#
# deploy.sh — deploy bunpro-mcp to Google Cloud Run.
#
# Idempotent: safe to re-run. Creates the bucket, secret, and service only
# if they don't already exist, then deploys the current source. See README.md
# ("Deploying") for the manual equivalent.
#
# Usage:
#   ./deploy.sh                      # deploy (prompts before creating a secret)
#   VAULT_BUCKET=my-vault ./deploy.sh
#   ./deploy.sh --seed ~/Obsidian/Japanese   # seed the bucket, then deploy
#   ./deploy.sh --seed ~/Obsidian/Japanese --dry-run-seed
#
# Config via env vars (defaults shown):
#   PROJECT      — GCP project id            (default: current gcloud project)
#   REGION       — Cloud Run region          (default: asia-southeast1)
#   VAULT_BUCKET — GCS bucket for the notes   (default: <project>-bunpro-vault)
#   SERVICE      — Cloud Run service name     (default: bunpro-mcp)
#   SECRET       — Secret Manager secret name (default: bunpro-mcp-token)
#   TZ_VALUE     — learner timezone           (default: Asia/Singapore)

set -euo pipefail

# --- config ------------------------------------------------------------------
REGION="${REGION:-asia-southeast1}"
SERVICE="${SERVICE:-bunpro-mcp}"
SECRET="${SECRET:-bunpro-mcp-token}"
TZ_VALUE="${TZ_VALUE:-Asia/Singapore}"

SEED_PATH=""
SEED_DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed)          SEED_PATH="${2:?--seed needs a path}"; shift 2 ;;
    --dry-run-seed)  SEED_DRY_RUN=1; shift ;;
    -h|--help)       sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Run from the repo root (where this script lives) so `--source .` is correct.
cd "$(dirname "$0")"

# --- prerequisites -----------------------------------------------------------
command -v gcloud >/dev/null || { echo "gcloud not found. Install the Google Cloud CLI." >&2; exit 1; }
command -v gsutil >/dev/null || { echo "gsutil not found. Install the Google Cloud CLI." >&2; exit 1; }

if ! gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q .; then
  echo "Not authenticated. Run: gcloud auth login" >&2
  exit 1
fi

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[[ -n "$PROJECT" && "$PROJECT" != "(unset)" ]] || { echo "No project set. Run: gcloud config set project <PROJECT_ID>  (or PROJECT=... ./deploy.sh)" >&2; exit 1; }
VAULT_BUCKET="${VAULT_BUCKET:-${PROJECT}-bunpro-vault}"

echo "Project: $PROJECT"
echo "Region:  $REGION"
echo "Bucket:  gs://$VAULT_BUCKET"
echo "Service: $SERVICE"
echo "Secret:  $SECRET"
echo

# --- 0. enable required APIs -------------------------------------------------
echo "==> Enabling required APIs (no-op if already on)..."
gcloud services enable \
  run.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROJECT"

# --- 1. bucket + versioning --------------------------------------------------
if gsutil ls -b "gs://$VAULT_BUCKET" >/dev/null 2>&1; then
  echo "==> Bucket gs://$VAULT_BUCKET already exists."
else
  echo "==> Creating bucket gs://$VAULT_BUCKET..."
  gsutil mb -p "$PROJECT" -l "$REGION" "gs://$VAULT_BUCKET"
fi
# Versioning on: nothing in this system ever deletes a note — a backstop.
gsutil versioning set on "gs://$VAULT_BUCKET"

# --- 2. auth secret ----------------------------------------------------------
if gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null 2>&1; then
  echo "==> Secret $SECRET already exists (leaving its value untouched)."
else
  echo "==> Creating secret $SECRET with a fresh random token..."
  openssl rand -base64 32 | gcloud secrets create "$SECRET" \
    --project "$PROJECT" --data-file=-
  echo "    A new bearer token was generated. Retrieve it after deploy with:"
  echo "    gcloud secrets versions access latest --secret=$SECRET --project=$PROJECT"
fi

# --- 3. optional: seed the bucket from a local vault -------------------------
if [[ -n "$SEED_PATH" ]]; then
  echo "==> Seeding bucket from $SEED_PATH ..."
  SEED_ARGS=("$SEED_PATH" --bucket "$VAULT_BUCKET")
  [[ "$SEED_DRY_RUN" -eq 1 ]] && SEED_ARGS+=(--dry-run)
  if command -v uv >/dev/null; then
    uv run python scripts/seed_bucket.py "${SEED_ARGS[@]}"
  else
    python scripts/seed_bucket.py "${SEED_ARGS[@]}"
  fi
fi

# --- 4. grant the runtime service account read access to the secret ----------
# The Cloud Run revision runs as this SA and reads MCP_AUTH_TOKEN from Secret
# Manager at startup. Without secretAccessor on the secret, the deploy fails
# with "Permission denied on secret". This must happen BEFORE the deploy.
PROJECT_NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUM}-compute@developer.gserviceaccount.com}"
echo "==> Granting roles/secretmanager.secretAccessor on $SECRET to $RUNTIME_SA..."
gcloud secrets add-iam-policy-binding "$SECRET" \
  --project "$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None >/dev/null

# --- 5. deploy ---------------------------------------------------------------
# --allow-unauthenticated is intentional: Claude can't mint Google IAM tokens,
# so the bearer token (from Secret Manager) is the real gate. Every route but
# /health returns 401 without it. The secret goes via --set-secrets, never
# --set-env-vars (env vars are visible in the console and deploy logs).
echo "==> Deploying $SERVICE to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --allow-unauthenticated \
  --max-instances=1 \
  --memory 512Mi \
  --timeout 300 \
  --set-env-vars "VAULT_BUCKET=$VAULT_BUCKET,TZ=$TZ_VALUE" \
  --set-secrets "MCP_AUTH_TOKEN=$SECRET:latest"

# --- 6. scope the service account to this bucket only ------------------------
SA="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')"
[[ -n "$SA" ]] || SA="$RUNTIME_SA"
echo "==> Granting roles/storage.objectAdmin on gs://$VAULT_BUCKET to $SA (bucket-scoped only)..."
gsutil iam ch "serviceAccount:${SA}:roles/storage.objectAdmin" "gs://$VAULT_BUCKET"

# --- 7. health check ---------------------------------------------------------
URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(status.url)')"

# OAuth discovery advertises MCP_PUBLIC_URL as the issuer, and clients compare
# it against the URL they dialled — a mismatch fails the connection. The URL
# only exists after the first deploy, so set it now and skip the update on
# every subsequent run.
CURRENT_PUBLIC_URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].env.filter("name":"MCP_PUBLIC_URL").extract("value").flatten())' 2>/dev/null || true)"
if [[ "$CURRENT_PUBLIC_URL" != "$URL" ]]; then
  echo "==> Setting MCP_PUBLIC_URL=$URL (OAuth issuer) and redeploying that revision..."
  gcloud run services update "$SERVICE" --project "$PROJECT" --region "$REGION" \
    --update-env-vars "MCP_PUBLIC_URL=$URL"
fi

echo
echo "==> Deployed. Service URL: $URL"
echo "==> Health check:"
if curl -fsS "$URL/health"; then
  echo
  echo "OK."
else
  echo "Health check did not return OK yet — the revision may still be starting." >&2
fi

cat <<EOF

Next steps:
  1. Get the server token (it is the password on the login page):
       gcloud secrets versions access latest --secret=$SECRET --project=$PROJECT
  2. In Claude settings, Add custom connector:
       URL: $URL/mcp
     Claude registers itself and opens a login page; paste the token there.
     Leave the Advanced OAuth Client ID / Secret fields empty.
  3. (If you didn't seed) load notes with:
       ./deploy.sh --seed ~/Obsidian/Japanese --dry-run-seed
       ./deploy.sh --seed ~/Obsidian/Japanese
EOF
