"""FastAPI server implementing the candidate-api-v1 contract over the store.

Closes the loop: the React dashboard (with VITE_PRAXIS_API_BASE_URL pointed
here) reads/mutates real, persisted candidates instead of a static fixture, and
its Contradictions tab is fed by the live store via the contradiction adapter.

Every data route hard-requires a valid Cognito JWT (the ``current_user``
dependency) and resolves the active org from the ``X-Praxis-Org`` header; the
caller must be a member of that org. ``/health`` stays open. When no DSN
resolves the in-memory JSON store is used and there is no orgs store, so the
membership check is skipped (offline/dev mode).

Run: uv run python -m knowledge.serve   (serves on http://localhost:8000)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ConflictOverwriter,
    Deduper,
    Redactor,
)
from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm
from knowledge.serve import contradiction_adapter, db, graph_adapter
from knowledge.serve.auth import Principal, current_user
from knowledge.serve.orgs_store import OrgsStore
from knowledge.serve.regenerate import (
    PipelineConfig,
    RegenerateUnavailableError,
    regenerate_candidates,
)
from knowledge.serve.store import CandidateStore, PromotionError, contradiction_ids
from knowledge.wiring import build_trio

METRICS_FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs" / "integration" / "fixtures" / "eval-metrics.json"
)
_DEFAULT_CORS_REGEX = (
    r"(http://(localhost|127\.0\.0\.1):\d+|https://[\w-]+\.onrender\.com"
    r"|https://[\w-]+\.cloudfront\.net|https://[\w-]+\.awsapprunner\.com"
    r"|https://[\w-]+\.praxiskg\.com)"
)


def _cors_origin_regex() -> str:
    custom = os.getenv("PRAXIS_CORS_ORIGIN_REGEX", "").strip()
    return custom or _DEFAULT_CORS_REGEX


def _store_type(store: Any) -> str:
    from knowledge.serve.postgres_store import PostgresCandidateStore

    return "postgres" if isinstance(store, PostgresCandidateStore) else "json"


def default_store() -> Any:
    """Pick a backing store: Postgres when a DSN resolves, else the JSON file.

    Tenancy is now per-request (resolved from the verified principal + the
    ``X-Praxis-Org`` header), so the Postgres store is built without tenant
    context and serves all users from a single connection.
    """
    if db.resolve_dsn() is not None:
        from knowledge.serve.postgres_store import PostgresCandidateStore

        return PostgresCandidateStore()
    return CandidateStore()


def create_app(store: Any | None = None) -> FastAPI:
    store = store if store is not None else default_store()
    # An OrgsStore only exists for the Postgres path (it shares the connection);
    # without one the routes skip the membership check (offline/dev mode).
    orgs_store: OrgsStore | None = None
    if _store_type(store) == "postgres":
        orgs_store = OrgsStore(store._conn)

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
        """Resolve + authorize the requester's active org for a data route.

        The org comes from the ``X-Praxis-Org`` header. When an OrgsStore is
        present (Postgres path) the principal must be a member, else 403. With
        no orgs store (in-memory/offline) any org string is accepted as-is.
        """
        org = x_praxis_org or "default"
        if orgs_store is not None:
            if not orgs_store.is_member(org, principal.sub):
                raise HTTPException(status_code=403, detail=f"not a member of org {org!r}")
        return org

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "store": _store_type(store),
        }

    @app.get("/me")
    def me(principal: Principal = Depends(current_user)) -> dict[str, Any]:
        orgs = orgs_store.list_orgs(principal.sub) if orgs_store is not None else []
        return {"sub": principal.sub, "email": principal.email, "orgs": orgs}

    @app.post("/orgs")
    def create_org(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        if orgs_store is None:
            raise HTTPException(status_code=503, detail="orgs require a database")
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
        if orgs_store is None:
            raise HTTPException(status_code=503, detail="orgs require a database")
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
        if orgs_store is None:
            raise HTTPException(status_code=503, detail="orgs require a database")
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

    @app.get("/candidates")
    def list_candidates(
        state: str | None = None,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> list[dict[str, Any]]:
        return store.list(org, principal.sub, state)

    @app.get("/candidates/{cid}")
    def get_candidate(
        cid: str,
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        c = store.get(org, principal.sub, cid)
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
            return store.promote(org, principal.sub, cid, body.get("targetState"))
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
            return store.reject(org, principal.sub, cid, body.get("reason"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    @app.post("/candidates")
    def create_candidate(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        try:
            return store.create(org, principal.sub, body)
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
            return store.update(org, principal.sub, cid, body)
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
            store.delete(org, principal.sub, cid)
            return {"deleted": cid}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    @app.get("/contradictions")
    def contradictions(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> list[dict[str, Any]]:
        return contradiction_adapter.serialize_pairs(store.list(org, principal.sub))

    @app.get("/graph")
    def graph(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Return a graph snapshot derived from the currently served candidates."""
        return {"graph": graph_adapter.graph_from_candidates(store.list(org, principal.sub))}

    @app.post("/contradictions/{pair_id}/resolve")
    def resolve(
        pair_id: str,
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        keep_id = body.get("keepId")
        if not keep_id:
            raise HTTPException(status_code=400, detail="keepId required")
        try:
            return store.resolve(org, principal.sub, pair_id, str(keep_id))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {keep_id}")

    @app.post("/contradictions/detect")
    def detect(
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Re-run live contradiction detection over the store (best-effort)."""
        listed = store.list(org, principal.sub)
        pairs = contradiction_adapter.detect(listed)
        by_id = {c["id"]: c for c in listed}
        touched: set[str] = set()
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                c = by_id.get(x)
                if c is not None:
                    links = set(contradiction_ids(c))
                    links.add(y)
                    c["contradiction_ids"] = sorted(links)
                    touched.add(x)
        # Persist the new links: per-row upsert for Postgres, file flush for JSON.
        if hasattr(store, "_upsert"):
            for x in touched:
                store._upsert(org, principal.sub, by_id[x])
        elif hasattr(store, "_persist"):
            store._persist()
        return {"detected_pairs": len(pairs)}

    @app.post("/evals/regenerate")
    def regenerate_evals(
        body: dict[str, Any] | None = Body(default=None),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Run eval-backed regeneration and replace pipeline-owned candidates."""
        try:
            config = PipelineConfig.from_body(body)
            result = regenerate_candidates(config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RegenerateUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        inserted = store.replace_pipeline_candidates(org, principal.sub, result.candidates)
        return {
            "preset": result.preset,
            "cases_run": result.cases_run,
            "cases_skipped": result.cases_skipped,
            "insights_generated": len(result.insights),
            "candidates_inserted": inserted,
            "ran_at": result.ran_at,
            "eval_results": result.eval_results,
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

    @app.post("/evals/run")
    def run_eval_scope(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        """Run every case under the selected scopes (seed -> agent -> grade)."""
        from knowledge.serve.eval_runner import run_scopes

        raw_scopes = body.get("scopes")
        if isinstance(raw_scopes, list):
            scopes = [str(s) for s in raw_scopes if str(s).strip()]
        elif body.get("scope"):
            scopes = [str(body.get("scope"))]
        else:
            scopes = None
        backend = str(body.get("backend") or "openrouter").strip()
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
        limit = body.get("limit")
        force = bool(body.get("force"))
        try:
            return run_scopes(scopes, backend, overrides, int(limit) if limit else None, force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/insights")
    def add_insight(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Ingest a fully-approved insight into the active ``facts`` store.

        Runs the eval write path (``ingestor.ingest`` -> ``graph.write``) with the
        force-overwrite policy, so a contradicting add supersedes the conflicting
        fact in place. The in-chat confirmation is the human gate, so the insight
        enters ``active`` at full credibility (``confidence`` defaults to 1.0).
        """
        if orgs_store is None:
            raise HTTPException(status_code=503, detail="insights require a database")
        insight = (body.get("insight") or "").strip()
        if not insight:
            raise HTTPException(status_code=400, detail="insight required")
        graph = PostgresVectorGraph(
            store._conn,
            org,
            principal.sub,
            policy=[Redactor(), Deduper(), ConflictOverwriter(llm=OpenRouterLlm())],
        )
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        # state=None: detect a merge/overwrite against any prior fact, including a
        # pending (proposed) one the approved insight supersedes — not just active.
        before = graph.search(insight, top_k=1, state=None)
        ingestor.ingest(insight, state="active")  # human-gated -> live knowledge
        after = graph.search(insight, top_k=1, state=None)
        # Read back the outcome: a stable id with a higher observation_count means
        # a merge/overwrite; a fresh id (or text change) means an add/overwrite.
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
        if orgs_store is None:
            raise HTTPException(status_code=503, detail="context requires a database")
        graph = PostgresVectorGraph(store._conn, org, principal.sub)
        _, _, reader = build_trio(graph=graph, llm=None)
        hits = graph.search(query, top_k=top_k) if query.strip() else []
        return {
            "context": reader.read(query),
            "hits": [
                {"id": h.fact.id, "text": h.fact.text, "score": h.score} for h in hits
            ],
        }

    @app.get("/metrics")
    def eval_metrics(
        principal: Principal = Depends(current_user),
    ) -> dict[str, Any]:
        """Eval metrics contract v1 — fixture until Dominic's harness endpoint ships."""
        if not METRICS_FIXTURE.exists():
            raise HTTPException(status_code=503, detail="metrics fixture unavailable")
        return json.loads(METRICS_FIXTURE.read_text(encoding="utf-8"))

    return app


app = create_app()
