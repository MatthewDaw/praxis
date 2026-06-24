"""Seed the mock/demo fixtures into the DB as default ``snapshot:*`` caches.

The dashboard used to ship a "Mock fixtures (evals)" data source that read JSON
fixtures generated from ``frontend/mock_data.py``. That source was removed; the
only live data source is now Postgres. This script ports the same fixtures into
the database as ready-to-load **snapshots** (rows in ``cached_facts`` /
``cached_fact_edges`` keyed by ``snapshot:<name>``), so a fresh graph can be
populated from the existing Snapshots UI ("Load" -> replace/add) instead of a
parallel mock provider.

Fixtures are split by scope/theme into independently-loadable snapshots:

    snapshot:monica-frontend   frontend/* candidates (TypeScript, React)
    snapshot:monica-backend    backend/python candidates
    snapshot:monica-infra      infra/ci candidates
    snapshot:monica-nushell    nushell candidates (incl. the cand_6..cand_17 set)
    snapshot:monica-evals      every auto-generated eval-namespace candidate
    snapshot:monica-all        every candidate above in one snapshot (all edges)

Snapshots are tenant-scoped ``(org_id, user_id)`` just like the live graph, so
the seed targets one tenant per run (default ``default`` / ``dev-user`` — the
identity the API uses when ``PRAXIS_AUTH_DISABLED=1``). Point ``--org`` /
``--user`` at a real Cognito tenant to seed it there.

Idempotent: each snapshot's cache rows are deleted and rewritten on every run.

Usage::

    .venv/Scripts/python.exe -m knowledge.serve.seed_snapshots
    .venv/Scripts/python.exe -m knowledge.serve.seed_snapshots --org acme --user <sub>
    .venv/Scripts/python.exe -m knowledge.serve.seed_snapshots --no-embeddings
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import _fit
from knowledge.serve import db

# ``frontend/mock_data.py`` is the source of truth for the fixtures. It is a
# top-level module under ``frontend/`` (not a package), so add that dir to the
# path before importing it.
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# Candidate dict keys that map to dedicated ``facts`` columns (or to edges) and
# therefore must NOT be duplicated into the ``meta`` jsonb blob.
_COLUMN_KEYS = {"id", "content", "state", "confidence", "scope", "category", "contradiction_ids"}

# Snapshot name (without the ``snapshot:`` prefix) per theme; ``--prefix``
# overrides the ``demo-`` part.
_GROUP_LABELS = {
    "frontend": "frontend",
    "backend": "backend",
    "infra": "infra",
    "nushell": "nushell",
    "evals": "evals",
    "misc": "misc",
}


def _load_fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Import the mock fixtures, returning ``(candidates, edges)``."""
    if str(_FRONTEND_DIR) not in sys.path:
        sys.path.insert(0, str(_FRONTEND_DIR))
    from mock_data import get_mock_candidate_dicts, get_mock_graph_dict  # type: ignore

    candidates = get_mock_candidate_dicts()
    edges = get_mock_graph_dict().get("edges", [])
    return candidates, edges


def _group_of(cand: dict[str, Any]) -> str:
    """Theme bucket for a candidate, derived from its scope (then id fallback).

    Scope's top segment maps directly (``frontend/react`` -> ``frontend``,
    ``eval/eval`` -> ``evals``). The scope-less ``cand_6``..``cand_17`` narrative
    rows are the nushell pipeline story, so they fall into ``nushell``.
    """
    scope = cand.get("scope")
    if scope:
        top = str(scope).split("/", 1)[0]
        if top == "eval":
            return "evals"
        if top in _GROUP_LABELS:
            return top
        return "misc"
    cid = str(cand.get("id", ""))
    if cid.startswith("cand_"):
        suffix = cid.split("_", 1)[1]
        if suffix.isdigit() and 6 <= int(suffix) <= 17:
            return "nushell"
    return "misc"


def _meta_of(cand: dict[str, Any]) -> dict[str, Any]:
    """Everything not stored in a dedicated column goes into ``meta`` jsonb.

    ``title`` and ``auditTrail`` are what ``fact_to_candidate`` reads back to
    rebuild the dashboard candidate; the rest (trace/session ids, confidence
    breakdown, eval-case linkage) round-trips for fidelity.
    """
    return {k: v for k, v in cand.items() if k not in _COLUMN_KEYS and k != "provenance"}


def _delete_snapshot(conn: Any, org: str, user: str, cache_key: str) -> None:
    """Drop a snapshot's cache rows (edges first for the FK) for re-seed."""
    conn.execute(
        "DELETE FROM cached_fact_edges WHERE org_id=%s AND user_id=%s AND cache_key=%s",
        (org, user, cache_key),
    )
    conn.execute(
        "DELETE FROM cached_facts WHERE org_id=%s AND user_id=%s AND cache_key=%s",
        (org, user, cache_key),
    )


def _insert_fact(
    conn: Any,
    org: str,
    user: str,
    cache_key: str,
    cand: dict[str, Any],
    embedding: Any | None,
) -> None:
    conn.execute(
        "INSERT INTO cached_facts "
        "(id, org_id, user_id, text, source, confidence, scope, category, state, "
        " embedding, meta, cache_key, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "        COALESCE(%s::timestamptz, now()))",
        (
            cand["id"],
            org,
            user,
            str(cand.get("content", "")),
            cand.get("provenance"),
            cand.get("confidence"),
            cand.get("scope"),
            cand.get("category"),
            cand.get("state", "proposed"),
            embedding,
            json.dumps(_meta_of(cand)),
            cache_key,
            cand.get("createdAt"),
        ),
    )


def _insert_edge(
    conn: Any, org: str, user: str, cache_key: str, src: str, dst: str, kind: str
) -> None:
    conn.execute(
        "INSERT INTO cached_fact_edges (org_id, user_id, cache_key, src_id, dst_id, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (org, user, cache_key, src, dst, kind),
    )


def _make_embedder(enabled: bool) -> Any | None:
    """Real OpenRouter embedder when enabled + keyed, else None (NULL vectors).

    NULL embeddings still render in the graph view and Contradictions tab; only
    pgvector similarity search / MCP recall skip them (``embedding IS NOT NULL``).
    """
    if not enabled:
        return None
    if not os.getenv("OPENROUTER_API_KEY"):
        print(
            "  note: OPENROUTER_API_KEY unset — seeding with NULL embeddings "
            "(graph view works; similarity search will skip these facts).",
            file=sys.stderr,
        )
        return None
    from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder

    return OpenRouterEmbedder()


def seed(
    *,
    org: str = "default",
    user: str = "dev-user",
    prefix: str = "monica-",
    combined: str = "all",
    embeddings: bool = True,
    dsn: str | None = None,
) -> dict[str, int]:
    """Seed the per-theme + combined snapshots for one tenant.

    ``combined`` is the name (after ``prefix``) of an extra snapshot holding
    every candidate and every edge in one place; pass ``""`` to skip it.
    Returns ``{snapshot_name: count}``.
    """
    candidates, edges = _load_fixtures()
    embedder = _make_embedder(embeddings)

    # Embed each candidate at most once, then reuse the vector for both its theme
    # snapshot and the combined snapshot.
    vecs: dict[str, Any] = {}
    if embedder is not None:
        for cand in candidates:
            vecs[str(cand["id"])] = _fit(embedder.embed_one(str(cand.get("content", ""))))

    # Bucket candidates by theme; remember each id's group so we only keep edges
    # whose endpoints live in the same theme snapshot (cross-theme edges drop,
    # but all of them survive in the combined snapshot).
    groups: dict[str, list[dict[str, Any]]] = {}
    group_of_id: dict[str, str] = {}
    for cand in candidates:
        group = _group_of(cand)
        groups.setdefault(group, []).append(cand)
        group_of_id[str(cand["id"])] = group

    counts: dict[str, int] = {}
    conn = db.connect(dsn)
    try:
        for group, rows in sorted(groups.items()):
            name = f"{prefix}{_GROUP_LABELS.get(group, group)}"
            cache_key = f"snapshot:{name}"
            _delete_snapshot(conn, org, user, cache_key)
            for cand in rows:
                _insert_fact(conn, org, user, cache_key, cand, vecs.get(str(cand["id"])))
            counts[name] = len(rows)
            print(f"  seeded snapshot:{name} ({len(rows)} facts)")

        dropped = 0
        for edge in edges:
            src, dst, kind = edge["src"], edge["dst"], edge.get("kind", "contradiction")
            g_src, g_dst = group_of_id.get(src), group_of_id.get(dst)
            if g_src is not None and g_src == g_dst:
                name = f"{prefix}{_GROUP_LABELS.get(g_src, g_src)}"
                _insert_edge(conn, org, user, f"snapshot:{name}", src, dst, kind)
            else:
                dropped += 1
        if dropped:
            print(f"  note: {dropped} cross-theme edge(s) kept only in the combined snapshot")

        if combined:
            name = f"{prefix}{combined}"
            cache_key = f"snapshot:{name}"
            _delete_snapshot(conn, org, user, cache_key)
            for cand in candidates:
                _insert_fact(conn, org, user, cache_key, cand, vecs.get(str(cand["id"])))
            for edge in edges:
                _insert_edge(
                    conn, org, user, cache_key,
                    edge["src"], edge["dst"], edge.get("kind", "contradiction"),
                )
            counts[name] = len(candidates)
            print(f"  seeded snapshot:{name} ({len(candidates)} facts, all edges)")
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="default", help="org_id to seed (default: default)")
    parser.add_argument("--user", default="dev-user", help="user_id to seed (default: dev-user)")
    parser.add_argument(
        "--prefix", default="monica-", help="snapshot name prefix (default: monica-)"
    )
    parser.add_argument(
        "--combined",
        default="all",
        help="name (after prefix) of the combined all-in-one snapshot; '' to skip (default: all)",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="insert NULL embeddings instead of calling the OpenRouter embedder",
    )
    parser.add_argument("--dsn", default=None, help="explicit Postgres DSN (else resolved from env)")
    args = parser.parse_args(argv)

    print(f"seeding demo snapshots for tenant ({args.org!r}, {args.user!r})")
    counts = seed(
        org=args.org,
        user=args.user,
        prefix=args.prefix,
        combined=args.combined,
        embeddings=not args.no_embeddings,
        dsn=args.dsn,
    )
    total = sum(counts.values())
    print(f"done: {len(counts)} snapshots, {total} facts total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
