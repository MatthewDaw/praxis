"""Restart-durability of scoped API keys (blocker #1).

A key minted yesterday MUST still authenticate after N restarts. The only thing
that can change across a restart is the hashing secret, so these tests pin the
invariant: with a STABLE ``PRAXIS_API_KEY_SECRET`` (env-provided, never
regenerated at boot) a minted key resolves indefinitely — and if that secret were
regenerated on restart, the key would die (the exact bug we guard against).

Fully offline against an in-memory fake of the ``api_keys`` table (no Postgres):
a "restart" is modeled as a fresh store holding the SAME persisted rows plus a
re-read of the secret from the environment.
"""

from __future__ import annotations

from knowledge.serve import apikeys

SECRET_ENV = "PRAXIS_API_KEY_SECRET"


class FakeCursor:
    def __init__(self, rows, rowcount=1):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeStore:
    """Durable in-memory stand-in: rows persist across simulated restarts."""

    def __init__(self, rows=None):
        # id -> (key_hash, org_id, user_id, revoked)
        self.rows: dict[str, tuple] = dict(rows or {})

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO api_keys"):
            key_id, key_hash, org_id, user_id, _label = params
            self.rows[key_id] = (key_hash, org_id, user_id, False)
            return FakeCursor([])
        if s.startswith("SELECT id, org_id, user_id FROM api_keys"):
            (key_hash,) = params
            for kid, (h, org, uid, revoked) in self.rows.items():
                if h == key_hash and not revoked:
                    return FakeCursor([(kid, org, uid)])
            return FakeCursor([])
        if s.startswith("UPDATE api_keys SET last_used_at"):
            return FakeCursor([])
        raise AssertionError(f"unexpected SQL: {s}")


def test_key_survives_restart_with_stable_secret(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, "stable-secret-value")
    store = FakeStore()
    _, raw = apikeys.mint_key(store, "bestie", user_id="u1")

    # "Restart": a brand-new store holding the SAME persisted rows, same secret.
    restarted = FakeStore(store.rows)
    for _ in range(5):  # after N restarts it still authenticates
        rec = apikeys.resolve_key(restarted, raw)
        assert rec is not None and rec.org_id == "bestie"


def test_regenerated_secret_would_invalidate_the_key(monkeypatch):
    # This is the bug we prevent: a boot-generated (changing) secret orphans keys.
    monkeypatch.setenv(SECRET_ENV, "secret-at-mint")
    store = FakeStore()
    _, raw = apikeys.mint_key(store, "bestie", user_id="u1")

    monkeypatch.setenv(SECRET_ENV, "different-secret-after-restart")
    assert apikeys.resolve_key(FakeStore(store.rows), raw) is None


def test_legacy_unpeppered_key_still_resolves_after_secret_added(monkeypatch):
    # A key minted BEFORE a pepper existed (plain sha256) must keep working once a
    # pepper is configured — seamless one-way migration, never a flag day.
    monkeypatch.delenv(SECRET_ENV, raising=False)
    store = FakeStore()
    _, raw = apikeys.mint_key(store, "sotos", user_id="u2")
    assert store.rows  # stored under the legacy hash

    monkeypatch.setenv(SECRET_ENV, "newly-configured-pepper")
    rec = apikeys.resolve_key(FakeStore(store.rows), raw)
    assert rec is not None and rec.org_id == "sotos"


def test_hash_is_deterministic_for_a_fixed_secret(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, "S")
    raw = "pxk_example"
    assert apikeys.hash_key(raw) == apikeys.hash_key(raw)
    monkeypatch.setenv(SECRET_ENV, "S2")
    assert apikeys.hash_key(raw) != apikeys._peppered_hash(raw, "S")


def test_unset_secret_uses_legacy_sha256(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    raw = "pxk_example"
    assert apikeys.hash_key(raw) == apikeys._legacy_hash(raw)
