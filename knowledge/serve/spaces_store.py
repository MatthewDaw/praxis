"""App-level spaces store: a login's private, named working knowledge graphs.

A *space* lets one login own multiple ``user_id`` partitions within an org. The
backend normally hardwires the tenant ``user_id`` to ``principal.sub`` (one live
graph per login per org); a space adds a second axis so different agents can
drive different live graphs concurrently. The effective tenant ``user_id`` is
derived by the app (default space => ``principal.sub``; named space ``<sid>`` =>
``f"{principal.sub}::space:{sid}"``) — this store only tracks which spaces a login
has created so the request path can validate ownership.

Spaces are PRIVATE to the creating login: every row is keyed by ``owner_sub`` and
is never shared across logins. Backed by the ``spaces`` table (see migrations/),
reusing a passed-in psycopg connection (the same autocommit connection the
candidate store uses), mirroring :class:`knowledge.serve.orgs_store.OrgsStore`.
"""

from __future__ import annotations

import psycopg


class SpacesStore:
    """A login's private named spaces persisted to the ``spaces`` table."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # --- mutations ---------------------------------------------------------
    def create_space(
        self, org_id: str, owner_sub: str, space_id: str, name: str | None
    ) -> None:
        """Create space ``space_id`` owned by ``owner_sub`` within ``org_id``.

        Raises ``ValueError`` if ``(org_id, owner_sub, space_id)`` already exists.
        Slug validation (shape, reserved names) is the caller's responsibility.

        The insert relies on the primary key rather than a pre-check ``SELECT`` so
        a concurrent duplicate create is a clean "already exists" (the second
        insert no-ops via ``ON CONFLICT``) instead of an uncaught ``UniqueViolation``.
        """
        cur = self._conn.execute(
            """
            INSERT INTO spaces (org_id, owner_sub, space_id, name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, owner_sub, space_id) DO NOTHING
            """,
            (org_id, owner_sub, space_id, name),
        )
        if cur.rowcount == 0:
            raise ValueError(f"space {space_id!r} already exists")

    def delete_space(self, org_id: str, owner_sub: str, space_id: str) -> bool:
        """Remove the space registry row; return True if one was deleted.

        Only the registry entry is touched here — the space's facts/edges/caches
        (stored under the derived ``user_id``) are purged by the caller so a
        re-created same-id space starts empty rather than resurrecting old data.
        """
        cur = self._conn.execute(
            """
            DELETE FROM spaces
            WHERE org_id = %s AND owner_sub = %s AND space_id = %s
            """,
            (org_id, owner_sub, space_id),
        )
        return cur.rowcount > 0

    # --- reads -------------------------------------------------------------
    def list_spaces(self, org_id: str, owner_sub: str) -> list[dict]:
        """Return ``owner_sub``'s spaces in ``org_id`` ordered by ``space_id``.

        Each row is ``{space_id, name, created_at}``.
        """
        rows = self._conn.execute(
            """
            SELECT space_id, name, created_at
            FROM spaces
            WHERE org_id = %s AND owner_sub = %s
            ORDER BY space_id
            """,
            (org_id, owner_sub),
        ).fetchall()
        return [
            {"space_id": r[0], "name": r[1], "created_at": r[2]} for r in rows
        ]

    def owns(self, org_id: str, owner_sub: str, space_id: str) -> bool:
        """Return True if ``owner_sub`` owns space ``space_id`` in ``org_id``."""
        row = self._conn.execute(
            """
            SELECT 1 FROM spaces
            WHERE org_id = %s AND owner_sub = %s AND space_id = %s
            """,
            (org_id, owner_sub, space_id),
        ).fetchone()
        return row is not None
