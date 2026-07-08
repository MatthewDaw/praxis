"""FastAPI server implementing the candidate-api-v1 contract over the facts spine.

Single source of truth: the ``facts`` table (one tenant graph per
``(org_id, user_id)``), reached through :class:`PostgresVectorGraph`. The
dashboard "candidate" read model is a projection of facts via
:class:`knowledge.serve.facts_candidates.FactsCandidates`; the graph view, the
MCP ``get_context`` retrieval, and the Contradictions tab all read the same rows.

Saved graph states are org-shared **snapshots** inside a **space**, held in the
``snapshots`` table keyed ``(org_id, space, snapshot)`` (eval fixtures use the
reserved ``__evals__`` space, one snapshot per case id). Loading a snapshot copies
its rows into the caller's working memory; dumping copies working memory into a
snapshot. A ``(space, snapshot)`` is addressed explicitly via the
``X-Praxis-Space`` + ``X-Praxis-Snapshot`` headers (generic routes) or request
body/query params (snapshot/mount routes) — never by mangling ``user_id``.

Every data route hard-requires a valid Cognito JWT (the ``current_user``
dependency) and resolves the active org from the ``X-Praxis-Org`` header; the
caller must be a member of that org. ``/health`` stays open. A Postgres DSN is
required (no JSON/offline fallback).

Run: uv run python -m knowledge.serve   (serves on http://localhost:8000)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv

# Load the repo-root ``.env`` (OPENROUTER/COGNITO/PRAXIS_DB_URL ...) before any
# ``knowledge.*`` import reads the environment. Without this the server starts
# with an empty env: no DB, no Cognito ("invalid token"), no embedder key.
load_dotenv()

# Export LLM/embedding spans to Phoenix when PHOENIX_COLLECTOR_ENDPOINT is set
# (no-op otherwise). Must run here, not in __main__, because uvicorn imports the
# app by string and never executes that block.
from knowledge.observability.tracing import setup_tracing  # noqa: E402

setup_tracing()

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402

from knowledge.knowledge_graph.knowledge_graph_variants.org_source_reader import (  # noqa: E402
    OrgSourceReader,
)
from knowledge.knowledge_graph.knowledge_graph_variants.overlay_graph import (  # noqa: E402
    OverlayGraph,
)
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    _READ_CHAR_BUDGET,
    CHECK_CATEGORY,
    DEFAULT_LEASE_TTL_SECONDS,
    EPISODIC_CATEGORY,
    LeaseConflict,
    PostgresVectorGraph,
    SnapshotKindError,
    default_write_policy,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    ClaimConflictDetector,
    ClaimExtractionJudge,
    ClaimExtractor,
    ClaimValueJudge,
    ConflictOverwriter,
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.memoizing_embedder import MemoizingEmbedder  # noqa: E402
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm  # noqa: E402
from knowledge.serve import batch_writer, db, graph_adapter  # noqa: E402
from knowledge.serve.auth import Principal, make_current_user  # noqa: E402
from knowledge.serve.facts_candidates import (  # noqa: E402
    FactsCandidates,
    PromotionError,
)
from knowledge.serve.mounted_store import MountedStore  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
from knowledge.serve.spaces_store import SpacesStore  # noqa: E402
from knowledge.serve.reserved_names import (  # noqa: E402
    RESERVED_EVAL_SPACE,
    is_reserved_space_id,
)
from knowledge.serve.rate_limit import (  # noqa: E402
    LLM_RATE_LIMIT,
    build_limiter,
)
from knowledge.serve.regenerate import (  # noqa: E402
    PipelineConfig,
    RegenerateUnavailableError,
    case_ids_for,
    distill_case,
)
from knowledge.wiring import build_trio  # noqa: E402

_DEFAULT_CORS_REGEX = (
    r"(http://(localhost|127\.0\.0\.1):\d+|https://[\w-]+\.onrender\.com"
    r"|https://[\w-]+\.cloudfront\.net|https://[\w-]+\.awsapprunner\.com"
    r"|https://[\w-]+\.praxiskg\.com)"
)


def _cors_origin_regex() -> str:
    custom = os.getenv("PRAXIS_CORS_ORIGIN_REGEX", "").strip()
    return custom or _DEFAULT_CORS_REGEX


# Max accepted request-body text size for the LLM-cost write routes (/ingest,
# /insights, /ingest/session), returning 413 above it. One shared ceiling so the
# paid surface has a uniform cap.
_MAX_BODY_BYTES = 128 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- shared insight-write helpers (single + batch paths, gap H8) -------------
# The single POST /insights and the bulk POST /insights/batch run the exact same
# per-insight write, so the logic lives here once and both endpoints call it. The
# batch path constructs the policy graph + ingestor ONCE and loops these helpers
# over the items, so N facts cost one HTTP/auth round-trip and one graph/embedder/
# connection setup — and the writes are serialized within the single request
# (avoiding the concurrent-write-burst 500s the local loop otherwise has to skirt).


def _insight_write_policy(on_conflict: str) -> list:
    """The write policy for a confirmed insight, keyed on conflict handling.

    ``surface`` flags a clash as a pending contradiction (keeps both); the default
    ``auto_resolve`` force-overwrites the loser. Mirrors the single-insight path.
    """
    if on_conflict == "surface":
        return default_write_policy()
    return [Redactor(), Deduper(), ConflictOverwriter(llm=OpenRouterLlm())]


def _record_episode(graph: PostgresVectorGraph, insight: str, body: dict[str, Any]) -> dict[str, Any]:
    """Store an episodic (decision-log) insight whole, bypassing the semantic pipeline (H4).

    ``retrievable`` confirms the read-your-writes contract (gap H8): the just-
    written fact is fetchable immediately, since writes commit on autocommit.
    """
    ep = (body.get("meta") or {}).get("episode") or {}
    fid = graph.record_episode(
        insight,
        alternatives=ep.get("alternatives"),
        outcome=ep.get("outcome", "pending"),
        decided_at=ep.get("decided_at"),
        derived_from=body.get("derivedFrom"),
    )
    return {
        "summary": "recorded episode",
        "action": "episode",
        "id": fid,
        "onConflict": "n/a",
        "contradictionsSurfaced": 0,
        "retrievable": graph.get_fact(fid) is not None,
    }


def _write_insight(
    graph: PostgresVectorGraph,
    ingestor: Any,
    *,
    insight: str,
    source: str | None,
    scope: str | None,
    category: str | None,
    meta: dict | None,
    on_conflict: str,
    derived_from: list | None = None,
) -> dict[str, Any]:
    """Ingest one confirmed insight into ``graph`` and report what happened.

    Returns the same shape the single ``POST /insights`` returns, plus
    ``retrievable`` (gap H8): True when the written fact is found by an immediate
    read-back — a confirmable read-your-writes signal so a caller need not poll.
    """
    before = graph.search(insight, top_k=1, state=None)
    before_contradictions = set(graph.all_edges("contradiction"))
    ingestor.ingest(  # human-gated -> live knowledge
        insight,
        state="active",
        source=source,
        scope=scope,
        category=category,
        meta=meta,
        # Shaped-fact lane: an ``add_insight`` payload is an already-distilled,
        # self-contained fact. Keep it WHOLE so a multi-sentence insight (e.g. a
        # settled requirement with its "Acceptance: ..." clause) lands as ONE fact
        # instead of fragmenting one-per-sentence. Dedup + contradiction surfacing
        # still run in ``graph.write``. Document/session distillation paths (/ingest,
        # /ingest/session, regeneration) keep the default atomic=False and split.
        atomic=True,
    )
    after = graph.search(insight, top_k=1, state=None)
    new_contradictions = set(graph.all_edges("contradiction")) - before_contradictions
    prior = before[0].fact if before else None
    top = after[0].fact if after else None
    # H5: link derivation provenance from the resulting fact to its sources, so an
    # invalidated source can later surface this fact as suspect. (The episode branch
    # records derivedFrom via record_episode instead.)
    if derived_from and top is not None:
        graph.record_derivation(top.id, [str(s) for s in derived_from])
    # surface mode that flagged a clash: report it as a pending contradiction so
    # the caller knows to go adjudicate it (the fact still landed, possibly
    # demoted to proposed by FR-005).
    if new_contradictions:
        action = "surfaced"
    elif prior is not None and top is not None and prior.id == top.id:
        # Same-id top means a dedup merge bumped the existing fact; otherwise the
        # insight was added (auto_resolve may have rejected + linked conflicts).
        action = "merged"
    else:
        action = "added"
    return {
        "summary": f"{action} insight",
        "action": action,
        "id": top.id if top is not None else None,
        "onConflict": on_conflict,
        "contradictionsSurfaced": len(new_contradictions),
        "retrievable": top is not None,
    }


def _check_upsert(
    graph: PostgresVectorGraph,
    *,
    insight: str,
    source: str | None,
    scope: str | None,
    meta: dict | None,
) -> dict[str, Any]:
    """Identity-keyed write for a validation/planning CHECK — NEVER text-deduped or reconciled.

    A check is a DECLARATIVE GATE keyed on ``meta.check_id`` + ``meta.run`` (the command whose
    non-zero exit fails the ticket), not a knowledge assertion to reconcile against the corpus.
    Two checks with different ``check_id``/``run`` enforce DIFFERENT tests and must stay distinct
    facts even when their prose reads alike, so the semantic dedup + conflict/claim pipeline must
    NOT run (that silently merged a new check into a similar one and dropped its ``run``). Identity
    is the ``check_id``:

    * same ``meta.check_id`` already in this (space, snapshot) → UPDATE that one fact in place
      (title/content/meta/run) — never a duplicate;
    * a new or absent-but-distinct ``check_id`` → ALWAYS a new distinct fact.

    The caller passes a REDACT-ONLY ``graph`` (``policy=[Redactor()]`` — no Deduper, no
    ConflictOverwriter/ClaimConflictDetector), so the insert path can never merge, overwrite, or
    raise a contradiction; ``onConflict`` therefore does not apply to checks. Secrets are still
    scrubbed by the redactor. The write-time section invariant still enforces that the fact fits
    the destination snapshot's kind (a validation check only in ``building-validation``, etc.).
    """
    meta = dict(meta or {})
    check_id = str(meta.get("check_id") or "").strip()
    existing = None
    if check_id:
        for f in graph.facts_by(
            category=CHECK_CATEGORY, state=None, meta_filter={"check_id": check_id}
        ):
            existing = f
            break
    if existing is not None:
        graph.update_fact(
            existing.id,
            text=insight,
            source=source,
            meta={**(existing.meta or {}), **meta},
            category=CHECK_CATEGORY,
        )
        return {
            "summary": f"check {check_id!r} updated in place",
            "action": "updated",
            "id": existing.id,
            "contradictionsSurfaced": 0,
            "retrievable": True,
        }
    fid = graph.write(
        insight, state="active", source=source, scope=scope,
        category=CHECK_CATEGORY, meta=meta,
    )
    return {
        "summary": "check stored",
        "action": "added",
        "id": fid,
        "contradictionsSurfaced": 0,
        "retrievable": fid is not None,
    }


def _batch_result_from_outcome(
    outcome: "batch_writer.BatchOutcome", on_conflict: str, index: int
) -> dict[str, Any]:
    """Render a batch writer outcome into the per-item result shape ``_write_insight``
    returns, derived from the WriteDecision instead of a before/after re-search.

    ``action`` mirrors ``_write_insight``: a contradiction flag -> "surfaced"; a
    dedup/augment merge into an existing fact -> "merged"; an add or force-overwrite
    (a fresh row) -> "added". ``contradictionsSurfaced`` counts the decision's
    ``contradiction:<id>`` flags (the edges ``persist`` records)."""
    if outcome.error is not None:
        return {"ok": False, "error": outcome.error, "index": index}
    decision = outcome.decision
    if decision is None:
        # A policy step suppressed the write (empty text is filtered earlier);
        # nothing landed, so it's ok but not retrievable.
        return {
            "ok": True, "summary": "added insight", "action": "added", "id": None,
            "onConflict": on_conflict, "contradictionsSurfaced": 0, "retrievable": False,
        }
    contradictions = sum(1 for f in decision.flags if f.startswith("contradiction:"))
    if contradictions:
        action = "surfaced"
    elif decision.action in ("update", "augment"):
        action = "merged"
    else:
        action = "added"
    return {
        "ok": True,
        "summary": f"{action} insight",
        "action": action,
        "id": outcome.fact_id,
        "onConflict": on_conflict,
        "contradictionsSurfaced": contradictions,
        "retrievable": outcome.fact_id is not None,
    }


class _ConnProxy:
    """A connection handle that forwards every access to *the calling thread's*
    real connection (resolved fresh on each attribute access).

    FastAPI runs sync endpoints in a thread pool, and a psycopg connection is not
    meant for concurrent cross-thread use. The server used to share ONE connection
    across the orgs store, auth, and every per-request graph: under a burst of
    concurrent writes they serialized on (and could wedge) that single connection,
    so one stuck/erroring write cascaded into 500s on all other writes *and* reads
    until the process was restarted (H13.2) — and a wedged connection also broke
    membership reads, so a user's orgs looked empty (H13.3, even though the rows
    are durable in Postgres). This proxy lets the app keep one ``conn`` reference
    while each worker thread transparently uses its own connection underneath.
    """

    __slots__ = ("_resolve",)

    def __init__(self, resolve: Callable[[], Any]) -> None:
        object.__setattr__(self, "_resolve", resolve)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


def create_app(conn: Any | None = None) -> FastAPI:
    """Build the app over per-thread Postgres connections.

    A passed-in ``conn`` (tests) is used directly and unshared (sequential). In
    production (``conn is None``) each worker thread lazily opens its own
    autocommit connection, reopened transparently if the DB dropped it (so a DB
    restart self-heals instead of needing an app restart). A resolvable DSN is
    required.
    """
    # Tracing is set up once at module import (see top of file); setup_tracing is
    # idempotent, so no need to call it again here.
    if conn is not None:
        # Explicit single connection (tests): one tenant, sequential — use as-is.
        # Bind to a separate name so the closure doesn't capture the ``conn`` var
        # we rebind to the proxy below (which would make resolve return the proxy).
        _explicit_conn = conn
        resolve_conn: Callable[[], Any] = lambda: _explicit_conn  # noqa: E731
        # A single explicit connection (tests): no independent per-worker
        # connections to hand out, so the batch writer runs serially on it.
        make_worker_conn: Callable[[], Any] | None = None
    else:
        dsn = db.resolve_dsn()
        if dsn is None:
            raise RuntimeError(
                "No Postgres DSN available: set PRAXIS_DB_URL, or configure "
                "PRAXIS_DB_SECRET with AWS credentials."
            )
        _tls = threading.local()

        def resolve_conn() -> Any:
            c = getattr(_tls, "conn", None)
            if c is not None and not c.closed and not getattr(c, "broken", False):
                return c
            # First use on this thread, or the prior connection died (e.g. the DB
            # restarted / dropped it) — open a fresh autocommit connection.
            c = db.connect(dsn)
            _tls.conn = c
            return c

        # The batch writer decides items in parallel, each worker on its OWN
        # connection (a psycopg connection is not safe for concurrent use). This
        # opens a fresh autocommit connection the worker closes when done.
        make_worker_conn = lambda: db.connect(dsn)  # noqa: E731

    conn = _ConnProxy(resolve_conn)
    orgs_store = OrgsStore(conn)
    mounted_store = MountedStore(conn)
    spaces_store = SpacesStore(conn)
    # Bind the auth dependency to this connection so it can also resolve API keys
    # (X-Praxis-Key) in addition to the Cognito Bearer JWT / dev seam.
    current_user = make_current_user(conn)
    # Test seam: lets reliability tests assert per-thread isolation + reopen.
    app_get_conn = resolve_conn

    # Reserved space ids (eval cache + the retired standalone layout) live in
    # ``reserved_names`` as the single source of truth (see ``is_reserved_space_id``).

    def _require_space(org: str, target: tuple[str, str]) -> tuple[str, str]:
        """Validate a snapshot target's space exists (404 otherwise); return it.

        The reserved eval space is never addressable through the generic
        snapshot-target seam (it has no ``spaces`` registry row anyway).
        """
        space, snap = target
        if space == RESERVED_EVAL_SPACE or not spaces_store.exists(org, space):
            raise HTTPException(status_code=404, detail=f"unknown space {space!r}")
        return space, snap

    def candidates_for(
        org: str, sub: str, target: tuple[str, str] | None = None
    ) -> FactsCandidates:
        """The candidate facade for the requester's working memory, or a snapshot.

        With no ``target`` this is the requester's private working-memory graph.
        With a ``(space, snapshot)`` target it projects the candidate surface over
        that org-shared snapshot so the factory can read/mutate a project
        snapshot's tickets directly by id.
        """
        if target is None:
            return FactsCandidates(conn, org, sub)
        space, snap = _require_space(org, target)
        return FactsCandidates(
            conn, org, sub, facts_table="snapshots", space=space, snapshot=snap
        )

    def live_graph(org: str, sub: str) -> PostgresVectorGraph:
        """The requester's working-memory graph (no write policy needed for reads)."""
        return PostgresVectorGraph(conn, org, sub)

    def graph_for(
        org: str,
        sub: str,
        target: tuple[str, str] | None,
        *,
        policy: list | None = None,
    ) -> PostgresVectorGraph:
        """Resolve the graph a generic read/write route operates on.

        ``target is None`` -> the requester's working memory (keyed ``(org, sub)``).
        A ``(space, snapshot)`` target -> the org-shared snapshot-bound graph
        (keyed ``(org, space, snapshot)``); an unknown space is a 404. This is the
        seam the factory uses to read/mutate a project snapshot (checks +
        ``prd-<project>`` tickets) with an explicit header pair, no user mangling.
        """
        if target is None:
            if policy is None:
                return live_graph(org, sub)
            return PostgresVectorGraph(conn, org, sub, policy=policy)
        space, snap = _require_space(org, target)
        kwargs: dict[str, Any] = {} if policy is None else {"policy": policy}
        return PostgresVectorGraph(
            conn, org, facts_table="snapshots", space=space, snapshot=snap, **kwargs
        )

    def _require_snapshot_for_check(category: Any, target: tuple[str, str] | None) -> None:
        """A validation/planning CHECK must be authored INTO a section snapshot, never working
        memory — which the factory never reads for checks, so a check written there is a silent
        no-op (the class of bug where af-build can't see an authored check). This is the
        working-memory half of the write-time section invariant: refuse a ``category="check"``
        write that carries no ``(space, snapshot)`` target so it fails LOUDLY instead of landing
        invisibly. Checks belong in ``building-validation``/``planning-validation`` (the snapshot's
        own kind invariant then enforces the right scope)."""
        if str(category or "").strip() == CHECK_CATEGORY and target is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "a category='check' fact must be authored into a section snapshot — pass "
                    "X-Praxis-Space + X-Praxis-Snapshot targeting 'building-validation' (validation) "
                    "or 'planning-validation' (planning). A check written to working memory is "
                    "invisible to the factory (af-build reads checks from the snapshot)."
                ),
            )

    app = FastAPI(title="Praxis Candidate API", version="1")
    # Test seam (see _ConnProxy): resolve the calling thread's live connection.
    app.state.get_conn = app_get_conn

    explicit_origins = [
        origin.strip()
        for origin in os.getenv("PRAXIS_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    cors_kwargs: dict[str, Any] = {
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if explicit_origins:
        cors_kwargs["allow_origins"] = explicit_origins
    else:
        cors_kwargs["allow_origin_regex"] = _cors_origin_regex()
    app.add_middleware(CORSMiddleware, **cors_kwargs)

    # Per-principal rate limiting (see knowledge/serve/rate_limit.py). Built here,
    # like CORS, because create_app() constructs the app inside the function. The
    # global default applies to every route; the LLM-cost routes layer a tighter
    # per-route limit via @limiter.limit below, and /health is exempted. Storage is
    # slowapi's in-memory store: per-instance only — fine for the single App Runner
    # instance today, but would need a shared backend (Redis) if it scales out.
    limiter = build_limiter()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    def active_org(
        principal: Principal = Depends(current_user),
        x_praxis_org: str | None = Header(default=None),
    ) -> str:
        """Resolve + authorize the requester's active org (from ``X-Praxis-Org``)."""
        org = x_praxis_org or "default"
        # API-key principals are scoped to exactly one org: the selected org must
        # equal the key's org (that match IS the membership for a key).
        if principal.api_key_org is not None:
            if org != principal.api_key_org:
                raise HTTPException(
                    status_code=403,
                    detail=f"API key is not scoped to org {org!r}",
                )
            return org
        if not orgs_store.is_member(org, principal.sub):
            raise HTTPException(status_code=403, detail=f"not a member of org {org!r}")
        return org

    def active_user_id(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> str:
        """The working-memory tenant ``user_id``: always the authenticated principal.

        Working memory is a per-user private live graph keyed ``(org, sub)``. There
        is no space-mangling: space/snapshot are explicit parameters on the
        snapshot/mount operations only (see ``snapshot_target``), never a
        header-driven working-graph selector. The name is kept to minimize the diff
        across the ~45 call sites that depend on it.
        """
        return principal.sub

    def snapshot_target(
        x_praxis_space: str | None = Header(default=None),
        x_praxis_snapshot: str | None = Header(default=None),
    ) -> tuple[str, str] | None:
        """Resolve an explicit ``(space, snapshot)`` target from headers, or None.

        When BOTH ``X-Praxis-Space`` and ``X-Praxis-Snapshot`` are present the
        generic read/write routes operate on that org-shared snapshot; when either
        is absent they use the requester's working memory. This is the seam that
        lets the factory read/mutate a project snapshot (checks + ``prd-<project>``
        tickets) without any ``{sub}::space:`` mangling.
        """
        space = (x_praxis_space or "").strip()
        snapshot = (x_praxis_snapshot or "").strip()
        if space and snapshot:
            return (space, snapshot)
        return None

    @app.get("/health")
    @limiter.exempt  # App Runner health check — never rate-limited.
    def health() -> dict[str, Any]:
        return {"status": "ok", "store": "postgres"}

    @app.get("/me")
    def me(principal: Principal = Depends(current_user)) -> dict[str, Any]:
        return {
            "sub": principal.sub,
            "email": principal.email,
            "orgs": orgs_store.list_orgs(principal.sub),
        }

    # --- orgs --------------------------------------------------------------
    @app.post("/orgs")
    def create_org(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        org_id, name, password = body.get("orgId"), body.get("name"), body.get("password")
        if not org_id or not password:
            raise HTTPException(status_code=400, detail="orgId and password required")
        try:
            orgs_store.create_org(str(org_id), name, str(password), principal.sub)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"orgId": org_id, "role": "owner"}

    @app.post("/orgs/join")
    def join_org(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        org_id, password = body.get("orgId"), body.get("password")
        if not org_id or not password:
            raise HTTPException(status_code=400, detail="orgId and password required")
        try:
            orgs_store.join_org(str(org_id), str(password), principal.sub)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"orgId": org_id, "role": "member"}

    @app.post("/orgs/password")
    def change_org_password(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        org_id = body.get("orgId")
        current, new = body.get("currentPassword"), body.get("newPassword")
        if not org_id or not current or not new:
            raise HTTPException(
                status_code=400,
                detail="orgId, currentPassword and newPassword required",
            )
        try:
            orgs_store.set_password(str(org_id), str(current), str(new), principal.sub)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"orgId": org_id, "status": "password_changed"}

    @app.patch("/orgs/{org_id}")
    def rename_org(
        org_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        """Rename an org's display name (owner-only). Mirrors ``delete_org``'s auth.

        Only ``name`` is mutable; ``org_id`` is the immutable tenant key. The same
        two-staged auth as delete (404 to a non-member so existence never leaks,
        403 to a member who is not the owner) plus the API-key scope guard, since
        the org comes from the path rather than the ``X-Praxis-Org`` header.
        """
        if principal.api_key_org is not None and principal.api_key_org != org_id:
            raise HTTPException(
                status_code=403, detail=f"API key is not scoped to org {org_id!r}"
            )
        if not orgs_store.is_member(org_id, principal.sub):
            raise HTTPException(status_code=404, detail=f"unknown org {org_id!r}")
        if not orgs_store.is_owner(org_id, principal.sub):
            raise HTTPException(
                status_code=403, detail="only an org owner can rename it"
            )
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        orgs_store.rename_org(org_id, name)
        return {"orgId": org_id, "name": name}

    @app.delete("/orgs/{org_id}")
    def delete_org(
        org_id: str,
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        """Permanently delete an org and EVERY member's data in it (owner-only).

        Authorization is two-staged so the response never leaks org existence to a
        non-member: a non-member gets 404 (indistinguishable from "no such org"),
        a member who is not the owner gets 403. The owner's delete purges all
        org-wide tenant storage (working memory + snapshots + mounts + api keys
        across every member) and then drops the ``orgs`` row, which cascades
        ``org_members`` + ``spaces``.
        Destructive and irreversible — this wipes the org for everyone in it.
        """
        # API-key principals are scoped to exactly one org (see ``active_org``); a
        # leaked key must not reach a sibling org its bound user happens to own —
        # least of all on this irreversible path. Other org routes get this via
        # ``Depends(active_org)``, but delete takes the org from the path (not the
        # ``X-Praxis-Org`` header), so enforce the same scope match explicitly.
        if principal.api_key_org is not None and principal.api_key_org != org_id:
            raise HTTPException(
                status_code=403, detail=f"API key is not scoped to org {org_id!r}"
            )
        if not orgs_store.is_member(org_id, principal.sub):
            raise HTTPException(status_code=404, detail=f"unknown org {org_id!r}")
        if not orgs_store.is_owner(org_id, principal.sub):
            raise HTTPException(
                status_code=403, detail="only an org owner can delete it"
            )
        _purge_org_storage(org_id)
        orgs_store.delete_org(org_id)
        return {"deleted": org_id}

    # --- spaces (org-shared project folders holding snapshots) -------------
    import re as _re

    # A space_id is a user-picked slug: lowercase letters/digits/dash/underscore.
    _SPACE_SLUG_RE = _re.compile(r"^[a-z0-9_-]+$")

    def _validate_space_slug(space_id: str, field: str = "spaceId") -> None:
        """Reject an empty/reserved/mis-shaped space slug (400).

        This single choke point guards every HTTP create path (``create_space``, the copy-to-org
        target, and ``save_snapshot``'s space field), so reserving the retired standalone-layout ids
        here (``building-validation`` / ``planning-validation`` / ``build-plan`` / ``<x>-plan``, plus
        the eval space) refuses re-creating that layout everywhere at once.
        """
        if not space_id or is_reserved_space_id(space_id):
            raise HTTPException(
                status_code=400, detail=f"{field} must be a non-reserved slug"
            )
        if not _SPACE_SLUG_RE.fullmatch(space_id):
            raise HTTPException(
                status_code=400,
                detail=f"{field} must be lowercase letters, digits, '-' or '_'",
            )

    @app.post("/spaces")
    def create_space(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Create an org-shared space (a project folder) in the active org.

        ``active_org`` proves membership; the space is org-shared (no owner) and
        readable by every member. No working-graph is created and nothing is
        activated — a space is just a folder that snapshots live in. 409 if a
        space by that id already exists in the org.
        """
        space_id = str(body.get("spaceId") or "").strip()
        name = body.get("name")
        name = str(name) if name is not None else None
        _validate_space_slug(space_id)
        try:
            spaces_store.create_space(org, space_id, name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"spaceId": space_id, "name": name}

    @app.get("/spaces")
    def list_spaces(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """List ALL spaces in the active org (ordered by id; eval space hidden)."""
        return {
            "spaces": [
                s
                for s in spaces_store.list_spaces(org)
                if s["space_id"] != RESERVED_EVAL_SPACE
            ]
        }

    # --- org storage purges (the data side of a delete) --------------------
    # These live here (not in a store) because they need the create_app-scoped
    # ``conn`` proxy and span the whole facts spine.
    def _purge_space_snapshots(org_id: str, space: str) -> None:
        """Hard-delete every snapshot in ``(org_id, space)`` and mounts of them.

        ``snapshots`` cascades its edges + claims via FK, so deleting the snapshot
        rows removes the whole sub-graph. ``mounted_snapshots`` has no such cascade
        and is deleted explicitly for the whole space. Working memory (``facts``) is
        NEVER touched.
        """
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM snapshots WHERE org_id=%s AND space=%s", (org_id, space)
            )
        mounted_store.unmount_space(org_id, space)

    def _purge_org_storage(org_id: str) -> None:
        """Hard-delete ALL of an org's tenant storage across every member, run just
        before the ``orgs`` row itself is removed.

        ``facts``/``snapshots``/``mounted_snapshots``/``api_keys`` have NO FK to
        ``orgs``, so dropping the org row would orphan them — they must be purged
        explicitly here (org-wide, every user + every space). Deleting the ``orgs``
        row afterwards (``orgs_store.delete_org``) cascades ``org_members`` +
        ``spaces``.
        """
        with conn.cursor() as cur:
            cur.execute("DELETE FROM facts WHERE org_id=%s", (org_id,))
            cur.execute("DELETE FROM snapshots WHERE org_id=%s", (org_id,))
            cur.execute("DELETE FROM mounted_snapshots WHERE org_id=%s", (org_id,))
            cur.execute("DELETE FROM api_keys WHERE org_id=%s", (org_id,))

    @app.delete("/spaces/{space_id}")
    def delete_space(
        space_id: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Permanently delete an org-shared space and ALL its snapshots.

        Any member of the org can delete a space (spaces are org-shared). Drops the
        ``spaces`` row plus every snapshot in the space (cascading edges/claims) and
        any mounts referencing those snapshots. Working memory (``facts``) is NEVER
        touched. 404 if the space is unknown. Destructive and irreversible.
        """
        if not spaces_store.exists(org, space_id):
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        _purge_space_snapshots(org, space_id)
        spaces_store.delete_space(org, space_id)
        return {"deleted": space_id}

    @app.post("/spaces/copy-to-org")
    def copy_space_to_org(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Copy ALL snapshots of a source space into a NEW space in another org.

        Cross-org sharing at the space (project) grain: every snapshot in the
        source ``space`` is copied into a freshly created ``targetSpace`` in
        ``targetOrg`` (which the caller must also belong to). The target space is
        created fresh: 409 if a space with that id already exists there (a copy
        never overwrites). 404 if the source space is unknown. Ids/embeddings are
        preserved verbatim. The space row is created first, so a copy that fails
        mid-flight leaves an empty space the caller can delete and retry.
        """
        space = str(body.get("space") or "").strip()
        target_org = str(body.get("targetOrg") or "").strip()
        target_space = str(body.get("targetSpace") or "").strip()
        name = body.get("name")
        name = str(name) if name is not None else None
        if not space or not spaces_store.exists(org, space):
            raise HTTPException(status_code=404, detail=f"unknown space {space!r}")
        if not target_org:
            raise HTTPException(status_code=400, detail="targetOrg required")
        _validate_space_slug(target_space, field="targetSpace")
        if not orgs_store.is_member(target_org, principal.sub):
            raise HTTPException(
                status_code=403, detail=f"not a member of org {target_org!r}"
            )
        try:
            spaces_store.create_space(target_org, target_space, name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        total = 0
        snapshots = 0
        for entry in live_graph(org, principal.sub).list_caches(space):
            snap = entry["snapshot"]
            dst = PostgresVectorGraph(
                conn, target_org, facts_table="snapshots",
                space=target_space, snapshot=snap,
            )
            total += dst.copy_snapshot_from(org, space, snap)
            snapshots += 1
        return {
            "targetOrg": target_org, "space": target_space,
            "snapshots": snapshots, "count": total,
        }

    @app.patch("/spaces/{space_id}")
    def rename_space(
        space_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Rename an org-shared space (display ``name`` only).

        Any member may rename a space (org-shared). Only ``name`` changes;
        ``space_id`` is the immutable key every snapshot is tenanted by, so the
        snapshots are untouched. 404 on unknown space.
        """
        if not spaces_store.exists(org, space_id):
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        spaces_store.rename_space(org, space_id, name)
        return {"spaceId": space_id, "name": name}

    # --- API keys (in-page key management, scoped to the active org) -------
    def _apikey_view(rec: dict[str, Any]) -> dict[str, Any]:
        """Public read model for one key (camelCase, never the raw key/hash)."""
        return {
            "id": rec["id"],
            "label": rec["label"],
            "userId": rec["user_id"],
            "createdAt": rec["created_at"],
            "lastUsedAt": rec["last_used_at"],
            "revoked": rec["revoked"],
        }

    @app.post("/apikeys")
    def create_apikey(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Mint a key for the active org, scoped to the caller's user id.

        The raw ``pxk_`` key is returned exactly once here and never persisted in
        clear; only its hash lives in the DB.
        """
        from knowledge.serve import apikeys

        label = body.get("label")
        label = str(label) if label is not None else None
        key_id, raw_key = apikeys.mint_key(conn, org, user_id=principal.sub, label=label)
        # Read back the freshly-minted row to source the canonical createdAt.
        rows = [k for k in apikeys.list_keys(conn, org) if k["id"] == key_id]
        created_at = rows[0]["created_at"] if rows else None
        return {
            "id": key_id,
            "key": raw_key,
            "label": label,
            "createdAt": created_at,
        }

    @app.get("/apikeys")
    def list_apikeys(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> list[dict[str, Any]]:
        """List the active org's keys (camelCase, never the raw key or hash)."""
        from knowledge.serve import apikeys

        return [_apikey_view(k) for k in apikeys.list_keys(conn, org)]

    @app.post("/apikeys/{key_id}/revoke")
    def revoke_apikey(
        key_id: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Revoke a key, but only if it belongs to the active org (else 404)."""
        from knowledge.serve import apikeys

        if not any(k["id"] == key_id for k in apikeys.list_keys(conn, org)):
            raise HTTPException(status_code=404, detail=f"unknown key {key_id}")
        apikeys.revoke_key(conn, key_id)
        return {"id": key_id, "revoked": True}

    # --- candidates (projection of the facts spine) ------------------------
    @app.get("/candidates")
    def list_candidates(
        state: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> list[dict[str, Any]]:
        return candidates_for(org, uid).list(state)

    @app.get("/candidates/{cid}")
    def get_candidate(
        cid: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        c = candidates_for(org, uid, target).get(cid)
        if c is None:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        return c

    @app.post("/candidates/{cid}/promote")
    def promote(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, uid).promote(cid, body.get("targetState"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        except PromotionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/candidates/{cid}/reject")
    def reject(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, uid).reject(cid, body.get("reason"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    @app.post("/facts/{fact_id}/outcome")
    def record_fact_outcome(
        fact_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Feed a downstream verification result back into a fact's trust.

        Body: ``{"success": bool}``. Increments the fact's success/failure count so
        retrieval's utility weighting demotes a repeatedly-failed fact and keeps a
        proven one — the outcome/trust feedback that makes the memory compound on
        what demonstrably worked rather than grow by volume alone.
        """
        success = body.get("success")
        if not isinstance(success, bool):
            raise HTTPException(
                status_code=400, detail="body must include a boolean 'success'"
            )
        # Honors the (space, snapshot) target like PATCH /candidates and /facts/by, so the
        # outcome lands on the snapshot-resident ticket the project's completeness derives
        # from. Surface any write error as a readable 500 detail rather than an empty body.
        try:
            graph_for(org, uid, target).record_outcome(fact_id, success=success)
        except ValueError as exc:  # e.g. a graph that refuses the write
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — never a bare/empty 500
            raise HTTPException(
                status_code=500, detail=f"record_outcome failed: {exc}"
            ) from exc
        return {"id": fact_id, "success": success}

    @app.post("/derivations")
    def record_derivation(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Attach a ``derived_from`` edge from one fact to each of its sources (H5).

        Body: ``{"factId": str, "sourceIds": [str, ...]}``. Links the fact to facts
        it was derived from so an invalidated source surfaces it as suspect via
        ``stale_derived`` / ``dependents``. This is the only direct way to create or
        repair a derivation edge between two *existing* facts — needed both to relink
        edges a merge destroyed and to link facts written via ``POST /candidates``.
        Idempotent (duplicate edges are ignored); self-edges are skipped.
        """
        fact_id = str(body.get("factId") or "").strip()
        source_ids = [str(s).strip() for s in (body.get("sourceIds") or []) if str(s).strip()]
        if not fact_id or not source_ids:
            raise HTTPException(
                status_code=400, detail="body must include 'factId' and non-empty 'sourceIds'"
            )
        g = live_graph(org, uid)
        if g.get_fact(fact_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown fact {fact_id}")
        missing = [s for s in source_ids if g.get_fact(s) is None]
        if missing:
            raise HTTPException(
                status_code=404, detail=f"unknown source fact(s): {', '.join(missing)}"
            )
        g.record_derivation(fact_id, source_ids)
        return {"factId": fact_id, "sourceIds": source_ids, "kind": "derived_from"}

    @app.post("/candidates")
    def create_candidate(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        _require_snapshot_for_check(body.get("category"), target)
        try:
            return candidates_for(org, uid, target).create(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.patch("/candidates/{cid}")
    def update_candidate(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        # A plain edit is a literal write (on_conflict defaults to "none"): only the
        # edited fact's fields change, no other fact is touched. "surface"/
        # "auto_resolve" are the explicit opt-ins mirroring /insights.
        on_conflict = str(body.get("onConflict") or "none").strip().lower()
        try:
            return candidates_for(org, uid, target).update(
                cid, body, on_conflict=on_conflict
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/candidates/{cid}")
    def delete_candidate(
        cid: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        try:
            candidates_for(org, uid).delete(cid)
            return {"deleted": cid}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    # --- contradictions ----------------------------------------------------
    @app.get("/contradictions")
    def contradictions(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> list[dict[str, Any]]:
        return candidates_for(org, uid, target).contradictions()

    @app.post("/contradictions/{pair_id}/resolve")
    def resolve(
        pair_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        facade = candidates_for(org, uid, target)
        custom_text = body.get("customText")
        if custom_text is not None and str(custom_text).strip():
            try:
                return facade.resolve_custom(pair_id, str(custom_text))
            except KeyError:
                raise HTTPException(status_code=404, detail=f"unknown contradiction {pair_id}")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        # H11: a cluster is settled by saying which members to keep. ``keep`` is
        # "all" (every member holds — a dismissed false positive), "none" (reject
        # all), or a list of fact ids to keep (reject the rest). This one primitive
        # subsumes keep-both, reject-all, and pick-a-winner.
        if "keep" not in body:
            raise HTTPException(
                status_code=400,
                detail="keep ('all', 'none', or a list of ids) or customText required",
            )
        try:
            return facade.resolve_keep(pair_id, body["keep"])
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown contradiction {pair_id}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # --- graph snapshot (the dashboard graph view) -------------------------
    @app.get("/graph")
    def graph(
        state: str = "active",
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """The live graph for the requester.

        ``state`` selects which facts to include:
        - ``"active"`` (default) — only active facts + edges, the exact rows
          retrieval reads (keeps the default view one-to-one with recall).
        - ``"all"`` — every fact regardless of lifecycle (active, proposed,
          decayed), with all edges between the included nodes.
        - a specific state (``proposed`` / ``active`` / ``decayed``) — only facts
          in that state, with edges between them.
        """
        g = graph_for(org, uid, target)
        if state == "active":
            return {"graph": graph_adapter.graph_from_facts(g.active_facts(), g.active_edges())}
        facts = g.all_facts(None if state == "all" else state)
        node_ids = {f.id for f in facts}
        edges = [e for e in g.all_edges() if e[0] in node_ids and e[1] in node_ids]
        return {"graph": graph_adapter.graph_from_facts(facts, edges)}

    @app.post("/graph/clear")
    def clear_graph(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Truncate the requester's working memory only (scoped to org_id + user_id).

        Deletes every fact/edge in the requester's private working memory
        ``(org, uid)``; other users' working memory and all org-shared snapshots
        are untouched. Implemented as a load of zero snapshots, which truncates
        working memory without refilling.
        """
        g = live_graph(org, uid)
        removed = len(g.all_facts())
        g.load_caches([])
        return {"cleared": removed}

    # --- snapshots (org-shared saved graph states inside a space) ----------
    @app.get("/snapshots")
    def list_snapshots(
        space: str = "",
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """List the org-shared snapshots saved in ``space``.

        Snapshots are org-shared, so every member sees the same list for a space.
        404 on an unknown space. Returns ``[{snapshot, count, createdAt}]``.
        """
        space = str(space or "").strip()
        if not space:
            raise HTTPException(status_code=400, detail="query param 'space' is required")
        if not spaces_store.exists(org, space):
            raise HTTPException(status_code=404, detail=f"unknown space {space!r}")
        entries = live_graph(org, principal.sub).list_caches(space)
        return {
            "space": space,
            "snapshots": [
                {
                    "snapshot": e["snapshot"],
                    "count": e["count"],
                    "createdAt": e["created_at"],
                }
                for e in entries
            ],
        }

    @app.post("/snapshots")
    def save_snapshot(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Dump the caller's working memory into snapshot ``(space, snapshot)``.

        The space is org-shared; it is registered on first write so the space list
        picks it up. An existing snapshot of the same ``(space, snapshot)`` is
        overwritten. Working memory is unchanged (this is a copy, not a move).
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        _validate_space_slug(space, field="space")
        if not snapshot:
            raise HTTPException(status_code=400, detail="snapshot required")
        spaces_store.ensure_space(org, space)
        try:
            count = live_graph(org, uid).save_cache(space, snapshot)
        except SnapshotKindError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"space": space, "snapshot": snapshot, "count": count}

    @app.post("/snapshots/load")
    def load_snapshot(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Load an org-shared snapshot ``(space, snapshot)`` into working memory.

        ``mode="replace"`` (default) truncates the caller's working memory then
        inserts the snapshot. ``mode="add"`` additively merges the snapshot into
        working memory (replacing only nodes the snapshot shares by id), keeping
        other working-memory facts.
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        mode = str(body.get("mode") or "replace").strip().lower()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        if mode not in ("add", "replace"):
            raise HTTPException(status_code=400, detail="mode must be 'add' or 'replace'")
        g = live_graph(org, uid)
        if g.cache_count(space, snapshot) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"unknown snapshot {snapshot!r} in space {space!r}",
            )
        ref = (space, snapshot)
        loaded = g.merge_caches_into_live([ref]) if mode == "add" else g.load_caches([ref])
        return {"loaded": loaded, "mode": mode}

    @app.delete("/snapshots")
    def delete_snapshot(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Delete an org-shared snapshot ``(space, snapshot)`` and unmount it everywhere.

        Drops the snapshot's facts/edges/claims (cascade) plus every viewer's mount
        of it, so no dangling mount can reference a deleted snapshot. The space
        registry row and all working memory are untouched.
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        live_graph(org, uid).delete_cache(space, snapshot)
        mounted_store.unmount_all(org, space, snapshot)
        return {"space": space, "snapshot": snapshot, "deleted": True}

    @app.post("/snapshots/copy-to-org")
    def copy_snapshot_to_org(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Copy one org-shared snapshot into a snapshot in another org.

        Reads ``(space, snapshot)`` in the active org and writes it into
        ``(targetSpace, targetSnapshot)`` in ``targetOrg`` (which the caller must
        also belong to). ``targetSnapshot`` defaults to the source snapshot name;
        the target space is registered on write. Ids/embeddings are preserved
        verbatim. 404 if the source snapshot is empty/unknown; 409 if the target
        snapshot already exists (a copy never overwrites).
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        target_org = str(body.get("targetOrg") or "").strip()
        target_space = str(body.get("targetSpace") or "").strip()
        target_snapshot = str(body.get("targetSnapshot") or snapshot).strip()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        if not target_org:
            raise HTTPException(status_code=400, detail="targetOrg required")
        _validate_space_slug(target_space, field="targetSpace")
        if not target_snapshot:
            raise HTTPException(status_code=400, detail="targetSnapshot required")
        if not orgs_store.is_member(target_org, principal.sub):
            raise HTTPException(
                status_code=403, detail=f"not a member of org {target_org!r}"
            )
        if live_graph(org, principal.sub).cache_count(space, snapshot) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"unknown snapshot {snapshot!r} in space {space!r}",
            )
        # Existence check runs on a LIVE graph in the target org (cache_count reads
        # the snapshots table for its org); a snapshot-bound graph rejects it.
        if live_graph(target_org, principal.sub).cache_count(target_space, target_snapshot) != 0:
            raise HTTPException(
                status_code=409,
                detail=f"snapshot {target_snapshot!r} already exists in space "
                f"{target_space!r} of org {target_org!r}",
            )
        dst = PostgresVectorGraph(
            conn, target_org, facts_table="snapshots",
            space=target_space, snapshot=target_snapshot,
        )
        spaces_store.ensure_space(target_org, target_space)
        try:
            count = dst.copy_snapshot_from(org, space, snapshot)
        except SnapshotKindError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "targetOrg": target_org,
            "space": target_space,
            "snapshot": target_snapshot,
            "count": count,
        }

    @app.patch("/snapshots")
    def rename_snapshot(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Rename a snapshot within its space (re-keys, not relabels).

        The snapshot ``(space, snapshot)`` is renamed to ``(space, newSnapshot)``:
        the new name must be free (409 on collision) and the rename rewrites the key
        across the snapshot's facts/edges/claims. Any viewer's mount of the old
        snapshot is repointed to ``newSnapshot`` so a mounted overlay keeps reading.
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        new_snapshot = str(body.get("newSnapshot") or "").strip()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        if not new_snapshot:
            raise HTTPException(status_code=400, detail="newSnapshot required")
        g = live_graph(org, uid)
        if g.cache_count(space, snapshot) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"unknown snapshot {snapshot!r} in space {space!r}",
            )
        if new_snapshot != snapshot and g.cache_count(space, new_snapshot) > 0:
            raise HTTPException(
                status_code=409,
                detail=f"snapshot {new_snapshot!r} already exists in space {space!r}",
            )
        if new_snapshot != snapshot:
            g.rename_cache(space, snapshot, new_snapshot)
            mounted_store.repoint(org, space, snapshot, new_snapshot)
        return {"space": space, "snapshot": new_snapshot}

    # --- mounted snapshots (read-only overlay selection) -------------------
    def _snapshot_probe(org: str, space: str, snapshot: str) -> PostgresVectorGraph:
        """A snapshot-bound graph for existence/count probes (no policy needed)."""
        return PostgresVectorGraph(
            conn, org, facts_table="snapshots", space=space, snapshot=snapshot
        )

    def _validate_mount_target(org: str, space: str, snapshot: str) -> None:
        """Ensure the org-shared snapshot ``(space, snapshot)`` exists (has rows)."""
        if _snapshot_probe(org, space, snapshot).cache_count(space, snapshot) == 0:
            raise HTTPException(
                status_code=404,
                detail=f"unknown snapshot {snapshot!r} in space {space!r}",
            )

    @app.get("/mounts")
    def list_mounts(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """List the caller's mounted snapshots (read-only retrieval overlays).

        Each mount adds an org-shared snapshot's facts to what the caller's
        working-memory retrieval reads, without merging them into working memory and
        without being carried over on a dump. Returns ``[{space, snapshot, count}]``.
        """
        return {
            "mounts": [
                {
                    "space": m["space"],
                    "snapshot": m["snapshot"],
                    "count": _snapshot_probe(org, m["space"], m["snapshot"]).cache_count(
                        m["space"], m["snapshot"]
                    ),
                }
                for m in mounted_store.list(org, principal.sub)
            ]
        }

    @app.post("/mounts")
    def add_mount(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Mount an org-shared snapshot ``(space, snapshot)`` as a read overlay."""
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        _validate_mount_target(org, space, snapshot)
        mounted_store.mount(org, principal.sub, space, snapshot)
        return {"space": space, "snapshot": snapshot, "mounted": True}

    @app.delete("/mounts")
    def remove_mount(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Unmount an org-shared snapshot (no-op if it was not mounted)."""
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        if not space or not snapshot:
            raise HTTPException(status_code=400, detail="space and snapshot required")
        mounted_store.unmount(org, principal.sub, space, snapshot)
        return {"space": space, "snapshot": snapshot, "mounted": False}

    # --- skill sharing: browse another member's facts + fold them in -------
    def _fact_brief(fact: Any) -> dict[str, Any]:
        """Compact read model for the browse view (one fact in a source)."""
        return {
            "id": fact.id,
            "text": fact.text,
            "scope": fact.scope,
            "clusterLabel": fact.cluster_label,
            "state": fact.state,
        }

    def _group_facts(facts: list[Any]) -> list[dict[str, Any]]:
        """Group facts into folders for the browse view.

        Grouping key is the fact ``scope`` (the folder concept the dashboard
        already groups by today), falling back to a stable "ungrouped" bucket.
        """
        groups: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for fact in facts:
            key = fact.scope or "ungrouped"
            if key not in groups:
                groups[key] = {"key": key, "label": key, "facts": []}
                order.append(key)
            groups[key]["facts"].append(_fact_brief(fact))
        return [groups[k] for k in order]

    @app.get("/org/sources")
    def org_sources(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """List browsable sources in the org: every space + its snapshots.

        Spaces are org-shared, so any member may browse any space's saved
        snapshots. Each snapshot carries its node count. The reserved eval space is
        hidden. Returns ``[{space, name, snapshots:[{snapshot, count}]}]``.
        """
        graph = live_graph(org, principal.sub)
        sources: list[dict[str, Any]] = []
        for s in spaces_store.list_spaces(org):
            space = s["space_id"]
            if space == RESERVED_EVAL_SPACE:
                continue
            snapshots = [
                {"snapshot": e["snapshot"], "count": e["count"]}
                for e in graph.list_caches(space)
            ]
            sources.append(
                {"space": space, "name": s["name"], "snapshots": snapshots}
            )
        return {"sources": sources}

    @app.get("/spaces/{space}/snapshots/{snapshot}/facts")
    def space_snapshot_facts(
        space: str,
        snapshot: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Browse one org-shared snapshot's facts, grouped by folder (``scope``).

        ``active_org`` already proved the caller is a member of ``org``; snapshots
        are org-shared, so any ``(space, snapshot)`` in the org is a valid target.
        The org-scoped reader pins ``org_id`` so no other org's rows are reachable.
        404 if the snapshot holds no facts.
        """
        reader = OrgSourceReader(conn, org, space=space, snapshot=snapshot)
        facts = reader.all_facts()
        if not facts:
            raise HTTPException(
                status_code=404,
                detail=f"unknown snapshot {snapshot!r} in space {space!r}",
            )
        return {
            "space": space,
            "snapshot": snapshot,
            "groups": _group_facts(facts),
        }

    @app.post("/fold-in")
    @limiter.limit(LLM_RATE_LIMIT)
    def fold_in(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Copy selected snapshot facts from a space into the caller's working memory.

        Each selected fact is written through a distillation-free policy
        (``Deduper`` then ``ConflictFlagger`` — no ``Redactor``, no LLM
        re-distillation): already-atomic facts are deduped against the caller's
        working memory and conflicts are flagged, never silently overwritten. Copies
        land ``active`` (an explicit user action) and carry provenance in ``meta``.
        Edges whose both endpoints are in the selection are carried with the
        copy, remapped to the caller's new fact ids.

        ``mode="replace"`` truncates the caller's own working memory before copying
        (so the caller ends up holding exactly the folded facts); ``mode="add"``
        (default) merges the selection into the existing working memory.
        """
        space = str(body.get("space") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        mode = str(body.get("mode") or "add").strip().lower()
        fact_ids = body.get("factIds") or []
        if not space:
            raise HTTPException(status_code=400, detail="space required")
        if not snapshot:
            raise HTTPException(status_code=400, detail="snapshot required")
        if mode not in ("add", "replace"):
            raise HTTPException(status_code=400, detail="mode must be 'add' or 'replace'")
        if not isinstance(fact_ids, list) or not fact_ids:
            raise HTTPException(status_code=400, detail="factIds must be a non-empty list")
        reader = OrgSourceReader(conn, org, space=space, snapshot=snapshot)
        src_facts = reader.get_facts([str(i) for i in fact_ids])
        if not src_facts:
            raise HTTPException(status_code=404, detail="no matching facts in source")

        # mode=replace: truncate the caller's working memory before copying (same
        # zero-snapshot-load truncation as /graph/clear). Dedup/conflict are then
        # moot on the now-empty graph, but the policy stays identical.
        if mode == "replace":
            live_graph(org, uid).load_caches([])

        # Distillation-free fold-in policy: dedup the already-atomic facts, then
        # the structural claim path (extract -> detect) flags genuine value
        # conflicts as contradiction edges. No Redactor (source facts are already
        # vetted) and no LLM re-distillation of the text itself.
        base_llm = OpenRouterLlm()
        graph = PostgresVectorGraph(
            conn,
            org,
            uid,
            policy=[
                Deduper(),
                ClaimExtractor(judge=ClaimExtractionJudge(llm=base_llm)),
                ClaimConflictDetector(judge=ClaimValueJudge(llm=base_llm)),
            ],
        )
        before_edges = set(graph.all_edges("contradiction"))
        existing_ids = {f.id for f in graph.all_facts()}
        id_map: dict[str, str] = {}
        deduped = 0
        for fact in src_facts:
            meta = dict(fact.meta or {})
            meta["foldedFrom"] = {"space": space, "snapshot": snapshot}
            meta["foldedFromFactId"] = fact.id
            if fact.cluster_label:
                meta.setdefault("sourceClusterLabel", fact.cluster_label)
            new_id = graph.write(
                fact.text,
                state="active",
                source=fact.source,
                scope=fact.scope,
                category=fact.category,
                meta=meta,
            )
            if new_id is None:
                continue
            # A returned id that already existed means dedup merged into it; a
            # fresh id is a genuinely new fact in the caller's graph.
            if new_id in existing_ids:
                deduped += 1
            else:
                existing_ids.add(new_id)
            id_map[fact.id] = new_id

        # Carry edges whose both endpoints came along, remapped to new ids.
        for src_id, dst_id, kind in reader.edges_among(list(id_map.keys())):
            if src_id in id_map and dst_id in id_map:
                graph.add_edge(id_map[src_id], id_map[dst_id], kind)

        new_ids = set(id_map.values())
        after_edges = set(graph.all_edges("contradiction"))
        new_conflicts = [
            {"newId": s, "rivalId": d}
            for (s, d, _k) in (after_edges - before_edges)
            if s in new_ids
        ]
        return {
            "folded": len(id_map) - deduped,
            "deduped": deduped,
            "conflicts": new_conflicts,
            "mode": mode,
        }

    # --- eval cache (per case) + loading into the live graph ---------------
    @app.get("/evals/cached")
    def cached_eval_cases(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Eval case ids with cached data + node counts (drives the UI status dots).

        ``counts`` maps each cached case id to how many nodes its cached
        distillation holds — what the dashboard shows inside the green dot.

        Eval fixtures live in ``snapshots`` under the reserved ``__evals__`` space,
        one snapshot per case id (org-scoped, not user-partitioned).
        """
        entries = live_graph(org, uid).list_caches(RESERVED_EVAL_SPACE)
        ids = [e["snapshot"] for e in entries]
        counts = {e["snapshot"]: int(e.get("count", 0)) for e in entries}
        return {"cached": ids, "counts": counts}

    def _selected_case_ids(body: dict[str, Any]) -> list[str]:
        """Resolve the requested scopes/caseIds into concrete eval case ids."""
        config = PipelineConfig.from_body(body)
        case_ids = case_ids_for(list(config.scopes), list(config.case_ids))
        if not case_ids:
            raise HTTPException(status_code=400, detail="no eval cases selected")
        return case_ids

    def _ensure_cached(
        org: str, sub: str, case_ids: list[str], *, distill: bool, force: bool
    ) -> tuple[list[str], list[str]]:
        """Make sure each case has a cache entry; (re)generate misses (or all, if force).

        Returns (regenerated_case_ids, from_cache_case_ids). Writes only the eval
        cache (``snapshots`` under the reserved ``__evals__`` space, snapshot=case
        id); working memory is untouched here.
        """
        live = live_graph(org, sub)
        regenerated: list[str] = []
        from_cache: list[str] = []
        for cid in case_ids:
            if not force and live.cache_count(RESERVED_EVAL_SPACE, cid) > 0:
                from_cache.append(cid)
                continue
            seeds = distill_case(cid, distill=distill)
            eval_graph = PostgresVectorGraph(
                conn,
                org,
                facts_table="snapshots",
                space=RESERVED_EVAL_SPACE,
                snapshot=cid,
                policy=default_write_policy(),
            )
            eval_graph.wipe_cache()  # clean slate for a (re)generated case
            for seed in seeds:
                eval_graph.write(
                    seed.text,
                    state=seed.state,
                    source=seed.source,
                    scope=seed.scope,
                    category=seed.category,
                )
            # Define-pass: tag the cached facts with topic clusters so the graph
            # view can collapse them into labeled super-nodes. Persisted into the
            # cache, so a later /evals/load carries the labels into the live graph
            # verbatim (no re-clustering). Best-effort: a flat graph still loads if
            # embeddings/clustering deps are unavailable.
            try:
                eval_graph.recluster()
            except Exception:
                pass
            regenerated.append(cid)
        return regenerated, from_cache

    @app.post("/evals/regenerate")
    @limiter.limit(LLM_RATE_LIMIT)
    def regenerate_evals(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Run pipeline: create/update the eval CACHE only — never touch the graph.

        Force-upserts the cache for the selected cases (distillation by default).
        The live graph is not changed; use ``/evals/load`` to put cached eval data
        into the graph.
        """
        body = body or {}
        try:
            case_ids = _selected_case_ids(body)
            regenerated, from_cache = _ensure_cached(
                org,
                uid,
                case_ids,
                distill=bool(body.get("distill", True)),
                force=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RegenerateUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return {
            "cases_cached": len(regenerated),
            "regenerated": regenerated,
            "from_cache": from_cache,
            "ran_at": _now(),
        }

    @app.post("/evals/load")
    @limiter.limit(LLM_RATE_LIMIT)
    def load_evals(
        request: Request,
        body: dict[str, Any] | None = Body(default=None),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Put cached eval data into the live graph.

        ``mode="add"`` (default) additively merges each eval's nodes into the
        graph (replacing only that eval's own nodes if already present), keeping
        other live facts. ``mode="replace"`` truncates the whole live graph first,
        then inserts the selected eval(s). Cache misses are (re)generated first.
        """
        body = body or {}
        mode = str(body.get("mode") or "add").strip().lower()
        if mode not in ("add", "replace"):
            raise HTTPException(status_code=400, detail="mode must be 'add' or 'replace'")
        try:
            case_ids = _selected_case_ids(body)
            regenerated, from_cache = _ensure_cached(
                org,
                uid,
                case_ids,
                distill=bool(body.get("distill", False)),
                force=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RegenerateUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        refs = [(RESERVED_EVAL_SPACE, cid) for cid in case_ids]
        live = live_graph(org, uid)
        if mode == "replace":
            facts_in_graph = live.load_caches(refs)
        else:
            facts_in_graph = live.merge_caches_into_live(refs)
        return {
            "mode": mode,
            "regenerated": regenerated,
            "from_cache": from_cache,
            "candidates_inserted": facts_in_graph,
            "ran_at": _now(),
        }

    @app.get("/evals/scopes")
    def list_eval_scopes(
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        """List selectable case folders (scopes) and their case counts."""
        from knowledge.serve.eval_runner import OVERRIDE_FIELDS, VALID_BACKENDS, list_scopes

        return {
            "scopes": list_scopes(),
            "backends": list(VALID_BACKENDS),
            "overrideFields": {k: (list(v) if v else None) for k, v in OVERRIDE_FIELDS.items()},
        }

    # --- insights + context (MCP read/write path) --------------------------
    @app.post("/insights")
    @limiter.limit(LLM_RATE_LIMIT)
    def add_insight(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Ingest a fully-approved insight into the live ``facts`` store.

        ``onConflict`` selects how a detected contradiction is handled:

        * ``"auto_resolve"`` (default, back-compat) — non-destructive resolution:
          the contradicting add lands as a fresh ``active`` fact and each conflicting
          fact is rejected (its text preserved) and linked via a ``contradicted_by``
          edge. The newest approved truth silently wins.
        * ``"surface"`` — retain BOTH facts and create a *pending* contradiction:
          the conflict is flagged (a ``contradiction`` edge) rather than resolved, so
          it shows up in ``GET /contradictions`` for a human/agent to adjudicate with
          ``POST /contradictions/{id}/resolve``. Neither side is rejected; FR-005 only
          demotes the newcomer to ``proposed`` so the pair is never both ``active``.

        The in-chat confirmation is the human gate, so the insight enters ``active``
        at full credibility (subject to the FR-005 demotion under ``surface``).

        ``raw`` (bool, default False) is the fast lane for trusted bulk inserts: it
        keeps the cheap regex ``Redactor`` (secrets are still scrubbed) but SKIPS the
        ``Deduper`` and the LLM-backed conflict/claim pipeline, so the per-item LLM
        round-trips that make large batches time out are avoided. ``onConflict`` is
        ignored when ``raw`` is set (there is no conflict step to configure).
        """
        insight = (body.get("insight") or "").strip()
        if not insight:
            raise HTTPException(status_code=400, detail="insight required")
        # Body-size cap (mirrors /ingest/session): reject oversized input before any
        # LLM call. 128 KB matches the session-ingest ceiling.
        if len(insight.encode("utf-8")) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="insight too large")
        _require_snapshot_for_check(body.get("category"), target)
        # Episodic (H4): a decision log entry bypasses the semantic pipeline — stored
        # whole, append-only, immutable (no distill/dedup/contradiction) and out of
        # semantic recall. Routed to the store-only producer.
        if (body.get("category") or "").strip() == EPISODIC_CATEGORY:
            return _record_episode(graph_for(org, uid, target), insight, body)
        on_conflict = str(body.get("onConflict") or "auto_resolve").strip().lower()
        if on_conflict not in ("auto_resolve", "surface"):
            raise HTTPException(
                status_code=400,
                detail="onConflict must be 'auto_resolve' or 'surface'",
            )
        # H12: writer-supplied metadata is persisted onto the resulting fact and
        # returned on reads. ``source``/``scope``/``category`` go into their facts
        # columns; ``meta`` (jsonb) carries arbitrary writer fields. A value the
        # writer sets wins over any ingestion-derived default (see Ingestor.ingest).
        meta = body.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise HTTPException(status_code=400, detail="meta must be an object")
        # A validation/planning CHECK is a declarative gate keyed on meta.check_id, NOT a
        # knowledge assertion — it must never be text-deduped or reconciled (that silently
        # merged distinct checks and dropped their `run`). Route to the identity-keyed upsert
        # on a REDACT-ONLY graph (no Deduper, no conflict/claim pipeline), so onConflict does
        # not apply. The section guard above already required a (space, snapshot) target.
        if (body.get("category") or "").strip() == CHECK_CATEGORY:
            return _check_upsert(
                graph_for(org, uid, target, policy=[Redactor()]),
                insight=insight,
                source=body.get("source"),
                scope=body.get("scope"),
                meta=meta,
            )
        # raw: trusted fast lane — redact only, no Deduper / LLM conflict steps.
        # auto_resolve: ConflictOverwriter turns a confirmed contradiction into a
        # force-overwrite (loser rejected). surface: the structural+semantic detector
        # pipeline only *flags* the clash, so the store persists a pending
        # contradiction edge instead of rejecting a side.
        # Only a literal JSON ``true`` enables the fast lane; anything else (incl.
        # the string "false") falls back to the SAFER reconciled write, since raw
        # reduces write-time safety (skips dedup + the conflict/claim policy).
        raw = body.get("raw", False) is True
        policy = [Redactor()] if raw else _insight_write_policy(on_conflict)
        graph = graph_for(org, uid, target, policy=policy)
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        return _write_insight(
            graph,
            ingestor,
            insight=insight,
            source=body.get("source"),
            scope=body.get("scope"),
            category=body.get("category"),
            meta=meta,
            on_conflict=on_conflict,
            derived_from=body.get("derivedFrom"),
        )

    @app.post("/insights/batch")
    def add_insights_batch(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Bulk-write many fully-approved insights in one synchronous round-trip (H8).

        Body: ``{"insights": [{"insight": str, "source"?, "scope"?, "category"?,
        "meta"?, "derivedFrom"?}, ...], "onConflict": "auto_resolve"|"surface"}``.
        Each item is the same shape ``POST /insights`` accepts; ``onConflict`` is
        batch-level (one policy for the whole call). This is the **shaped-fact**
        lane — no LLM distillation — so it stays fast and lower-loss; for raw
        documents that need distilling use ``POST /ingest``.

        Why it exists (gap H8): the local loop accumulates many learnings per
        session. Writing them one-MCP-call-at-a-time pays N HTTP/auth/graph
        setups and, if fired concurrently, can overwhelm the conflict-checked
        write path. This endpoint does ONE round-trip and builds the policy graph
        ONCE. The conflict-checked semantic insights are **decided in parallel**
        (each worker on its own connection) and **committed serially** with a
        same-batch reconciliation pass, so dedup is preserved while the costly
        recall+judge work overlaps (see ``knowledge/serve/batch_writer``).

        Returns ``{"results": [...], "count": int}`` — one result per input item,
        in order, each carrying ``id``/``action``/``contradictionsSurfaced`` and a
        ``retrievable`` flag confirming the fact is immediately read-back-able
        (read-your-writes), so the caller doesn't have to poll. Each result also
        carries ``ok`` and, on a per-item failure, an ``error`` string — one bad
        item fails cleanly without aborting the rest of the batch.

        ``raw`` (bool, default False) is the batch-level fast lane: it keeps the
        cheap regex ``Redactor`` (secrets still scrubbed) but SKIPS the ``Deduper``
        and the LLM conflict/claim steps for every item, so a large trusted bulk
        insert (e.g. 71 items) that would otherwise time out on per-item LLM
        conflict checks lands quickly. ``onConflict`` is ignored when ``raw`` is set.
        The embedding prefetch below still runs, so the write stays cheap on the
        embedder too.
        """
        insights = body.get("insights")
        if not isinstance(insights, list) or not insights:
            raise HTTPException(status_code=400, detail="insights must be a non-empty list")
        # The batch lane is the bulk personal-knowledge (working-memory) path; a CHECK must be
        # authored into a section snapshot (see _require_snapshot_for_check), which this endpoint
        # does not target — so refuse a batched check rather than let it land invisibly. Author
        # checks one at a time via POST /insights with X-Praxis-Space/Snapshot.
        if any(
            isinstance(it, dict) and str(it.get("category") or "").strip() == CHECK_CATEGORY
            for it in insights
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "category='check' facts cannot be batch-written to working memory — author "
                    "each check via POST /insights targeting a 'building-validation'/'planning-"
                    "validation' snapshot (X-Praxis-Space + X-Praxis-Snapshot)."
                ),
            )
        on_conflict = str(body.get("onConflict") or "auto_resolve").strip().lower()
        if on_conflict not in ("auto_resolve", "surface"):
            raise HTTPException(
                status_code=400, detail="onConflict must be 'auto_resolve' or 'surface'"
            )
        # One policy graph for the whole batch (the throughput win). It is the serial
        # "base": it commits each decision and is the connection a same-batch
        # re-decide runs on. Worker graphs are cloned from it onto their own
        # connections inside the batch writer.
        # raw skips dedup + LLM conflict for the whole batch (redact-only fast lane).
        # Only a literal JSON ``true`` enables the fast lane; anything else (incl.
        # the string "false") falls back to the SAFER reconciled write, since raw
        # reduces write-time safety (skips dedup + the conflict/claim policy).
        raw = body.get("raw", False) is True
        policy = [Redactor()] if raw else _insight_write_policy(on_conflict)
        graph = PostgresVectorGraph(conn, org, uid, policy=policy)
        # Memoize + pre-embed every item's text in ONE batch call up front so the
        # parallel decides (and any re-decide) resolve embeddings from the memo
        # instead of paying N round-trips. The memo is lock-guarded so the workers
        # share it safely; any text not pre-warmed falls through to the inner embedder.
        graph.embedder = MemoizingEmbedder(graph.embedder)
        graph.embedder.prefetch(
            [
                (item.get("insight") or "").strip()
                for item in insights
                if isinstance(item, dict)
            ]
        )

        # Pass 1: validate + split. Episodic items are store-only (no recall, and
        # semantic recall excludes them), so they neither see nor affect the
        # semantic facts — handle them serially inline; collect the rest for the
        # parallel decide. ``results[i]`` is filled in input order either way.
        results: list[dict[str, Any] | None] = [None] * len(insights)
        to_decide: list[dict[str, Any]] = []
        decide_index: list[int] = []
        for i, item in enumerate(insights):
            if not isinstance(item, dict):
                results[i] = {"ok": False, "error": "each insight must be an object", "index": i}
                continue
            text = (item.get("insight") or "").strip()
            if not text:
                results[i] = {"ok": False, "error": "insight required", "index": i}
                continue
            item_meta = item.get("meta")
            if item_meta is not None and not isinstance(item_meta, dict):
                results[i] = {"ok": False, "error": "meta must be an object", "index": i}
                continue
            if (item.get("category") or "").strip() == EPISODIC_CATEGORY:
                try:
                    res = _record_episode(graph, text, item)
                    res["ok"] = True
                except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
                    res = {"ok": False, "error": str(exc), "index": i}
                results[i] = res
                continue
            to_decide.append(
                {
                    "text": text,
                    "state": "active",  # human-gated add -> live knowledge
                    "source": item.get("source"),
                    "scope": item.get("scope"),
                    "category": item.get("category"),
                    "meta": item_meta,
                    "derived_from": item.get("derivedFrom"),
                }
            )
            decide_index.append(i)

        # Pass 2: parallel-decide / serial-commit the semantic insights. With an
        # explicit single connection (tests) there are no independent connections,
        # so fall back to one worker on the shared connection (still correct: all
        # items decide before any commit, and the reconciliation pass dedups
        # same-batch collisions). Don't close a connection we don't own.
        if to_decide:
            if make_worker_conn is not None:
                connect_fn: Callable[[], Any] = make_worker_conn
                close_fn: Callable[[Any], None] | None = None
                workers = max(1, int(os.getenv(
                    "PRAXIS_BATCH_WRITE_WORKERS", str(batch_writer.DEFAULT_MAX_WORKERS)
                )))
            else:
                connect_fn = lambda: conn  # noqa: E731 - shared explicit connection
                close_fn = lambda _c: None  # noqa: E731 - not ours to close
                workers = 1
            outcomes = batch_writer.write_insights(
                to_decide,
                base=graph,
                connect=connect_fn,
                close=close_fn,
                policy_factory=lambda: _insight_write_policy(on_conflict),
                max_workers=workers,
            )
            for idx, outcome in zip(decide_index, outcomes):
                results[idx] = _batch_result_from_outcome(outcome, on_conflict, idx)

        return {"results": results, "count": len(results)}

    @app.post("/ingest")
    @limiter.limit(LLM_RATE_LIMIT)
    def ingest_documents(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Batch-ingest raw documents through the tenant's distillation pipeline.

        Body: ``{"documents": [{"text": str, "source": str|null}],
        "state": "active"|"proposed", "onConflict": "auto_resolve"|"surface"}``
        (state defaults to "active", onConflict to "auto_resolve"). Each
        document is run through the same ingestor that distills raw text into
        facts (``build_trio`` over the tenant's live graph), at the given state —
        this is the pipeline path, NOT the no-pipeline ``/candidates`` insert.

        ``onConflict`` mirrors ``POST /insights``: ``"auto_resolve"`` (default)
        rejects the losing side of a detected clash; ``"surface"`` keeps both facts
        and records a *pending* contradiction (visible in ``GET /contradictions``).

        Returns ``{"results": [{"id": str|null, "action": str}], "count": int}``;
        one result per input document. ``id`` is the top matching fact after that
        document's distillation (best-effort), ``action`` is ``"ingested"``.
        """
        documents = body.get("documents")
        if not isinstance(documents, list) or not documents:
            raise HTTPException(status_code=400, detail="documents must be a non-empty list")
        # Body-size cap (mirrors /ingest/session): reject oversized input — the sum of
        # the documents' text — before any LLM call. 128 KB matches that ceiling.
        total_bytes = sum(
            len(str((doc or {}).get("text") or "").encode("utf-8"))
            for doc in documents
            if isinstance(doc, dict)
        )
        if total_bytes > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="documents too large")
        state = str(body.get("state") or "active").strip().lower()
        if state not in ("active", "proposed"):
            raise HTTPException(status_code=400, detail="state must be 'active' or 'proposed'")
        on_conflict = str(body.get("onConflict") or "auto_resolve").strip().lower()
        if on_conflict not in ("auto_resolve", "surface"):
            raise HTTPException(
                status_code=400,
                detail="onConflict must be 'auto_resolve' or 'surface'",
            )

        from knowledge.injestion.dump_ingest import ingest_dump

        ingest_llm = OpenRouterLlm()
        # Exact-dedup-only write policy: ingest_dump owns dedup AND conflict
        # resolution via slot-granular claims (the distiller emits a (subject,
        # attribute, value) per fact whose subject carries every discriminating
        # qualifier, so different table rows are different slots and are never
        # false-flagged). The write policy must NOT also run a conflict step or it
        # would re-introduce the coarse-slot false positives.
        graph = graph_for(org, uid, target, policy=[Redactor(), Deduper()])
        # H5: body-level derivation provenance — each distilled fact links back to
        # these source ids via a ``derived_from`` edge.
        derived_from = body.get("derivedFrom")
        results: list[dict[str, Any]] = []
        for doc in documents:
            if not isinstance(doc, dict):
                raise HTTPException(status_code=400, detail="each document must be an object")
            text = (doc.get("text") or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="each document needs non-empty text")
            source = doc.get("source")
            # H12: per-document writer metadata is persisted onto every fact the
            # document distills into.
            scope = doc.get("scope")
            category = doc.get("category")
            meta = doc.get("meta")
            if meta is not None and not isinstance(meta, dict):
                raise HTTPException(status_code=400, detail="meta must be an object")
            summary = ingest_dump(
                graph,
                ingest_llm,
                text,
                state=state,
                source=str(source) if source else None,
                scope=str(scope) if scope else None,
                category=str(category) if category else None,
                meta=meta,
                on_conflict=on_conflict,
            )
            # Best-effort provenance back to the caller: the top fact the just-
            # ingested text now matches (ids are per-fact, a doc distills to many).
            hits = graph.search(text, top_k=1, state=None)
            top_id = hits[0].fact.id if hits else None
            if derived_from and top_id is not None:
                graph.record_derivation(top_id, [str(s) for s in derived_from])
            results.append({
                "id": top_id,
                "action": "ingested",
                "facts": summary["facts"],
                "merged": summary["merged"],
                "conflicts": summary["conflicts"],
                "surfaced": summary.get("surfaced", 0),
            })
        return {"results": results, "count": len(results)}

    @app.post("/ingest/session")
    @limiter.limit(LLM_RATE_LIMIT)
    def ingest_session(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Distill one solved-problem session narrative into ``proposed`` candidates.

        Body: ``{"narrative": str, "source": str|null}``. Runs the structured
        ``SessionIngestor`` distiller (NOT ``ingest_dump``, NOT ``/insights``' active
        path) over the narrative and writes each insight as a ``proposed`` candidate
        through the tenant graph's redact+dedup policy, preserving the insight's
        ``scope``/``category`` (which the base ``Ingestor.ingest`` would drop).

        ``source`` is validated to ``session/<id>`` when supplied and auto-generated
        server-side (not caller-spoofable) when omitted. Oversized narratives are
        rejected (413) before any LLM call. Returns
        ``{"source", "count", "candidates": [{"id","scope","category"}]}``.
        """
        import re
        import uuid

        from knowledge.injestion.injestor_variants.session_injestor import (
            SessionIngestor,
        )

        source_re = re.compile(r"session/[A-Za-z0-9_-]{1,64}")

        narrative = (body.get("narrative") or "").strip()
        if not narrative:
            raise HTTPException(status_code=400, detail="narrative must be non-empty")
        if len(narrative.encode("utf-8")) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="narrative too large")
        source = body.get("source")
        if source is not None:
            source = str(source)
            if not source_re.fullmatch(source):
                raise HTTPException(
                    status_code=400, detail="source must match session/<id>"
                )
        else:
            source = f"session/{uuid.uuid4().hex[:16]}"

        # Scrub secrets BEFORE the narrative reaches the third-party LLM — the write
        # policy's Redactor only scrubs the stored fact text, not the prompt. Reuses
        # the exact Redactor patterns (no duplication).
        from knowledge.knowledge_graph.write_policy.write_step_variants.redactor import (
            redact_text,
        )

        narrative = redact_text(narrative)

        # Distill via SessionIngestor directly (build_trio hardwires PromptIngestor and
        # /ingest is bound to ingest_dump). Redact+dedup policy mirrors /ingest so
        # session narratives — which may carry secrets — are scrubbed on write too.
        graph = PostgresVectorGraph(
            conn, org, uid, policy=[Redactor(), Deduper()]
        )
        ingestor = SessionIngestor(graph, OpenRouterLlm())
        insights = ingestor.synthesis(narrative, source=source)
        candidates: list[dict[str, Any]] = []
        for ins in insights:
            # Write each insight with its scope/category (PostgresVectorGraph.write
            # accepts them; the base Ingestor.ingest, bound to the frozen ABC write
            # signature, would not). Lands "proposed" — the human-gated lifecycle.
            fid = graph.write(
                ins.raw_text,
                state="proposed",
                source=ins.source,
                scope=ins.scope,
                category=ins.category,
            )
            if fid:
                candidates.append(
                    {"id": fid, "scope": ins.scope, "category": ins.category}
                )
        return {"source": source, "count": len(candidates), "candidates": candidates}

    # --- derivation / staleness traversal (H5) -----------------------------
    def _derivation_view(fact: Any) -> dict[str, Any]:
        """Compact read model for a derivation/staleness hit (incl. ``meta``)."""
        return {
            "id": fact.id,
            "text": fact.text,
            "state": fact.state,
            "source": getattr(fact, "source", None),
            "scope": getattr(fact, "scope", None),
            "category": getattr(fact, "category", None),
            "meta": dict(fact.meta or {}),
        }

    def _requirement_completeness_view(item: dict[str, Any]) -> dict[str, Any]:
        view = _derivation_view(item["fact"])
        view.update(
            {
                "reason": item["reason"],
                "reasons": item["reasons"],
                "successCount": item["success_count"],
                "failureCount": item["failure_count"],
                "lastOutcome": item["last_outcome"],
            }
        )
        # Build-loop lease view, so the selector can reason about who (if anyone)
        # already holds this ticket. Present whenever the source item carries it.
        if "claim" in item:
            view["claim"] = item["claim"]
        return view

    @app.get("/derivations/stale")
    def stale_derivations(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Active learnings flagged stale because a fact they derive from was invalidated (H5).

        When a source fact is invalidated (e.g. rejected), the H5 hook stamps a
        review edge on every transitive ``derived_from`` dependent. This surfaces
        those suspect learnings for human/agent review (precision-first: flagged,
        never auto-rejected).
        """
        stale = live_graph(org, uid).stale_derived()
        return {"stale": [_derivation_view(f) for f in stale]}

    @app.get("/facts/{fact_id}/dependents")
    def fact_dependents(
        fact_id: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Transitive derivation dependents of ``fact_id`` (the learnings derived from it).

        Walks ``derived_from`` edges (src=dependent -> dst=basis) up the chain,
        cycle-guarded and depth-bounded, newest first.
        """
        deps = live_graph(org, uid).dependents(fact_id)
        return {"factId": fact_id, "dependents": [_derivation_view(f) for f in deps]}

    # --- requirement RENDERS surface (factory completeness gate) ------------
    @app.post("/surfaces")
    def ensure_surface(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Idempotently ensure a surface fact exists for ``(project, screenId)``.

        A surface is a screen in the clickable wireframe modeled as a fact
        (``category="surface"``) so it can be a typed ``renders`` edge endpoint.
        At most one surface fact per ``(project, screenId)``; re-calling merges
        ``title``/``file``/``states`` into its meta.
        """
        project = str(body.get("project") or "").strip()
        screen_id = str(body.get("screenId") or "").strip()
        if not project or not screen_id:
            raise HTTPException(
                status_code=400, detail="body must include 'project' and 'screenId'"
            )
        g = live_graph(org, uid)
        surface_id = g.ensure_surface(
            project,
            screen_id,
            title=body.get("title"),
            file=body.get("file"),
            states=body.get("states"),
        )
        return {"id": surface_id, "project": project, "screenId": screen_id}

    @app.post("/surfaces/bind")
    def bind_surface(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Bind a requirement fact to a surface via a typed ``renders`` edge (PRIMARY write).

        Ensures the surface fact exists, then attaches ``requirement --renders--> surface``.
        Idempotent (duplicate edges are ignored). 404 if the requirement fact is unknown.
        With a ``(space, snapshot)`` target the edge is written into that snapshot (so a
        surface-bound check/requirement authored into a project snapshot resolves at build).
        """
        requirement_fact_id = str(body.get("requirementFactId") or "").strip()
        screen_id = str(body.get("screenId") or "").strip()
        project = str(body.get("project") or "").strip()
        if not requirement_fact_id or not screen_id or not project:
            raise HTTPException(
                status_code=400,
                detail="body must include 'requirementFactId', 'screenId' and 'project'",
            )
        g = graph_for(org, uid, target)
        if g.get_fact(requirement_fact_id) is None:
            raise HTTPException(
                status_code=404, detail=f"unknown fact {requirement_fact_id}"
            )
        surface_id = g.bind_surface(
            requirement_fact_id,
            screen_id,
            project,
            title=body.get("title"),
            file=body.get("file"),
            states=body.get("states"),
        )
        return {
            "requirementFactId": requirement_fact_id,
            "surfaceId": surface_id,
            "screenId": screen_id,
        }

    @app.post("/surfaces/unbind")
    def unbind_surface(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Remove the ``renders`` edge between a requirement fact and a surface.

        Idempotent: a no-op if no such edge (or surface) exists.
        """
        requirement_fact_id = str(body.get("requirementFactId") or "").strip()
        screen_id = str(body.get("screenId") or "").strip()
        project = str(body.get("project") or "").strip()
        if not requirement_fact_id or not screen_id or not project:
            raise HTTPException(
                status_code=400,
                detail="body must include 'requirementFactId', 'screenId' and 'project'",
            )
        g = live_graph(org, uid)
        g.unbind_surface(requirement_fact_id, screen_id, project)
        return {
            "requirementFactId": requirement_fact_id,
            "screenId": screen_id,
            "project": project,
            "ok": True,
        }

    @app.get("/surfaces/{screen_id}/requirements")
    def requirements_for_surface(
        screen_id: str,
        project: str = "",
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
    ) -> dict[str, Any]:
        """Active requirement facts that render the surface ``(project, screenId)`` (PRIMARY read).

        Answers "which requirements govern screen s-X" for the wireframe->code step.
        """
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        reqs = live_graph(org, uid).requirements_for_surface(project, screen_id)
        return {
            "project": project,
            "screenId": screen_id,
            "requirements": [_derivation_view(f) for f in reqs],
        }

    @app.get("/surfaces/{screen_id}/checks")
    def checks_for_surface(
        screen_id: str,
        project: str = "",
        scope: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Active ``check`` facts bound to the surface ``(project, screenId)`` (EXHAUSTIVE).

        The coverage-spine convenience over ``/facts/by``: every check the
        ``renders`` edge binds to this screen, optionally narrowed to one gate via
        ``scope`` (matches ``meta.scope`` — "planning" | "validation"). Active-only.
        """
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        checks = graph_for(org, uid, target).checks_for_surface(
            project, screen_id, scope=scope
        )
        return {
            "project": project,
            "screenId": screen_id,
            "scope": scope,
            "checks": [_derivation_view(f) for f in checks],
        }

    @app.get("/facts/by")
    def facts_by(
        category: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        state: str = "active",
        meta: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """EXHAUSTIVE, server-side filtered fact enumeration (no top-k, no ranking).

        The completeness primitive: returns EVERY active fact matching the column
        filters (``category``/``source``/``scope`` — the top-level scope column) plus
        the JSONB ``meta`` filter. ``state`` defaults to ``active``; pass
        ``state=any`` (or empty) to enumerate across all lifecycle states. ``meta`` is
        a JSON object string (e.g. ``{"scope":"validation","applies_to":"s-home"}``);
        each key matches by scalar equality OR array-membership. 400 on invalid JSON.
        """
        state_filter: str | None = state
        if state in ("", "any", "all"):
            state_filter = None
        meta_filter: dict | None = None
        if meta is not None and meta.strip():
            try:
                parsed = json.loads(meta)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail=f"meta must be valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=400, detail="meta must be a JSON object"
                )
            meta_filter = parsed
        facts = graph_for(org, uid, target).facts_by(
            category=category,
            source=source,
            scope=scope,
            state=state_filter,
            meta_filter=meta_filter,
        )
        return {"facts": [_derivation_view(f) for f in facts]}

    @app.get("/facts/{fact_id}/surfaces")
    def surfaces_for_requirement(
        fact_id: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Active surface facts governed by the requirement ``fact_id`` (which screens it renders)."""
        surfaces = graph_for(org, uid, target).surfaces_for_requirement(fact_id)
        return {"factId": fact_id, "surfaces": [_derivation_view(f) for f in surfaces]}

    @app.get("/surfaces/bindings")
    def list_surface_bindings(
        project: str = "",
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """All requirement<->surface ``renders`` bindings for ``project`` (any state)."""
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        bindings = graph_for(org, uid, target).list_surface_bindings(project)
        return {"project": project, "bindings": bindings}

    @app.get("/surfaces/coverage")
    def surface_coverage(
        project: str = "",
        scope: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Bidirectional completeness gate for ``project``.

        Reports surfaces with no governing requirement and requirements that render
        no surface (optionally filtered to a requirement ``scope`` such as ``mvp``).
        """
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        cov = graph_for(org, uid, target).surface_coverage(project, scope=scope)
        return {
            "project": project,
            "uncoveredSurfaces": [_derivation_view(f) for f in cov["uncoveredSurfaces"]],
            "uncoveredRequirements": [
                _derivation_view(f) for f in cov["uncoveredRequirements"]
            ],
        }

    @app.get("/requirements/incomplete")
    def incomplete_requirements(
        project: str = "",
        exclude_leased: bool = False,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Active requirements in ``prd-<project>`` not yet verified-complete (derived
        from verification + staleness: never-built | regressed | stale).

        Each item carries a ``claim`` view (``build_state``, ``claim_owner``,
        ``claim_heartbeat_at``, ``lease_live``) for the multi-agent build loop. With
        ``exclude_leased=true`` tickets under a LIVE lease are omitted so a worker
        only sees claimable ones (stale-leased and unclaimed remain). Default false
        keeps the prior behavior/response for back-compat."""
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        items = graph_for(org, uid, target).incomplete_requirements(
            project, exclude_leased=exclude_leased
        )
        return {
            "project": project,
            "incomplete": [_requirement_completeness_view(i) for i in items],
        }

    @app.post("/requirements/{cid}/claim")
    def claim_requirement(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Atomically lease ticket ``cid`` to ``owner`` for the build loop.

        Body: ``{"owner": str, "lease_ttl_seconds": int = 1800}``. Grants iff the
        ticket is not held by a different LIVE lease (unclaimed / same owner / stale).
        ``200 {claim}`` on grant; ``409`` (with current owner + remaining seconds)
        when a different live owner holds it. The grant is atomic at the DB row level
        — two concurrent claims yield exactly one ``200`` and one ``409``."""
        owner = str(body.get("owner") or "").strip()
        if not owner:
            raise HTTPException(status_code=400, detail="body must include 'owner'")
        ttl = body.get("lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="'lease_ttl_seconds' must be an integer"
            )
        try:
            claim = graph_for(org, uid, target).claim_requirement(cid, owner, ttl)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown requirement {cid}")
        except LeaseConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "ticket held by a live lease",
                    "owner": exc.owner,
                    "remainingSeconds": exc.remaining,
                },
            )
        return {"id": cid, "claim": claim}

    @app.post("/requirements/{cid}/heartbeat")
    def heartbeat_requirement(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Renew ``owner``'s live lease on ticket ``cid`` (Body: ``{"owner": str}``).

        ``200 {claim}`` when ``owner`` still holds a live lease (heartbeat bumped);
        ``409`` once the lease was lost or expired (stop working it); ``404`` if the
        requirement is unknown."""
        owner = str(body.get("owner") or "").strip()
        if not owner:
            raise HTTPException(status_code=400, detail="body must include 'owner'")
        try:
            claim = graph_for(org, uid, target).heartbeat_requirement(cid, owner)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown requirement {cid}")
        except LeaseConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "lease lost or expired",
                    "owner": exc.owner,
                    "remainingSeconds": exc.remaining,
                },
            )
        return {"id": cid, "claim": claim}

    @app.post("/requirements/{cid}/release")
    def release_requirement(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Clear ``owner``'s lease and set a terminal ``build_state``.

        Body: ``{"owner": str, "state": "finished" | "incomplete"}``. Used when a
        ticket finishes or an agent yields cleanly: drops the lease keys and records
        ``build_state`` (MERGED into ``meta``, never clobbering other keys). ``200
        {claim}`` on success; ``409`` if ``owner`` no longer holds the lease; ``404``
        if unknown; ``400`` for a bad ``state``. ``finished`` clears the lease only —
        outcome-derived completeness is unchanged."""
        owner = str(body.get("owner") or "").strip()
        if not owner:
            raise HTTPException(status_code=400, detail="body must include 'owner'")
        state = str(body.get("state") or "").strip()
        try:
            claim = graph_for(org, uid, target).release_requirement(cid, owner, state)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown requirement {cid}")
        except LeaseConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "not the lease owner",
                    "owner": exc.owner,
                    "remainingSeconds": exc.remaining,
                },
            )
        return {"id": cid, "claim": claim}

    @app.get("/requirements/completeness")
    def completeness_summary(
        project: str = "",
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Done-of-definition counts for ``prd-<project>``'s active requirements."""
        project = str(project or "").strip()
        if not project:
            raise HTTPException(status_code=400, detail="query param 'project' is required")
        summary = graph_for(org, uid, target).completeness_summary(project)
        return {"project": project, **summary}

    @app.get("/context")
    @limiter.limit(LLM_RATE_LIMIT)
    def get_context(
        request: Request,
        query: str = "",
        top_k: int = 8,
        include_episodic: bool = False,
        as_of: str | None = None,
        hybrid: bool = False,
        keyword_weight: float | None = None,
        char_budget: int | None = None,
        category: str | None = None,
        categories: str | None = None,
        scope: str | None = None,
        meta: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
        uid: str = Depends(active_user_id),
        target: tuple[str, str] | None = Depends(snapshot_target),
    ) -> dict[str, Any]:
        """Return active-fact context relevant to ``query`` (the eval read path).

        If the caller has mounted snapshots (read-only overlays), retrieval unions
        the live graph with those snapshots; overlay hits are flagged ``mounted``
        with their ``space``/``snapshot``. Mounts never affect writes or saves. An
        explicit ``(space, snapshot)`` target reads that snapshot directly and
        never unions mounts.

        Episodic decision logs (``category="episodic"``) are excluded by default (H2)
        so "why we decided" notes never pollute semantic recall; pass
        ``include_episodic=true`` to include them.

        ``as_of`` (ISO-8601, e.g. ``2024-01-01T00:00:00Z``) rewinds retrieval to a
        point in time: only facts whose validity window covers that instant are
        returned, so a later-written fact is excluded. Applies to the live-graph
        ``hits``; the mounted-snapshot union is not point-in-time aware.

        Retrieval-tuning knobs (gap H7), all optional, defaulting to the calibrated
        behavior: ``hybrid`` fuses a BM25 keyword branch into the cosine ranking;
        ``keyword_weight`` biases that fusion toward exact/symbol matches (only with
        ``hybrid=true``); ``char_budget`` caps the returned ``context`` size. Fusion
        knobs apply to the live graph; the mounted-snapshot union is cosine-only.

        Positive read filters (all optional) narrow the SIMILARITY ranking to a subset
        without changing the ranking: ``category`` (single) and/or ``categories`` (a
        comma-separated list) keep only those categories; ``scope`` matches the
        top-level scope column; ``meta`` is a JSON object string (400 on bad JSON) —
        e.g. ``{"scope":"planning"}`` — matched against the JSONB ``meta`` column by
        scalar equality OR array-membership (same as ``/facts/by``). The filters apply
        to BOTH the live graph and any mounted-snapshot union. Absent -> unchanged.
        """
        as_of_dt: datetime | None = None
        if as_of is not None and as_of.strip():
            try:
                as_of_dt = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="as_of must be an ISO-8601 timestamp"
                )
        # Merge single ``category`` + CSV ``categories`` into one positive list
        # (deduped, order-preserving); None when empty so _where skips the filter.
        cats: list[str] = []
        if category and category.strip():
            cats.append(category.strip())
        if categories and categories.strip():
            cats.extend(c.strip() for c in categories.split(",") if c.strip())
        cats_filter = list(dict.fromkeys(cats)) or None
        scope_filter = scope.strip() if scope and scope.strip() else None
        meta_filter: dict | None = None
        if meta is not None and meta.strip():
            try:
                parsed = json.loads(meta)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail=f"meta must be valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="meta must be a JSON object")
            meta_filter = parsed
        base = graph_for(org, uid, target)
        # A snapshot-bound read (explicit X-Praxis-Space + X-Praxis-Snapshot) reads
        # that snapshot directly and never unions mounts — mounts are a
        # working-memory read overlay only. Working-memory reads union the viewer's
        # mounted snapshots (read-only) so retrieval sees live facts plus every
        # mounted (space, snapshot).
        if target is None:
            mounts = mounted_store.list(org, principal.sub)
            graph = OverlayGraph(base, mounts) if mounts else base
        else:
            graph = base
        exclude = None if include_episodic else [EPISODIC_CATEGORY]
        hits = (
            graph.search(
                query,
                top_k=top_k,
                hybrid=hybrid,
                keyword_weight=keyword_weight,
                exclude_categories=exclude,
                categories=cats_filter,
                scope=scope_filter,
                meta_filter=meta_filter,
                as_of=as_of_dt,
            )
            if query.strip() else []
        )
        # When a positive filter is active, derive the context blob from the filtered
        # hits so it matches them (graph.read takes no positive filter). With no filter
        # this stays the exact prior call, preserving byte-for-byte parity.
        if cats_filter or scope_filter or meta_filter:
            budget = _READ_CHAR_BUDGET if char_budget is None else char_budget
            parts: list[str] = []
            used = 0
            for h in hits:
                if used + len(h.fact.text) > budget and parts:
                    break
                parts.append(h.fact.text)
                used += len(h.fact.text)
            context_text = "\n\n".join(parts)
        else:
            context_text = graph.read(
                query, exclude_categories=exclude, char_budget=char_budget
            )
        return {
            "context": context_text,
            "hits": [
                {
                    "id": h.fact.id,
                    "text": h.fact.text,
                    "score": h.score,
                    "source": getattr(h.fact, "source", None),
                    "scope": getattr(h.fact, "scope", None),
                    "category": getattr(h.fact, "category", None),
                    "mounted": bool((h.fact.meta or {}).get("mountedFrom")),
                    "space": (h.fact.meta or {}).get("mountedFrom", {}).get("space"),
                    "snapshot": (h.fact.meta or {}).get("mountedFrom", {}).get("snapshot"),
                }
                for h in hits
            ],
        }

    return app


app = create_app()
