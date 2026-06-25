"""FastAPI server implementing the candidate-api-v1 contract over the facts spine.

Single source of truth: the ``facts`` table (one tenant graph per
``(org_id, user_id)``), reached through :class:`PostgresVectorGraph`. The
dashboard "candidate" read model is a projection of facts via
:class:`knowledge.serve.facts_candidates.FactsCandidates`; the graph view, the
MCP ``get_context`` retrieval, and the Contradictions tab all read the same rows.

Saved graph states (user snapshots + cached eval cases) live in the
``cached_facts`` table keyed by ``cache_key`` (``snapshot:<name>`` /
``eval:<case_id>``); loading one truncates the live graph and inserts the saved
rows, and snapshotting copies the live graph into the cache.

Every data route hard-requires a valid Cognito JWT (the ``current_user``
dependency) and resolves the active org from the ``X-Praxis-Org`` header; the
caller must be a member of that org. ``/health`` stays open. A Postgres DSN is
required (no JSON/offline fallback).

Run: uv run python -m knowledge.serve   (serves on http://localhost:8000)
"""

from __future__ import annotations

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
    EPISODIC_CATEGORY,
    PostgresVectorGraph,
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
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm  # noqa: E402
from knowledge.serve import db, graph_adapter  # noqa: E402
from knowledge.serve.auth import Principal, make_current_user  # noqa: E402
from knowledge.serve.facts_candidates import (  # noqa: E402
    DeletionError,
    FactsCandidates,
    PromotionError,
)
from knowledge.serve.mounted_store import MountedStore  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
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

    conn = _ConnProxy(resolve_conn)
    orgs_store = OrgsStore(conn)
    mounted_store = MountedStore(conn)
    # Bind the auth dependency to this connection so it can also resolve API keys
    # (X-Praxis-Key) in addition to the Cognito Bearer JWT / dev seam.
    current_user = make_current_user(conn)
    # Test seam: lets reliability tests assert per-thread isolation + reopen.
    app_get_conn = resolve_conn

    def candidates_for(org: str, sub: str) -> FactsCandidates:
        """The candidate facade for one requester's tenant graph."""
        return FactsCandidates(conn, org, sub)

    def live_graph(org: str, sub: str) -> PostgresVectorGraph:
        """The live facts graph for one requester (no write policy needed for reads)."""
        return PostgresVectorGraph(conn, org, sub)

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
    ) -> list[dict[str, Any]]:
        return candidates_for(org, principal.sub).list(state)

    @app.get("/candidates/{cid}")
    def get_candidate(
        cid: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        c = candidates_for(org, principal.sub).get(cid)
        if c is None:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        return c

    @app.post("/candidates/{cid}/promote")
    def promote(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, principal.sub).promote(cid, body.get("targetState"))
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
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, principal.sub).reject(cid, body.get("reason"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    @app.post("/facts/{fact_id}/outcome")
    def record_fact_outcome(
        fact_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
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
        live_graph(org, principal.sub).record_outcome(fact_id, success=success)
        return {"id": fact_id, "success": success}

    @app.post("/candidates")
    def create_candidate(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, principal.sub).create(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.patch("/candidates/{cid}")
    def update_candidate(
        cid: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        try:
            return candidates_for(org, principal.sub).update(cid, body)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/candidates/{cid}")
    def delete_candidate(
        cid: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        try:
            candidates_for(org, principal.sub).delete(cid)
            return {"deleted": cid}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        except DeletionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # --- contradictions ----------------------------------------------------
    @app.get("/contradictions")
    def contradictions(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> list[dict[str, Any]]:
        return candidates_for(org, principal.sub).contradictions()

    @app.post("/contradictions/{pair_id}/resolve")
    def resolve(
        pair_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        facade = candidates_for(org, principal.sub)
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
        g = live_graph(org, principal.sub)
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
    ) -> dict[str, Any]:
        """Truncate the requester's live graph only (scoped to org_id + user_id).

        Deletes every fact/edge owned by ``principal.sub`` in this org; other
        users' rows (including their shared facts) are untouched. Implemented as
        a load of zero cache keys, which truncates without refilling.
        """
        g = live_graph(org, principal.sub)
        removed = len(g.all_facts())
        g.load_caches([])
        return {"cleared": removed}

    # --- snapshots (saved live-graph states in the cache) ------------------
    @app.get("/snapshots")
    def list_snapshots(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        entries = live_graph(org, principal.sub).list_caches("snapshot:")
        return {
            "snapshots": [
                {
                    "name": e["key"].split("snapshot:", 1)[1],
                    "count": e["count"],
                    "createdAt": e["created_at"],
                }
                for e in entries
            ]
        }

    @app.post("/snapshots")
    def save_snapshot(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Save the current live graph under ``name`` (create or overwrite)."""
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        count = live_graph(org, principal.sub).save_cache(f"snapshot:{name}")
        return {"name": name, "count": count}

    @app.post("/snapshots/{name}/load")
    def load_snapshot(
        name: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Put a snapshot into the live graph.

        ``mode="replace"`` (default) truncates the whole live graph then inserts
        the snapshot. ``mode="add"`` additively merges the snapshot into the
        current graph (replacing only nodes the snapshot shares by id), keeping
        other live facts.
        """
        mode = str(body.get("mode") or "replace").strip().lower()
        if mode not in ("add", "replace"):
            raise HTTPException(status_code=400, detail="mode must be 'add' or 'replace'")
        g = live_graph(org, principal.sub)
        key = f"snapshot:{name}"
        if g.cache_count(key) == 0:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {name!r}")
        loaded = g.merge_caches_into_live([key]) if mode == "add" else g.load_caches([key])
        return {"loaded": loaded, "mode": mode}

    @app.delete("/snapshots/{name}")
    def delete_snapshot(
        name: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        live_graph(org, principal.sub).delete_cache(f"snapshot:{name}")
        # Drop any mounts the owner had of this snapshot (it no longer exists).
        mounted_store.unmount(org, principal.sub, principal.sub, name)
        return {"deleted": name}

    # --- mounted snapshots (read-only overlay selection) -------------------
    def _validate_mount_target(org: str, source_user: str, name: str) -> None:
        """Ensure ``source_user`` is an org member and the snapshot exists."""
        if not orgs_store.is_member(org, source_user):
            raise HTTPException(
                status_code=404, detail=f"{source_user!r} is not a member of org {org!r}"
            )
        if live_graph(org, source_user).cache_count(f"snapshot:{name}") == 0:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {name!r}")

    @app.get("/mounts")
    def list_mounts(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """List the caller's mounted snapshots (read-only retrieval overlays).

        Each mount adds a snapshot's facts to what retrieval reads, without
        merging them into the live graph and without being carried over on save.
        """
        mounts = mounted_store.list(org, principal.sub)
        return {
            "mounts": [
                {
                    "sourceUser": m["source_user_id"],
                    "snapshot": m["snapshot_name"],
                    "isSelf": m["source_user_id"] == principal.sub,
                    "count": live_graph(org, m["source_user_id"]).cache_count(
                        f"snapshot:{m['snapshot_name']}"
                    ),
                }
                for m in mounts
            ]
        }

    @app.post("/mounts")
    def add_mount(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Mount a snapshot (your own or an org member's) as a read overlay."""
        source_user = str(body.get("sourceUser") or principal.sub).strip()
        name = str(body.get("snapshot") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="snapshot required")
        _validate_mount_target(org, source_user, name)
        mounted_store.mount(org, principal.sub, source_user, name)
        return {"sourceUser": source_user, "snapshot": name, "mounted": True}

    @app.delete("/mounts")
    def remove_mount(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Unmount a snapshot (no-op if it was not mounted)."""
        source_user = str(body.get("sourceUser") or principal.sub).strip()
        name = str(body.get("snapshot") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="snapshot required")
        mounted_store.unmount(org, principal.sub, source_user, name)
        return {"sourceUser": source_user, "snapshot": name, "mounted": False}

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
        """List browsable sources in the org: every member + their snapshots.

        Within an org (the trust boundary) any member may browse any other
        member's saved snapshots. Each member's snapshots (name + node count)
        are read from their own cache partition.
        """
        sources: list[dict[str, Any]] = []
        for member in orgs_store.members(org):
            uid = member["user_id"]
            is_self = uid == principal.sub
            snapshots = [
                {
                    "name": e["key"].split("snapshot:", 1)[1],
                    "count": e["count"],
                }
                for e in live_graph(org, uid).list_caches("snapshot:")
            ]
            sources.append(
                {
                    "userId": uid,
                    # Emails aren't stored app-side (org_members keys on the
                    # Cognito sub), so we can only resolve the caller's own
                    # username from their token; teammates fall back to the id.
                    "username": principal.email if is_self else None,
                    "role": member["role"],
                    "isSelf": is_self,
                    "snapshots": snapshots,
                }
            )
        return {"sources": sources}

    @app.get("/org/sources/{user_id}/snapshots/{name}/facts")
    def org_source_snapshot_facts(
        user_id: str,
        name: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Browse one member's snapshot facts, grouped by folder (``scope``).

        ``active_org`` already proved the caller is a member of ``org``; full
        within-org trust means any member is a valid ``user_id`` target. The
        org-scoped reader pins ``org_id`` so no other org's rows are reachable.
        404 if ``user_id`` is not a member or the snapshot holds no facts.
        """
        if not orgs_store.is_member(org, user_id):
            raise HTTPException(
                status_code=404, detail=f"{user_id!r} is not a member of org {org!r}"
            )
        if live_graph(org, user_id).cache_count(f"snapshot:{name}") == 0:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {name!r}")
        reader = OrgSourceReader(conn, org, user_id, cache_key=f"snapshot:{name}")
        return {
            "userId": user_id,
            "snapshot": name,
            "groups": _group_facts(reader.all_facts()),
        }

    @app.post("/fold-in")
    @limiter.limit(LLM_RATE_LIMIT)
    def fold_in(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Copy selected snapshot facts from a source into the caller's live graph.

        Each selected fact is written through a distillation-free policy
        (``Deduper`` then ``ConflictFlagger`` — no ``Redactor``, no LLM
        re-distillation): already-atomic facts are deduped against the caller's
        graph and conflicts are flagged, never silently overwritten. Copies land
        ``active`` (an explicit user action) and carry provenance in ``meta``.
        Edges whose both endpoints are in the selection are carried with the
        copy, remapped to the caller's new fact ids.

        ``mode="replace"`` truncates the caller's own live graph before copying
        (so the caller ends up holding exactly the folded facts); ``mode="add"``
        (default) merges the selection into the existing graph.
        """
        source_user = str(body.get("sourceUser") or "").strip()
        snapshot = str(body.get("snapshot") or "").strip()
        mode = str(body.get("mode") or "add").strip().lower()
        fact_ids = body.get("factIds") or []
        if not source_user:
            raise HTTPException(status_code=400, detail="sourceUser required")
        if not snapshot:
            raise HTTPException(status_code=400, detail="snapshot required")
        if mode not in ("add", "replace"):
            raise HTTPException(status_code=400, detail="mode must be 'add' or 'replace'")
        if not isinstance(fact_ids, list) or not fact_ids:
            raise HTTPException(status_code=400, detail="factIds must be a non-empty list")
        if not orgs_store.is_member(org, source_user):
            raise HTTPException(
                status_code=404, detail=f"{source_user!r} is not a member of org {org!r}"
            )
        cache_key = f"snapshot:{snapshot}"
        reader = OrgSourceReader(conn, org, source_user, cache_key=cache_key)
        src_facts = reader.get_facts([str(i) for i in fact_ids])
        if not src_facts:
            raise HTTPException(status_code=404, detail="no matching facts in source")

        # mode=replace: truncate the caller's live graph before copying (same
        # zero-cache-load truncation as /graph/clear). Dedup/conflict are then
        # moot on the now-empty graph, but the policy stays identical.
        if mode == "replace":
            live_graph(org, principal.sub).load_caches([])

        # Distillation-free fold-in policy: dedup the already-atomic facts, then
        # the structural claim path (extract -> detect) flags genuine value
        # conflicts as contradiction edges. No Redactor (source facts are already
        # vetted) and no LLM re-distillation of the text itself.
        base_llm = OpenRouterLlm()
        graph = PostgresVectorGraph(
            conn,
            org,
            principal.sub,
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
            meta["foldedFrom"] = {"userId": source_user, "source": cache_key}
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
    ) -> dict[str, Any]:
        """Eval case ids with cached data + node counts (drives the UI status dots).

        ``counts`` maps each cached case id to how many nodes its cached
        distillation holds — what the dashboard shows inside the green dot.
        """
        entries = live_graph(org, principal.sub).list_caches("eval:")
        ids = [e["key"].split("eval:", 1)[1] for e in entries]
        counts = {
            e["key"].split("eval:", 1)[1]: int(e.get("count", 0)) for e in entries
        }
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

        Returns (regenerated_case_ids, from_cache_case_ids). Writes only the
        cache (``cached_facts``); the live graph is untouched here.
        """
        live = live_graph(org, sub)
        regenerated: list[str] = []
        from_cache: list[str] = []
        for cid in case_ids:
            key = f"eval:{cid}"
            if not force and live.cache_count(key) > 0:
                from_cache.append(cid)
                continue
            seeds = distill_case(cid, distill=distill)
            eval_graph = PostgresVectorGraph(
                conn,
                org,
                sub,
                facts_table="cached_facts",
                edges_table="cached_fact_edges",
                cache_key=key,
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
                principal.sub,
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
                principal.sub,
                case_ids,
                distill=bool(body.get("distill", False)),
                force=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RegenerateUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        keys = [f"eval:{cid}" for cid in case_ids]
        live = live_graph(org, principal.sub)
        if mode == "replace":
            facts_in_graph = live.load_caches(keys)
        else:
            facts_in_graph = live.merge_caches_into_live(keys)
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
        """
        insight = (body.get("insight") or "").strip()
        if not insight:
            raise HTTPException(status_code=400, detail="insight required")
        # Body-size cap (mirrors /ingest/session): reject oversized input before any
        # LLM call. 128 KB matches the session-ingest ceiling.
        if len(insight.encode("utf-8")) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="insight too large")
        # Episodic (H4): a decision log entry bypasses the semantic pipeline — stored
        # whole, append-only, immutable (no distill/dedup/contradiction) and out of
        # semantic recall. Routed to the store-only producer.
        if (body.get("category") or "").strip() == EPISODIC_CATEGORY:
            return _record_episode(live_graph(org, principal.sub), insight, body)
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
        # auto_resolve: ConflictOverwriter turns a confirmed contradiction into a
        # force-overwrite (loser rejected). surface: the structural+semantic detector
        # pipeline only *flags* the clash, so the store persists a pending
        # contradiction edge instead of rejecting a side.
        graph = PostgresVectorGraph(
            conn, org, principal.sub, policy=_insight_write_policy(on_conflict)
        )
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
        write path. This endpoint does ONE round-trip, builds the policy graph +
        ingestor ONCE, and writes the items **serially** within the request.

        Returns ``{"results": [...], "count": int}`` — one result per input item,
        in order, each carrying ``id``/``action``/``contradictionsSurfaced`` and a
        ``retrievable`` flag confirming the fact is immediately read-back-able
        (read-your-writes), so the caller doesn't have to poll. Each result also
        carries ``ok`` and, on a per-item failure, an ``error`` string — one bad
        item fails cleanly without aborting the rest of the batch.
        """
        insights = body.get("insights")
        if not isinstance(insights, list) or not insights:
            raise HTTPException(status_code=400, detail="insights must be a non-empty list")
        on_conflict = str(body.get("onConflict") or "auto_resolve").strip().lower()
        if on_conflict not in ("auto_resolve", "surface"):
            raise HTTPException(
                status_code=400, detail="onConflict must be 'auto_resolve' or 'surface'"
            )
        # One policy graph + ingestor for the whole batch (the throughput win); the
        # episodic store-only path needs no policy, so it reuses the same instance.
        graph = PostgresVectorGraph(
            conn, org, principal.sub, policy=_insight_write_policy(on_conflict)
        )
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        results: list[dict[str, Any]] = []
        for i, item in enumerate(insights):
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "each insight must be an object", "index": i})
                continue
            text = (item.get("insight") or "").strip()
            if not text:
                results.append({"ok": False, "error": "insight required", "index": i})
                continue
            item_meta = item.get("meta")
            if item_meta is not None and not isinstance(item_meta, dict):
                results.append({"ok": False, "error": "meta must be an object", "index": i})
                continue
            # A single bad/edge-case item must not poison the rest of the batch.
            try:
                if (item.get("category") or "").strip() == EPISODIC_CATEGORY:
                    res = _record_episode(graph, text, item)
                else:
                    res = _write_insight(
                        graph,
                        ingestor,
                        insight=text,
                        source=item.get("source"),
                        scope=item.get("scope"),
                        category=item.get("category"),
                        meta=item_meta,
                        on_conflict=on_conflict,
                        derived_from=item.get("derivedFrom"),
                    )
                res["ok"] = True
            except Exception as exc:  # noqa: BLE001 - report, don't abort the batch
                res = {"ok": False, "error": str(exc), "index": i}
            results.append(res)
        return {"results": results, "count": len(results)}

    @app.post("/ingest")
    @limiter.limit(LLM_RATE_LIMIT)
    def ingest_documents(
        request: Request,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
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
        graph = PostgresVectorGraph(
            conn,
            org,
            principal.sub,
            policy=[Redactor(), Deduper()],
        )
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
            conn, org, principal.sub, policy=[Redactor(), Deduper()]
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

    @app.get("/derivations/stale")
    def stale_derivations(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Active learnings flagged stale because a fact they derive from was invalidated (H5).

        When a source fact is invalidated (e.g. rejected), the H5 hook stamps a
        review edge on every transitive ``derived_from`` dependent. This surfaces
        those suspect learnings for human/agent review (precision-first: flagged,
        never auto-rejected).
        """
        stale = live_graph(org, principal.sub).stale_derived()
        return {"stale": [_derivation_view(f) for f in stale]}

    @app.get("/facts/{fact_id}/dependents")
    def fact_dependents(
        fact_id: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Transitive derivation dependents of ``fact_id`` (the learnings derived from it).

        Walks ``derived_from`` edges (src=dependent -> dst=basis) up the chain,
        cycle-guarded and depth-bounded, newest first.
        """
        deps = live_graph(org, principal.sub).dependents(fact_id)
        return {"factId": fact_id, "dependents": [_derivation_view(f) for f in deps]}

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
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Return active-fact context relevant to ``query`` (the eval read path).

        If the caller has mounted snapshots (read-only overlays), retrieval unions
        the live graph with those snapshots; overlay hits are flagged ``mounted``
        with their ``owner``/``snapshot``. Mounts never affect writes or saves.

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
        """
        as_of_dt: datetime | None = None
        if as_of is not None and as_of.strip():
            try:
                as_of_dt = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="as_of must be an ISO-8601 timestamp"
                )
        live = live_graph(org, principal.sub)
        mounts = mounted_store.list(org, principal.sub)
        graph = OverlayGraph(live, mounts) if mounts else live
        exclude = None if include_episodic else [EPISODIC_CATEGORY]
        hits = (
            graph.search(
                query,
                top_k=top_k,
                hybrid=hybrid,
                keyword_weight=keyword_weight,
                exclude_categories=exclude,
                as_of=as_of_dt,
            )
            if query.strip() else []
        )
        return {
            "context": graph.read(query, exclude_categories=exclude, char_budget=char_budget),
            "hits": [
                {
                    "id": h.fact.id,
                    "text": h.fact.text,
                    "score": h.score,
                    "source": getattr(h.fact, "source", None),
                    "scope": getattr(h.fact, "scope", None),
                    "category": getattr(h.fact, "category", None),
                    "mounted": bool((h.fact.meta or {}).get("mountedFrom")),
                    "owner": (h.fact.meta or {}).get("mountedFrom", {}).get("userId"),
                    "snapshot": (h.fact.meta or {}).get("mountedFrom", {}).get("snapshot"),
                }
                for h in hits
            ],
        }

    return app


app = create_app()
