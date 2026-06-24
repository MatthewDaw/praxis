"""Scoped API keys: mint / hash / verify / revoke + a thin CLI.

An API key is a long-lived, org-scoped service token for automated agents that
can't run the Cognito SRP + per-request token mint. The raw key has the form
``pxk_<random>`` and is shown exactly once at mint time; the database stores only
its sha256 hex (:data:`api_keys.key_hash`). Resolving a key yields the owning
org (and optional user) so the auth dependency can build a Principal and enforce
that the request's ``X-Praxis-Org`` equals the key's org.

CLI (uses the same ``knowledge.serve.db.connect()`` as the server)::

    uv run python -m knowledge.serve.apikeys mint --org acme [--user <sub>] [--label ci]
    uv run python -m knowledge.serve.apikeys revoke <id>
    uv run python -m knowledge.serve.apikeys list [--org acme]
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

KEY_PREFIX = "pxk_"


def hash_key(raw_key: str) -> str:
    """Return the sha256 hex of a raw API key (what we persist + look up by)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """Mint a fresh raw key (``pxk_<random>``) — never stored, shown once."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass
class ApiKeyRecord:
    id: str
    org_id: str
    user_id: str | None
    label: str | None


def mint_key(
    conn: Any, org_id: str, user_id: str | None = None, label: str | None = None
) -> tuple[str, str]:
    """Create a key for ``org_id``; return ``(key_id, raw_key)``.

    Only the hash is persisted; the returned raw key is the sole copy.
    """
    key_id = uuid.uuid4().hex
    raw_key = generate_key()
    conn.execute(
        "INSERT INTO api_keys (id, key_hash, org_id, user_id, label) "
        "VALUES (%s, %s, %s, %s, %s)",
        (key_id, hash_key(raw_key), org_id, user_id, label),
    )
    return key_id, raw_key


def revoke_key(conn: Any, key_id: str) -> bool:
    """Mark a key revoked. Returns True if a row was updated."""
    cur = conn.execute(
        "UPDATE api_keys SET revoked = true WHERE id = %s AND NOT revoked",
        (key_id,),
    )
    return cur.rowcount > 0


def resolve_key(conn: Any, raw_key: str) -> ApiKeyRecord | None:
    """Resolve a raw key to its record, or None if unknown/revoked.

    Bumps ``last_used_at`` as a side effect on a successful lookup.
    """
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None
    row = conn.execute(
        "SELECT id, org_id, user_id FROM api_keys "
        "WHERE key_hash = %s AND NOT revoked",
        (hash_key(raw_key),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row[0],)
    )
    return ApiKeyRecord(id=row[0], org_id=row[1], user_id=row[2], label=None)


def list_keys(conn: Any, org_id: str | None = None) -> list[dict[str, Any]]:
    """List keys (id/org/user/label/created/last_used/revoked), optionally by org."""
    sql = (
        "SELECT id, org_id, user_id, label, created_at, last_used_at, revoked "
        "FROM api_keys"
    )
    params: list[object] = []
    if org_id is not None:
        sql += " WHERE org_id = %s"
        params.append(org_id)
    sql += " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0],
            "org_id": r[1],
            "user_id": r[2],
            "label": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
            "last_used_at": r[5].isoformat() if r[5] else None,
            "revoked": r[6],
        }
        for r in rows
    ]


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    from knowledge.serve import db

    parser = argparse.ArgumentParser(prog="knowledge.serve.apikeys")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mint = sub.add_parser("mint", help="mint a new key (prints the raw key once)")
    p_mint.add_argument("--org", required=True)
    p_mint.add_argument("--user", default=None)
    p_mint.add_argument("--label", default=None)

    p_revoke = sub.add_parser("revoke", help="revoke a key by id")
    p_revoke.add_argument("id")

    p_list = sub.add_parser("list", help="list keys")
    p_list.add_argument("--org", default=None)

    args = parser.parse_args(argv)
    conn = db.connect()
    db.bootstrap()

    if args.cmd == "mint":
        key_id, raw_key = mint_key(conn, args.org, args.user, args.label)
        print(f"minted key id={key_id} org={args.org}")
        print(f"raw key (store it now, shown only once): {raw_key}")
        return 0
    if args.cmd == "revoke":
        ok = revoke_key(conn, args.id)
        print(f"revoked {args.id}" if ok else f"no active key {args.id}")
        return 0 if ok else 1
    if args.cmd == "list":
        for k in list_keys(conn, args.org):
            flag = " [REVOKED]" if k["revoked"] else ""
            print(f"{k['id']}  org={k['org_id']}  user={k['user_id']}  label={k['label']}{flag}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
