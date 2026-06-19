"""FastAPI server implementing the candidate-api-v1 contract over the store.

Closes the loop: the React dashboard (with VITE_PRAXIS_API_BASE_URL pointed
here) reads/mutates real, persisted candidates instead of a static fixture, and
its Contradictions tab is fed by the live store via the contradiction adapter.

Run: uv run python -m knowledge.serve   (serves on http://localhost:8000)
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from knowledge.serve import contradiction_adapter, db
from knowledge.serve.store import CandidateStore, PromotionError, contradiction_ids


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

    # The dashboard dev server runs on a different localhost port.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "candidates": len(store.list())}

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

    return app


app = create_app()
