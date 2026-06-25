"""App-level orgs store: create/join password-gated orgs and track membership.

Orgs are an app concept (not Cognito groups). A user creates an org and sets its
password, or joins an existing org by supplying that password. Passwords are
hashed with stdlib ``pbkdf2_hmac(sha256)`` using a per-org random salt; verify
uses ``hmac.compare_digest`` for a constant-time comparison. Backed by the
``orgs`` / ``org_members`` tables (see migrations/), reusing a passed-in psycopg
connection (the same autocommit connection the candidate store uses).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import psycopg

# pbkdf2 parameters: sha256 over a 16-byte hex salt; hashes/salts stored as hex.
_HASH_NAME = "sha256"
_ITERATIONS = 200_000


def _hash_password(password: str, salt: str) -> str:
    """Return the hex pbkdf2_hmac(sha256) digest of ``password`` for ``salt``."""
    dk = hashlib.pbkdf2_hmac(
        _HASH_NAME, password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    )
    return dk.hex()


class OrgsStore:
    """Password-gated orgs + membership persisted to ``orgs``/``org_members``."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # --- mutations ---------------------------------------------------------
    def create_org(self, org_id: str, name: str, password: str, user_id: str) -> None:
        """Create ``org_id`` (hashing ``password``) and add ``user_id`` as owner.

        Raises ``ValueError`` if the org already exists.
        """
        row = self._conn.execute(
            "SELECT 1 FROM orgs WHERE org_id = %s", (org_id,)
        ).fetchone()
        if row:
            raise ValueError(f"org {org_id!r} already exists")
        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        self._conn.execute(
            """
            INSERT INTO orgs (org_id, name, password_hash, password_salt, created_by)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (org_id, name, password_hash, salt, user_id),
        )
        self._conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, user_id, "owner"),
        )

    def join_org(self, org_id: str, password: str, user_id: str) -> None:
        """Add ``user_id`` to ``org_id`` after verifying ``password``.

        Raises ``ValueError`` on unknown org or bad password.
        """
        row = self._conn.execute(
            "SELECT password_hash, password_salt FROM orgs WHERE org_id = %s",
            (org_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown org {org_id!r}")
        password_hash, salt = row
        if not hmac.compare_digest(password_hash, _hash_password(password, salt)):
            raise ValueError("invalid org password")
        self._conn.execute(
            """
            INSERT INTO org_members (org_id, user_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (org_id, user_id) DO NOTHING
            """,
            (org_id, user_id, "member"),
        )

    def set_password(
        self, org_id: str, current_password: str, new_password: str, user_id: str
    ) -> None:
        """Rotate ``org_id``'s join password after verifying the current one.

        The caller must be a member and supply the correct current password.
        Raises ``ValueError`` on unknown org, non-membership, or bad password.
        """
        row = self._conn.execute(
            "SELECT password_hash, password_salt FROM orgs WHERE org_id = %s",
            (org_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown org {org_id!r}")
        if not self.is_member(org_id, user_id):
            raise ValueError(f"not a member of org {org_id!r}")
        password_hash, salt = row
        if not hmac.compare_digest(password_hash, _hash_password(current_password, salt)):
            raise ValueError("invalid current password")
        new_salt = secrets.token_hex(16)
        self._conn.execute(
            "UPDATE orgs SET password_hash = %s, password_salt = %s WHERE org_id = %s",
            (_hash_password(new_password, new_salt), new_salt, org_id),
        )

    # --- reads -------------------------------------------------------------
    def list_orgs(self, user_id: str) -> list[dict]:
        """Return the orgs ``user_id`` belongs to, with name and role."""
        rows = self._conn.execute(
            """
            SELECT m.org_id, o.name, m.role
            FROM org_members m
            JOIN orgs o ON o.org_id = m.org_id
            WHERE m.user_id = %s
            ORDER BY m.org_id
            """,
            (user_id,),
        ).fetchall()
        return [{"org_id": r[0], "name": r[1], "role": r[2]} for r in rows]

    def members(self, org_id: str) -> list[dict]:
        """Return every member of ``org_id`` as ``{user_id, role}`` (owners first).

        Used by skill sharing to enumerate the org members whose graphs a caller
        may browse. Emails are not stored app-side (they live in Cognito), so
        only the Cognito ``sub`` (``user_id``) and role are returned.
        """
        rows = self._conn.execute(
            """
            SELECT user_id, role FROM org_members WHERE org_id = %s
            ORDER BY (role = 'owner') DESC, user_id
            """,
            (org_id,),
        ).fetchall()
        return [{"user_id": r[0], "role": r[1]} for r in rows]

    def is_member(self, org_id: str, user_id: str) -> bool:
        """Return True if ``user_id`` is a member of ``org_id``."""
        row = self._conn.execute(
            "SELECT 1 FROM org_members WHERE org_id = %s AND user_id = %s",
            (org_id, user_id),
        ).fetchone()
        return row is not None
