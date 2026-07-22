"""Seed the mock/demo fixtures into the DB as snapshots inside a demo space.

The dashboard used to ship a "Mock fixtures (evals)" data source that read JSON
fixtures generated from ``frontend/mock_data.py``. That source was removed; the
only live data source is now Postgres. This script ports the same fixtures into
the database as ready-to-load **snapshots** (rows in ``snapshots`` /
``snapshot_edges``), so a fresh graph can be populated from the Snapshots UI
("Load" -> replace/add) instead of a parallel mock provider.

Under the org -> space -> snapshot tenancy model a snapshot is an ORG-SHARED
named graph inside a space; it carries no ``user_id`` and no ``shared`` flag.
Fixtures are split by scope/theme into independently-loadable snapshots, all
living in one demo ``--space`` (default ``monica``) with BARE snapshot names:

    space=monica snapshot=frontend   frontend/* candidates (TypeScript, React)
    space=monica snapshot=backend    backend/python candidates
    space=monica snapshot=infra      infra/ci candidates
    space=monica snapshot=nushell    nushell candidates (incl. the cand_6..cand_17 set)
    space=monica snapshot=evals      every auto-generated eval-namespace candidate
    space=monica snapshot=all        every candidate above in one snapshot (all edges)

Snapshots are org-scoped, so the seed targets one org per run (default
``default``). Point ``--org`` / ``--space`` at a real tenant/space to seed there.

Idempotent: each snapshot's rows are deleted and rewritten on every run.

Usage::

    .venv/Scripts/python.exe -m knowledge.serve.seed_snapshots
    .venv/Scripts/python.exe -m knowledge.serve.seed_snapshots --org acme --space demo
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

# Known theme buckets; a candidate's scope top-segment is kept as its own snapshot
# name when it names one of these (otherwise it falls into ``misc``).
_GROUP_NAMES = frozenset({"frontend", "backend", "infra", "nushell", "evals", "misc"})


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
        if top in _GROUP_NAMES:
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


def _delete_snapshot(conn: Any, org: str, space: str, snapshot: str) -> None:
    """Drop a snapshot's rows (edges first for the FK) for re-seed."""
    conn.execute(
        "DELETE FROM snapshot_edges WHERE org_id=%s AND space=%s AND snapshot=%s",
        (org, space, snapshot),
    )
    conn.execute(
        "DELETE FROM snapshots WHERE org_id=%s AND space=%s AND snapshot=%s",
        (org, space, snapshot),
    )


def _insert_fact(
    conn: Any,
    org: str,
    space: str,
    snapshot: str,
    cand: dict[str, Any],
    embedding: Any | None,
) -> None:
    conn.execute(
        "INSERT INTO snapshots "
        "(id, org_id, space, snapshot, text, source, confidence, scope, category, state, "
        " embedding, meta, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "        COALESCE(%s::timestamptz, now()))",
        (
            cand["id"],
            org,
            space,
            snapshot,
            str(cand.get("content", "")),
            cand.get("provenance"),
            cand.get("confidence"),
            cand.get("scope"),
            cand.get("category"),
            cand.get("state", "proposed"),
            embedding,
            json.dumps(_meta_of(cand)),
            cand.get("createdAt"),
        ),
    )


def _insert_edge(
    conn: Any, org: str, space: str, snapshot: str, src: str, dst: str, kind: str
) -> None:
    conn.execute(
        "INSERT INTO snapshot_edges (org_id, space, snapshot, src_id, dst_id, kind) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (org, space, snapshot, src, dst, kind),
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
    space: str = "monica",
    combined: str = "all",
    embeddings: bool = True,
    dsn: str | None = None,
) -> dict[str, int]:
    """Seed the per-theme + combined snapshots into one org-shared space.

    ``combined`` is the bare name of an extra snapshot holding every candidate
    and every edge in one place; pass ``""`` to skip it. Returns
    ``{snapshot_name: count}``.
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
            name = group
            _delete_snapshot(conn, org, space, name)
            for cand in rows:
                _insert_fact(conn, org, space, name, cand, vecs.get(str(cand["id"])))
            counts[name] = len(rows)
            print(f"  seeded {space}/{name} ({len(rows)} facts)")

        dropped = 0
        for edge in edges:
            src, dst, kind = edge["src"], edge["dst"], edge.get("kind", "contradiction")
            g_src, g_dst = group_of_id.get(src), group_of_id.get(dst)
            if g_src is not None and g_src == g_dst:
                _insert_edge(conn, org, space, g_src, src, dst, kind)
            else:
                dropped += 1
        if dropped:
            print(f"  note: {dropped} cross-theme edge(s) kept only in the combined snapshot")

        if combined:
            _delete_snapshot(conn, org, space, combined)
            for cand in candidates:
                _insert_fact(conn, org, space, combined, cand, vecs.get(str(cand["id"])))
            for edge in edges:
                _insert_edge(
                    conn, org, space, combined,
                    edge["src"], edge["dst"], edge.get("kind", "contradiction"),
                )
            counts[combined] = len(candidates)
            print(f"  seeded {space}/{combined} ({len(candidates)} facts, all edges)")
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="default", help="org_id to seed (default: default)")
    parser.add_argument(
        "--space", default="monica", help="org-shared space to seed into (default: monica)"
    )
    parser.add_argument(
        "--combined",
        default="all",
        help="bare name of the combined all-in-one snapshot; '' to skip (default: all)",
    )
    parser.add_argument(
        "--no-embeddings",
        action="store_true",
        help="insert NULL embeddings instead of calling the OpenRouter embedder",
    )
    parser.add_argument("--dsn", default=None, help="explicit Postgres DSN (else resolved from env)")
    args = parser.parse_args(argv)

    print(f"seeding demo snapshots into space ({args.org!r}, {args.space!r})")
    counts = seed(
        org=args.org,
        space=args.space,
        combined=args.combined,
        embeddings=not args.no_embeddings,
        dsn=args.dsn,
    )
    total = sum(counts.values())
    print(f"done: {len(counts)} snapshots, {total} facts total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
