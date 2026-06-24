"""Unit tests for scoped API keys + the API-key-aware auth dependency.

These run fully offline against an in-memory fake of the tiny ``api_keys`` SQL
surface (insert / select-by-hash / update revoked + last_used), so no Postgres or
network is needed. They cover: mint -> hash-only storage, resolve accept/reject,
revoke, and the ``make_current_user`` dependency resolving + org-scoping a key.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from knowledge.serve import apikeys, auth


class FakeCursor:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Minimal in-memory stand-in for the api_keys table operations used here."""

    def __init__(self):
        # id -> dict(key_hash, org_id, user_id, label, revoked)
        self.keys: dict[str, dict] = {}

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO api_keys"):
            key_id, key_hash, org_id, user_id, label = params
            self.keys[key_id] = {
                "key_hash": key_hash,
                "org_id": org_id,
                "user_id": user_id,
                "label": label,
                "revoked": False,
            }
            return FakeCursor([], 1)
        if s.startswith("UPDATE api_keys SET revoked = true"):
            (key_id,) = params
            row = self.keys.get(key_id)
            if row and not row["revoked"]:
                row["revoked"] = True
                return FakeCursor([], 1)
            return FakeCursor([], 0)
        if s.startswith("SELECT id, org_id, user_id FROM api_keys"):
            (key_hash,) = params
            for kid, row in self.keys.items():
                if row["key_hash"] == key_hash and not row["revoked"]:
                    return FakeCursor([(kid, row["org_id"], row["user_id"])], 1)
            return FakeCursor([], 0)
        if s.startswith("UPDATE api_keys SET last_used_at"):
            return FakeCursor([], 1)
        raise AssertionError(f"unexpected SQL: {s}")


def test_mint_stores_hash_only_and_returns_raw_once():
    conn = FakeConn()
    key_id, raw = apikeys.mint_key(conn, "acme", user_id="u1", label="ci")
    assert raw.startswith("pxk_")
    stored = conn.keys[key_id]
    assert stored["key_hash"] == apikeys.hash_key(raw)
    assert raw not in stored["key_hash"]  # raw never persisted
    assert stored["org_id"] == "acme"


def test_resolve_accepts_valid_key():
    conn = FakeConn()
    _, raw = apikeys.mint_key(conn, "acme", user_id="u1")
    rec = apikeys.resolve_key(conn, raw)
    assert rec is not None
    assert rec.org_id == "acme"
    assert rec.user_id == "u1"


def test_resolve_rejects_unknown_and_malformed():
    conn = FakeConn()
    assert apikeys.resolve_key(conn, "pxk_nope") is None
    assert apikeys.resolve_key(conn, "not-a-key") is None
    assert apikeys.resolve_key(conn, "") is None


def test_revoke_then_resolve_rejects():
    conn = FakeConn()
    key_id, raw = apikeys.mint_key(conn, "acme")
    assert apikeys.revoke_key(conn, key_id) is True
    assert apikeys.resolve_key(conn, raw) is None
    assert apikeys.revoke_key(conn, key_id) is False  # already revoked


def test_make_current_user_resolves_api_key(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    conn = FakeConn()
    _, raw = apikeys.mint_key(conn, "acme", user_id="u1")
    dep = auth.make_current_user(conn)
    p = dep(authorization=None, x_praxis_key=raw)
    assert p.sub == "u1"
    assert p.api_key_org == "acme"


def test_make_current_user_synthetic_sub_when_no_user(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    conn = FakeConn()
    key_id, raw = apikeys.mint_key(conn, "acme")
    dep = auth.make_current_user(conn)
    p = dep(authorization=None, x_praxis_key=raw)
    assert p.sub == f"apikey:{key_id}"
    assert p.api_key_org == "acme"


def test_make_current_user_rejects_bad_key(monkeypatch):
    monkeypatch.delenv("PRAXIS_AUTH_DISABLED", raising=False)
    conn = FakeConn()
    dep = auth.make_current_user(conn)
    with pytest.raises(HTTPException) as exc:
        dep(authorization=None, x_praxis_key="pxk_bogus")
    assert exc.value.status_code == 401


def test_make_current_user_dev_seam_short_circuits(monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    conn = FakeConn()
    dep = auth.make_current_user(conn)
    p = dep(authorization=None, x_praxis_key=None)
    assert p.sub == "dev-user"
