"""One-off, idempotent migration onto the unified ``facts`` spine.

Run once against the local Postgres (5433):

    .venv/Scripts/python.exe -m migrations.m2026_06_23_unify_facts

It does three guarded, re-runnable steps:

1. Lift every ``candidates`` row under ``(praxis, *)`` into a ``facts`` row under
   the real ``(praxis, USER)`` tenant (id reused), embedding ``doc.content`` and
   recreating contradiction links as ``fact_edges``.
2. Re-tenant the orphaned ``(default, dev-user)`` facts written during the
   broken-auth window onto ``(praxis, USER)``, skipping PK collisions.
3. Drop the now-dead ``candidates`` table.

Each step prints a summary line. Safe to re-run: inserts use
``ON CONFLICT DO NOTHING`` and the re-tenant skips ids that already exist.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    _fit,
)
from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder
from knowledge.serve.db import connect

# Target tenant: the real praxis user.
ORG = "praxis"
USER = "24782438-1091-70d3-3e55-f9f3510b2aba"

# The broken-auth-window orphans landed under this tenant.
ORPHAN_ORG = "default"
ORPHAN_USER = "dev-user"


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)
    ).fetchone()
    return bool(row and row[0])


def _migrate_candidates(conn, embedder: OpenRouterEmbedder) -> int:
    """Step 1: candidates(praxis, *) -> facts(praxis, USER) + contradiction edges."""
    if not _table_exists(conn, "candidates"):
        print("step 1: no `candidates` table — migrated 0")
        return 0

    rows = conn.execute(
        "SELECT doc FROM candidates WHERE org_id = %s", (ORG,)
    ).fetchall()

    migrated = 0
    # Track which doc ids made it into facts so we only wire up edges between
    # facts that now actually exist (FK + plan requirement).
    docs: list[dict] = []
    for (doc,) in rows:
        if not isinstance(doc, dict):
            continue
        fact_id = doc.get("id")
        if not fact_id:
            continue
        docs.append(doc)

        content = str(doc.get("content") or "")
        meta = {
            "title": doc.get("title"),
            "auditTrail": doc.get("auditTrail") or doc.get("audit_trail") or [],
        }
        embedding = _fit(embedder.embed_one(content)) if content else None

        result = conn.execute(
            """
            INSERT INTO facts
                (id, org_id, user_id, text, source, confidence, state,
                 observation_count, embedding, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
            ON CONFLICT (org_id, user_id, id) DO NOTHING
            RETURNING id
            """,
            (
                fact_id,
                ORG,
                USER,
                content,
                doc.get("provenance"),
                doc.get("confidence"),
                doc.get("state") or "proposed",
                embedding,
                Jsonb(meta),
            ),
        )
        if result.fetchone() is not None:
            migrated += 1

    # Recreate contradiction links now that all facts have been inserted.
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM facts WHERE org_id = %s AND user_id = %s", (ORG, USER)
        ).fetchall()
    }
    edges = 0
    for doc in docs:
        fact_id = doc.get("id")
        rivals = doc.get("contradiction_ids") or doc.get("contradictions") or []
        for rival in rivals:
            if fact_id not in existing or rival not in existing:
                continue
            res = conn.execute(
                """
                INSERT INTO fact_edges (org_id, user_id, src_id, dst_id, kind)
                VALUES (%s, %s, %s, %s, 'contradiction')
                ON CONFLICT DO NOTHING
                RETURNING src_id
                """,
                (ORG, USER, fact_id, rival),
            )
            if res.fetchone() is not None:
                edges += 1

    print(f"step 1: migrated {migrated} candidate(s) -> facts; created {edges} edge(s)")
    return migrated


def _retenant_orphans(conn) -> int:
    """Step 2: re-tenant (default, dev-user) facts -> (praxis, USER).

    Guard against PK collisions: only move ids that don't already exist under
    the target tenant (an UPDATE ... WHERE NOT EXISTS). Colliding orphans are
    left in place rather than clobbering the canonical praxis row.
    """
    if not _table_exists(conn, "facts"):
        print("step 2: no `facts` table — re-tenanted 0")
        return 0

    result = conn.execute(
        """
        UPDATE facts AS o
           SET org_id = %s, user_id = %s
         WHERE o.org_id = %s AND o.user_id = %s
           AND NOT EXISTS (
               SELECT 1 FROM facts AS t
                WHERE t.org_id = %s AND t.user_id = %s AND t.id = o.id
           )
        """,
        (ORG, USER, ORPHAN_ORG, ORPHAN_USER, ORG, USER),
    )
    moved = result.rowcount

    remaining = conn.execute(
        "SELECT count(*) FROM facts WHERE org_id = %s AND user_id = %s",
        (ORPHAN_ORG, ORPHAN_USER),
    ).fetchone()[0]
    print(
        f"step 2: re-tenanted {moved} orphan fact(s) -> ({ORG}, USER); "
        f"{remaining} left (PK collision, skipped)"
    )
    return moved


def _drop_candidates(conn) -> None:
    """Step 3: drop the dead candidates table."""
    existed = _table_exists(conn, "candidates")
    conn.execute("DROP TABLE IF EXISTS candidates")
    print(f"step 3: dropped `candidates` table ({'existed' if existed else 'absent'})")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    embedder = OpenRouterEmbedder()
    with connect() as conn:  # registers the pgvector adapter
        _migrate_candidates(conn, embedder)
        _retenant_orphans(conn)
        _drop_candidates(conn)
    print("migration complete.")


if __name__ == "__main__":
    main()
