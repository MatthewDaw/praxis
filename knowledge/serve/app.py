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

from knowledge.knowledge_graph.knowledge_graph_variants.org_source_reader import (  # noqa: E402
    OrgSourceReader,
)
from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
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
    # Bind the auth dependency to this connection so it can also resolve API keys
    # (X-Praxis-Key) in addition to the Cognito Bearer JWT / dev seam.
    current_user = make_current_user(conn)

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
        return {"deleted": name}

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
    def fold_in(
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

        Non-destructive resolution: a contradicting add lands as a fresh ``active``
        fact and each conflicting fact is rejected (its text preserved) and linked
        back via a ``contradicted_by`` edge, rather than being overwritten in place.
        The in-chat confirmation is the human gate, so the insight enters ``active``
        at full credibility.
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
        # Non-destructive resolution: an approved contradiction is always a fresh
        # add (the new fact gets a new id), so a same-id top means a dedup merge
        # bumped the existing fact; otherwise the insight was added (possibly
        # rejecting + linking conflicting facts).
        if prior is not None and top is not None and prior.id == top.id:
            action = "merged"
        else:
            action = "added"
        return {
            "summary": f"{action} insight",
            "action": action,
            "id": top.id if top is not None else None,
        }

    @app.post("/ingest")
    def ingest_documents(
        body: dict[str, Any] = Body(default={}),
        principal: Principal = Depends(current_user),
        org: str = Depends(active_org),
    ) -> dict[str, Any]:
        """Batch-ingest raw documents through the tenant's distillation pipeline.

        Body: ``{"documents": [{"text": str, "source": str|null}],
        "state": "active"|"proposed"}`` (state defaults to "active"). Each
        document is run through the same ingestor that distills raw text into
        facts (``build_trio`` over the tenant's live graph), at the given state —
        this is the pipeline path, NOT the no-pipeline ``/candidates`` insert.

        Returns ``{"results": [{"id": str|null, "action": str}], "count": int}``;
        one result per input document. ``id`` is the top matching fact after that
        document's distillation (best-effort), ``action`` is ``"ingested"``.
        """
        documents = body.get("documents")
        if not isinstance(documents, list) or not documents:
            raise HTTPException(status_code=400, detail="documents must be a non-empty list")
        state = str(body.get("state") or "active").strip().lower()
        if state not in ("active", "proposed"):
            raise HTTPException(status_code=400, detail="state must be 'active' or 'proposed'")

        graph = PostgresVectorGraph(
            conn,
            org,
            principal.sub,
            policy=[Redactor(), Deduper(), ConflictOverwriter(llm=OpenRouterLlm())],
        )
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        results: list[dict[str, Any]] = []
        for doc in documents:
            if not isinstance(doc, dict):
                raise HTTPException(status_code=400, detail="each document must be an object")
            text = (doc.get("text") or "").strip()
            if not text:
                raise HTTPException(status_code=400, detail="each document needs non-empty text")
            ingestor.ingest(text, state=state)
            # Best-effort provenance back to the caller: the top fact the just-
            # ingested text now matches (ids are per-fact, a doc distills to many).
            hits = graph.search(text, top_k=1, state=None)
            top_id = hits[0].fact.id if hits else None
            results.append({"id": top_id, "action": "ingested"})
        return {"results": results, "count": len(results)}

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
                {
                    "id": h.fact.id,
                    "text": h.fact.text,
                    "score": h.score,
                    "source": getattr(h.fact, "source", None),
                    "scope": getattr(h.fact, "scope", None),
                    "category": getattr(h.fact, "category", None),
                }
                for h in hits
            ],
        }

    return app


app = create_app()
