"""App-level spaces store: org-shared, named project folders.

A *space* is an org-shared "project folder" that holds a collection of snapshots
for one project. It is NOT partitioned by user: any member of the org can read
every space and every snapshot in it (think of it like a folder on disk any user
can open). The ``spaces`` table is therefore org-level, keyed
``(org_id, space_id)`` with no owner.

Backed by the ``spaces`` table (see migrations/), reusing a passed-in psycopg
connection (the same autocommit connection the candidate store uses), mirroring
:class:`knowledge.serve.orgs_store.OrgsStore`.
"""

from __future__ import annotations

import psycopg

from knowledge.serve.reserved_names import is_reserved_space_id


class SpacesStore:
    """Org-shared named spaces persisted to the ``spaces`` table."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # --- mutations ---------------------------------------------------------
    def create_space(self, org_id: str, space_id: str, name: str | None) -> None:
        """Create org-shared space ``space_id`` within ``org_id``.

        Raises ``ValueError`` if ``(org_id, space_id)`` already exists or if
        ``space_id`` is a reserved name (a real invariant here — the retired
        standalone layout is unrepresentable even for direct/non-HTTP callers).
        Slug SHAPE validation stays the caller's responsibility.

        The insert relies on the primary key rather than a pre-check ``SELECT`` so
        a concurrent duplicate create is a clean "already exists" (the second
        insert no-ops via ``ON CONFLICT``) instead of an uncaught ``UniqueViolation``.
        """
        if is_reserved_space_id(space_id):
            raise ValueError(f"space {space_id!r} is a reserved name")
        cur = self._conn.execute(
            """
            INSERT INTO spaces (org_id, space_id, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, space_id) DO NOTHING
            """,
            (org_id, space_id, name),
        )
        if cur.rowcount == 0:
            raise ValueError(f"space {space_id!r} already exists")

    def ensure_space(self, org_id: str, space_id: str, name: str | None = None) -> None:
        """Idempotently register ``space_id`` in ``org_id`` (no error on collision).

        Used when a snapshot write implies a space that may not yet have a
        registry row (e.g. the factory dumping into ``prd-<project>``). Unlike
        :meth:`create_space` a pre-existing row is a no-op, never an error.

        Reserved space ids are still refused: ``save_snapshot`` only ever ensures
        the legitimate project space ``<project>`` (never a ``-validation`` /
        ``-plan`` id), so this guard never fires on the real write path but keeps
        the retired standalone layout unrepresentable for any caller.
        """
        if is_reserved_space_id(space_id):
            raise ValueError(f"space {space_id!r} is a reserved name")
        self._conn.execute(
            """
            INSERT INTO spaces (org_id, space_id, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, space_id) DO NOTHING
            """,
            (org_id, space_id, name),
        )

    def rename_space(self, org_id: str, space_id: str, name: str | None) -> bool:
        """Set the display ``name`` of a space; True if it existed.

        Only the human-facing ``name`` changes — ``space_id`` is the immutable key
        every snapshot is tenanted by, so it is never touched.
        """
        cur = self._conn.execute(
            "UPDATE spaces SET name = %s WHERE org_id = %s AND space_id = %s",
            (name, org_id, space_id),
        )
        return cur.rowcount > 0

    def delete_space(self, org_id: str, space_id: str) -> bool:
        """Remove the space registry row; return True if one was deleted.

        Only the registry entry is touched here — the space's snapshots (and any
        mounts referencing them) are purged by the caller.
        """
        cur = self._conn.execute(
            "DELETE FROM spaces WHERE org_id = %s AND space_id = %s",
            (org_id, space_id),
        )
        return cur.rowcount > 0

    # --- reads -------------------------------------------------------------
    def list_spaces(self, org_id: str) -> list[dict]:
        """Return every space in ``org_id`` ordered by ``space_id``.

        Each row is ``{space_id, name, created_at}``.
        """
        rows = self._conn.execute(
            """
            SELECT space_id, name, created_at
            FROM spaces
            WHERE org_id = %s
            ORDER BY space_id
            """,
            (org_id,),
        ).fetchall()
        return [
            {"space_id": r[0], "name": r[1], "created_at": r[2]} for r in rows
        ]

    def exists(self, org_id: str, space_id: str) -> bool:
        """Return True if space ``space_id`` exists in ``org_id``."""
        row = self._conn.execute(
            "SELECT 1 FROM spaces WHERE org_id = %s AND space_id = %s",
            (org_id, space_id),
        ).fetchone()
        return row is not None
