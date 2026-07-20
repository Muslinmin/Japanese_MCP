"""The front door. Declares tools, validates, hands off to lower modules.

Served over streamable HTTP behind a bearer token (see `auth.py`). Logging
goes to stderr so the platform collects it.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from mcp.server.auth.settings import (  # noqa: E402
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from bunpro_mcp import ingest  # noqa: E402
from bunpro_mcp.auth import (  # noqa: E402
    SCOPE,
    StatelessOAuthProvider,
    login_page,
    login_submit,
    public_url,
    require_token_env,
)
from bunpro_mcp.ingest import make_item  # noqa: E402
from bunpro_mcp.models import (  # noqa: E402
    AddReport,
    Grade,
    GradeReport,
    ImportReport,
    ItemDetail,
    Kind,
    Level,
    PracticePoolResponse,
    Progress,
    QueueResponse,
)
from bunpro_mcp.scoring import apply_grade, is_mastered, to_queue_entry  # noqa: E402
from bunpro_mcp.storage import GCSBackend  # noqa: E402
from bunpro_mcp.vault import Vault  # noqa: E402

logger = logging.getLogger(__name__)

GRAMMAR_FOCUS_MAX = 3

# The learner is in UTC+8 and the container is in UTC. Without this, a
# review at 9pm local lands on tomorrow's date and the SRS schedule drifts
# a day — so the day boundary follows the learner, not the machine.
DEFAULT_TIMEZONE = "Asia/Singapore"
DEFAULT_PORT = 8080


class ItemNotFound(Exception):
    pass


def _today():
    return datetime.now(ZoneInfo(os.environ.get("TZ") or DEFAULT_TIMEZONE)).date()


def _port() -> int:
    return int(os.environ.get("PORT") or DEFAULT_PORT)


# Config is validated at import so a misconfigured deploy dies immediately
# rather than 500ing on the first tool call. Reaching the bucket is *not*
# checked here: a transient GCS blip at boot would stop the revision ever
# going healthy, and the platform would loop restarting it.
VAULT_BUCKET = os.environ.get("VAULT_BUCKET")
VAULT_PATH = os.environ.get("VAULT_PATH")
if not VAULT_BUCKET and not VAULT_PATH:
    raise RuntimeError(
        "Set VAULT_BUCKET (GCS bucket holding the notes) for a deployed server, "
        "or VAULT_PATH (local folder) for local runs. Neither is set."
    )

_vault: Vault | None = None


def get_vault() -> Vault:
    """The vault, built on first use rather than at import.

    Lazy because construction lists the bucket, and that is a network call
    no import should depend on.
    """
    global _vault
    if _vault is None:
        if VAULT_BUCKET:
            _vault = Vault(backend=GCSBackend(VAULT_BUCKET))
        else:
            _vault = Vault(Path(VAULT_PATH))
    return _vault


mcp = FastMCP(
    "bunpro",
    host="0.0.0.0",
    port=_port(),
    # Every tool is a self-contained request/response — nothing streams and
    # nothing is remembered between calls, so any instance can serve any
    # request and there are no sessions to lose when the platform scales to
    # zero between reviews.
    stateless_http=True,
    # The SDK serves /authorize, /token, /register, /revoke and both
    # discovery documents from this; auth.py supplies only the parts it
    # can't know — where credentials live and who the user is.
    auth=AuthSettings(
        issuer_url=public_url(),
        resource_server_url=public_url(),
        client_registration_options=ClientRegistrationOptions(
            # Claude registers itself dynamically; there is no console here
            # in which to pre-create a client.
            enabled=True,
            valid_scopes=[SCOPE],
            default_scopes=[SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
        # No required scopes: a token this server issued is a token from
        # the one person who knows the password. Demanding a scope only
        # adds a way for a client that requested none to be locked out.
        required_scopes=None,
    ),
    auth_server_provider=StatelessOAuthProvider(),
)


# Both are public by design — the SDK protects /mcp, and custom routes are
# mounted outside that. A login page behind auth could not be logged into.
mcp.custom_route("/login", methods=["GET"])(login_page)
mcp.custom_route("/login", methods=["POST"])(login_submit)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Liveness only. Deliberately does not touch the bucket — a probe that
    fails on a transient storage error would take down a server that is
    otherwise fine."""
    return JSONResponse({"status": "ok"})


@mcp.tool()
def get_review_queue(
    limit: int = 20,
    kind: Literal["grammar", "vocab", "both"] = "both",
) -> QueueResponse:
    """Get the Japanese items most in need of review right now, ordered by
    priority. Call this at the start of a review session. Returns grammar
    points and vocabulary with a freshness score for each, plus a note on
    how the learner last got it wrong."""
    today = _today()
    everything = get_vault().load_all()
    total_items = len(everything)  # everything in the vault, suspended included
    all_items = [item for item in everything if not item.suspended]

    all_entries = [to_queue_entry(item, today) for item in all_items]

    grammar_entries = sorted(
        (e for e in all_entries if e.kind == "grammar"),
        key=lambda e: e.priority,
        reverse=True,
    )
    grammar_focus = [e.id for e in grammar_entries[:GRAMMAR_FOCUS_MAX]]

    filtered = all_entries if kind == "both" else [e for e in all_entries if e.kind == kind]
    filtered.sort(key=lambda e: e.priority, reverse=True)
    queue = filtered[:limit]

    return QueueResponse(
        generated_at=today,
        total_items=total_items,
        returned=len(queue),
        grammar_focus=grammar_focus,
        queue=queue,
    )


@mcp.tool()
def get_practice_pool(
    limit: int = 60,
    kind: Literal["grammar", "vocab", "both"] = "both",
) -> PracticePoolResponse:
    """Get a pool of Japanese words and grammar the learner has ALREADY
    mastered, for active-use practice — the opposite of the review queue.
    Use this when the learner wants to be tested on or practice words they
    already know, not review what they're forgetting. Workflow: (1) ideate a
    concrete conversation theme or scenario (ordering at an izakaya,
    complaining about the weather, a job interview); (2) call this to get the
    mastered pool; (3) the pool is NOT pre-filtered by theme — from it, you
    pick the words and grammar that fit your theme, using each item's meaning;
    (4) propose the scenario and your chosen words to the learner and get
    their buy-in before starting; (5) run the practice conversation; (6) at
    the end, call submit_grades once for every item you practiced — grade
    fluent use 3 or 4, hesitation 2, and a blank or misuse 1 with a
    one-sentence error_note. Returns each item with its meaning and reading so
    you can select by theme."""
    today = _today()
    mastered = [i for i in get_vault().load_all() if not i.suspended and is_mastered(i)]
    selected = mastered if kind == "both" else [i for i in mastered if i.kind == kind]
    selected.sort(key=lambda i: i.memory.stability, reverse=True)
    pool = [to_queue_entry(i, today) for i in selected[:limit]]

    return PracticePoolResponse(
        generated_at=today,
        total_mastered=len(mastered),
        returned=len(pool),
        pool=pool,
    )


@mcp.tool()
def get_item(item_id: str) -> ItemDetail:
    """Get everything known about one Japanese grammar point or word,
    including the learner's own notes from their vault."""
    detail = get_vault().get_detail(item_id)
    if detail is None:
        raise ItemNotFound(
            f"No item with id {item_id!r}. Call get_review_queue to see valid ids."
        )
    item, body = detail
    entry = to_queue_entry(item, _today())
    return ItemDetail(**entry.model_dump(), body=body)


@mcp.tool()
def submit_grades(grades: list[Grade]) -> GradeReport:
    """Record how the learner performed on items during a review. Call this
    at the END of a review session, once, with every item you observed.
    Grade 1 = could not recall or used it wrong, 2 = struggled, 3 = correct,
    4 = effortless. Include a one-sentence error_note when they got it
    wrong, describing the specific mistake."""
    today = _today()
    vault = get_vault()
    updated = 0
    unknown_ids: list[str] = []
    summary: list[str] = []

    for grade in grades:
        item = vault.get(grade.item_id)
        if item is None:
            unknown_ids.append(grade.item_id)
            continue

        new_state = apply_grade(item.memory, grade.grade, today, grade.error_note)
        vault.write_memory(item.id, new_state)
        updated += 1

        if grade.grade == 1:
            summary.append(f"{item.surface}: marked as a lapse, will resurface soon")
        else:
            summary.append(f"{item.surface}: next review in ~{round(new_state.stability)} days")

    return GradeReport(updated=updated, unknown_ids=unknown_ids, summary=summary)


@mcp.tool()
def add_item(
    surface: str,
    kind: Kind,
    reading: str | None = None,
    meaning: str | None = None,
    level: Level | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    progress: Progress | None = None,
) -> AddReport:
    """Add a Japanese grammar point or word the learner has just
    encountered. Fill in the reading, meaning, and JLPT level yourself from
    your own knowledge of Japanese — do not ask the learner for them unless
    the word is genuinely ambiguous. Put any context the learner gave you
    (where they met it, what confused them) into `note`. If the learner
    says they already partly know this word (e.g. "I'm Adept on this" or
    quotes a Bunpro SRS stage), pass that bucket as `progress` — one of
    Beginner, Adept, Seasoned, Expert, Master — so the review schedule
    starts from their actual familiarity instead of treating it as brand
    new. Leave `progress` unset for something they are meeting for the
    first time."""
    today = _today()
    vault = get_vault()
    candidate = make_item(
        surface, kind, today, reading=reading, meaning=meaning, level=level, progress=progress
    )
    if tags:
        candidate.tags = tags

    existing = vault.get(candidate.id)
    if existing is not None:
        return AddReport(created=False, already_exists=True, item=existing)

    vault.upsert(candidate, body=note)
    created_item = vault.get(candidate.id)
    assert created_item is not None
    return AddReport(created=True, already_exists=False, item=created_item)


@mcp.tool()
def import_export(csv_content: str, dry_run: bool = True) -> ImportReport:
    """Import a Bunpro CSV export into the vault. Pass the CSV's *contents*
    as text, not a filename. ALWAYS run with dry_run=true first and show the
    learner the report before running for real."""
    return ingest.import_export(get_vault(), csv_content, _today(), dry_run)


def main() -> None:
    import uvicorn

    require_token_env()
    # No middleware to add: the SDK wraps /mcp in RequireAuthMiddleware
    # itself once auth is configured, and /health and /login are meant to
    # be reachable without a credential.
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=_port())


if __name__ == "__main__":
    main()
