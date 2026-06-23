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
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

# Load the repo-root ``.env`` (OPENROUTER/COGNITO/PRAXIS_DB_URL ...) before any
# ``knowledge.*`` import reads the environment. Without this the server starts
# with an empty env: no DB, no Cognito ("invalid token"), no embedder key.
load_dotenv()

from fastapi import Body, Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
    default_write_policy,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    ConflictOverwriter,
    Deduper,
    Redactor,
)
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm  # noqa: E402
from knowledge.serve import db, graph_adapter  # noqa: E402
from knowledge.serve.auth import Principal, current_user  # noqa: E402
from knowledge.serve.facts_candidates import FactsCandidates, PromotionError  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402
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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_app(conn: Any | None = None) -> FastAPI:
    """Build the app over a single shared Postgres connection.

    The connection is opened once per process (autocommit) and shared by the
    orgs store and every per-request tenant graph. A resolvable DSN is required.
    """
    conn = conn if conn is not None else db.connect()
    orgs_store = OrgsStore(conn)

    def candidates_for(org: str, sub: str) -> FactsCandidates:
        """The candidate facade for one requester's tenant graph."""
        return FactsCandidates(conn, org, sub)

    def live_graph(org: str, sub: str) -> PostgresVectorGraph:
        """The live facts graph for one requester (no write policy needed for reads)."""
        return PostgresVectorGraph(conn, org, sub)

    app = FastAPI(title="Praxis Candidate API", version="1")

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

    def active_org(
        principal: Principal = Depends(current_user),
        x_praxis_org: str | None = Header(default=None),
    ) -> str:
        """Resolve + authorize the requester's active org (from ``X-Praxis-Org``)."""
        org = x_praxis_org or "default"
        if not orgs_store.is_member(org, principal.sub):
            raise HTTPException(status_code=403, detail=f"not a member of org {org!r}")
        return org

    @app.get("/health")
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
        keep_id = body.get("keepId")
        if not keep_id:
            raise HTTPException(status_code=400, detail="keepId or customText required")
        try:
            return facade.resolve(pair_id, str(keep_id))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown contradiction {pair_id}")

    # --- graph snapshot (the dashboard graph view) -------------------------
    @app.get("/graph")
    def graph(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """The live graph: active facts + their edges, the same rows retrieval reads."""
        g = live_graph(org, principal.sub)
        return {"graph": graph_adapter.graph_from_facts(g.active_facts(), g.active_edges())}

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
        return {"deleted": name}

    # --- eval cache (per case) + loading into the live graph ---------------
    @app.get("/evals/cached")
    def cached_eval_cases(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Eval case ids that currently have cached data (drives the UI status dots)."""
        entries = live_graph(org, principal.sub).list_caches("eval:")
        return {"cached": [e["key"].split("eval:", 1)[1] for e in entries]}

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
            regenerated.append(cid)
        return regenerated, from_cache

    @app.post("/evals/regenerate")
    def regenerate_evals(
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
    def load_evals(
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
    def add_insight(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Ingest a fully-approved insight into the live ``facts`` store.

        Force-overwrite policy: a contradicting add supersedes the conflicting
        fact in place. The in-chat confirmation is the human gate, so the insight
        enters ``active`` at full credibility.
        """
        insight = (body.get("insight") or "").strip()
        if not insight:
            raise HTTPException(status_code=400, detail="insight required")
        graph = PostgresVectorGraph(
            conn,
            org,
            principal.sub,
            policy=[Redactor(), Deduper(), ConflictOverwriter(llm=OpenRouterLlm())],
        )
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        before = graph.search(insight, top_k=1, state=None)
        ingestor.ingest(insight, state="active")  # human-gated -> live knowledge
        after = graph.search(insight, top_k=1, state=None)
        prior = before[0].fact if before else None
        top = after[0].fact if after else None
        if prior is not None and top is not None and prior.id == top.id:
            action = "overwrote" if top.text != prior.text else "merged"
        else:
            action = "added"
        return {
            "summary": f"{action} insight",
            "action": action,
            "id": top.id if top is not None else None,
        }

    @app.get("/context")
    def get_context(
        query: str = "",
        top_k: int = 8,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Return active-fact context relevant to ``query`` (the eval read path)."""
        graph = live_graph(org, principal.sub)
        _, _, reader = build_trio(graph=graph, llm=None)
        hits = graph.search(query, top_k=top_k) if query.strip() else []
        return {
            "context": reader.read(query),
            "hits": [
                {"id": h.fact.id, "text": h.fact.text, "score": h.score} for h in hits
            ],
        }

    return app


app = create_app()
