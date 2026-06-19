"""FastAPI server implementing the candidate-api-v1 contract over the store.

Closes the loop: the React dashboard (with VITE_PRAXIS_API_BASE_URL pointed
here) reads/mutates real, persisted candidates instead of a static fixture, and
its Contradictions tab is fed by the live store via the contradiction adapter.

Run: uv run python -m knowledge.serve   (serves on http://localhost:8000)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from knowledge.serve import contradiction_adapter, db
from knowledge.serve.store import CandidateStore, PromotionError, contradiction_ids

METRICS_FIXTURE = (
    Path(__file__).resolve().parents[2] / "docs" / "integration" / "fixtures" / "eval-metrics.json"
)
_DEFAULT_CORS_REGEX = (
    r"(http://(localhost|127\.0\.0\.1):\d+|https://[\w-]+\.onrender\.com)"
)


def _cors_origin_regex() -> str:
    custom = os.getenv("PRAXIS_CORS_ORIGIN_REGEX", "").strip()
    return custom or _DEFAULT_CORS_REGEX


def _store_type(store: Any) -> str:
    from knowledge.serve.postgres_store import PostgresCandidateStore

    return "postgres" if isinstance(store, PostgresCandidateStore) else "json"


def default_store() -> Any:
    """Pick a backing store: Postgres when a DSN resolves, else the JSON file.

    Tenant context comes from the environment (PRAXIS_ORG_ID / PRAXIS_USER_ID /
    PRAXIS_SHARED) so the same server can be scoped to an org or a user.
    """
    if db.resolve_dsn() is not None:
        from knowledge.serve.postgres_store import PostgresCandidateStore

        return PostgresCandidateStore(
            org_id=os.environ.get("PRAXIS_ORG_ID", "default"),
            user_id=os.environ.get("PRAXIS_USER_ID", "default"),
            shared=os.environ.get("PRAXIS_SHARED", "false").lower() in ("1", "true", "yes"),
        )
    return CandidateStore()


def create_app(store: Any | None = None) -> FastAPI:
    store = store if store is not None else default_store()
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

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "candidates": len(store.list()),
            "store": _store_type(store),
        }

    @app.get("/candidates")
    def list_candidates(state: str | None = None) -> list[dict[str, Any]]:
        return store.list(state)

    @app.get("/candidates/{cid}")
    def get_candidate(cid: str) -> dict[str, Any]:
        c = store.get(cid)
        if c is None:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        return c

    @app.post("/candidates/{cid}/promote")
    def promote(cid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            return store.promote(cid, body.get("targetState"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")
        except PromotionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/candidates/{cid}/reject")
    def reject(cid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            return store.reject(cid, body.get("reason"))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {cid}")

    @app.get("/contradictions")
    def contradictions() -> list[dict[str, Any]]:
        return contradiction_adapter.serialize_pairs(store.list())

    @app.post("/contradictions/{pair_id}/resolve")
    def resolve(pair_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        keep_id = body.get("keepId")
        if not keep_id:
            raise HTTPException(status_code=400, detail="keepId required")
        try:
            return store.resolve(pair_id, str(keep_id))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown candidate {keep_id}")

    @app.post("/contradictions/detect")
    def detect() -> dict[str, Any]:
        """Re-run live contradiction detection over the store (best-effort)."""
        pairs = contradiction_adapter.detect(store.list())
        by_id = {c["id"]: c for c in store.list()}
        for a, b in pairs:
            for x, y in ((a, b), (b, a)):
                c = by_id.get(x)
                if c is not None:
                    links = set(contradiction_ids(c))
                    links.add(y)
                    c["contradiction_ids"] = sorted(links)
        store._persist()
        return {"detected_pairs": len(pairs)}

    @app.get("/metrics")
    def eval_metrics() -> dict[str, Any]:
        """Eval metrics contract v1 — fixture until Dominic's harness endpoint ships."""
        if not METRICS_FIXTURE.exists():
            raise HTTPException(status_code=503, detail="metrics fixture unavailable")
        return json.loads(METRICS_FIXTURE.read_text(encoding="utf-8"))

    return app


app = create_app()
