"""Per-viewer mounted-snapshot set: the read-only overlay selection.

A *mount* records that when ``(org_id, user_id)`` does a retrieval read, the
backend should also expose the facts of an org-shared snapshot ``(space,
snapshot)`` — without merging that snapshot into the viewer's working memory.
Mounts are a read-time concern only (see
:mod:`knowledge.knowledge_graph.knowledge_graph_variants.overlay_graph`):
writes/ingest and dumping a snapshot operate on the live ``facts`` table alone,
so a mounted overlay is never carried into a dump.

Snapshots are org-shared, so a viewer may mount any ``(space, snapshot)`` in
their org. Space + snapshot existence is validated by the route before calling
:meth:`mount`; this store is a thin, idempotent persistence layer over the
``mounted_snapshots`` table, mirroring :class:`knowledge.serve.orgs_store.OrgsStore`.
"""

from __future__ import annotations

import psycopg


class MountedStore:
    """Mounted read-only snapshot overlays persisted to ``mounted_snapshots``."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def list(self, org_id: str, user_id: str) -> list[dict]:
        """Return the viewer's mounts as ``{space, snapshot}`` rows."""
        rows = self._conn.execute(
            """
            SELECT space, snapshot
            FROM mounted_snapshots
            WHERE org_id = %s AND user_id = %s
            ORDER BY space, snapshot
            """,
            (org_id, user_id),
        ).fetchall()
        return [{"space": r[0], "snapshot": r[1]} for r in rows]

    def mount(self, org_id: str, user_id: str, space: str, snapshot: str) -> None:
        """Add a mount (idempotent). Validation is the caller's responsibility."""
        self._conn.execute(
            """
            INSERT INTO mounted_snapshots (org_id, user_id, space, snapshot)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, user_id, space, snapshot) DO NOTHING
            """,
            (org_id, user_id, space, snapshot),
        )

    def unmount(self, org_id: str, user_id: str, space: str, snapshot: str) -> None:
        """Remove one viewer's mount (no-op if it was not mounted)."""
        self._conn.execute(
            """
            DELETE FROM mounted_snapshots
            WHERE org_id = %s AND user_id = %s AND space = %s AND snapshot = %s
            """,
            (org_id, user_id, space, snapshot),
        )

    def unmount_all(self, org_id: str, space: str, snapshot: str) -> None:
        """Drop EVERY viewer's mount of ``(space, snapshot)`` in the org.

        Called when a snapshot is deleted (directly or by dropping its space) so a
        dangling mount can never reference a snapshot that no longer exists.
        """
        self._conn.execute(
            """
            DELETE FROM mounted_snapshots
            WHERE org_id = %s AND space = %s AND snapshot = %s
            """,
            (org_id, space, snapshot),
        )

    def unmount_space(self, org_id: str, space: str) -> None:
        """Drop EVERY viewer's mount of any snapshot in ``space`` (space deletion)."""
        self._conn.execute(
            "DELETE FROM mounted_snapshots WHERE org_id = %s AND space = %s",
            (org_id, space),
        )

    def repoint(
        self, org_id: str, space: str, snapshot: str, new_snapshot: str
    ) -> None:
        """Re-point every viewer's mount of ``(space, snapshot)`` to ``new_snapshot``.

        Called when a snapshot is renamed within its space so a mounted overlay
        keeps being read after the rename.
        """
        self._conn.execute(
            """
            UPDATE mounted_snapshots SET snapshot = %s
            WHERE org_id = %s AND space = %s AND snapshot = %s
            """,
            (new_snapshot, org_id, space, snapshot),
        )
