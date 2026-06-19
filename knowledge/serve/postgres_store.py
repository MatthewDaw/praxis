"""Postgres-backed candidate store backing the dashboard API.

A drop-in replacement for :class:`store.CandidateStore` that persists the
verbatim dashboard candidate dict in the ``candidates`` table's ``doc`` jsonb
column. Rows are multi-tenant: every row is owned by an ``(org_id, user_id)``
pair, and ``shared`` rows are visible to the whole org. The read predicate for
a requester is ``org_id = %s AND (shared OR user_id = %s)``. On first run the
store seeds itself from the dashboard's mock fixture (when no rows are visible).
"""

from __future__ import annotations

import json
from pathlib import Path

from psycopg.types.json import Jsonb

from knowledge.serve import db
from knowledge.serve.store import (
    SEED_FIXTURE,
    Candidate,
    PromotionError,
    _link_id,
    _NEXT_STATE,
    _now,
    contradiction_ids,
)


def _cid(c: Candidate) -> str:
    return str(c.get("id", ""))


def _state(c: Candidate) -> str:
    return str(c.get("state", "proposed"))


class PostgresCandidateStore:
    """Candidate store persisted to the multi-tenant ``candidates`` table."""

    def __init__(
        self,
        dsn: str | None = None,
        org_id: str = "default",
        user_id: str = "default",
        shared: bool = False,
        seed: Path = SEED_FIXTURE,
    ) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.shared = shared
        self.seed = Path(seed)
        self._conn = db.connect(dsn)
        self._last_listed: list[Candidate] = []
        self._seed_if_empty()

    # --- persistence -------------------------------------------------------
    def _seed_if_empty(self) -> None:
        """Seed from the mock fixture when no rows are visible to this requester."""
        row = self._conn.execute(
            "SELECT count(*) FROM candidates WHERE org_id = %s AND (shared OR user_id = %s)",
            (self.org_id, self.user_id),
        ).fetchone()
        if row and row[0]:
            return
        seeded = (
            json.loads(self.seed.read_text(encoding="utf-8-sig"))
            if self.seed.exists()
            else []
        )
        for c in seeded:
            self._upsert(c)

    def _persist(self) -> None:
        """Upsert every dict currently tracked from the last ``list()`` call."""
        for c in self._last_listed:
            self._upsert(c)

    def _upsert(self, doc: Candidate) -> None:
        """Insert or update a candidate row, keeping id/state in sync with the doc."""
        self._conn.execute(
            """
            INSERT INTO candidates (id, org_id, user_id, shared, state, doc, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (org_id, user_id, id) DO UPDATE SET
                shared = EXCLUDED.shared,
                state = EXCLUDED.state,
                doc = EXCLUDED.doc,
                updated_at = now()
            """,
            (
                doc["id"],
                self.org_id,
                self.user_id,
                self.shared,
                doc.get("state", "proposed"),
                Jsonb(doc),
            ),
        )

    # --- reads -------------------------------------------------------------
    def list(self, state: str | None = None) -> list[Candidate]:
        sql = "SELECT doc FROM candidates WHERE org_id = %s AND (shared OR user_id = %s)"
        params: list[object] = [self.org_id, self.user_id]
        if state is not None:
            sql += " AND state = %s"
            params.append(state)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        self._last_listed = [r[0] for r in rows]
        return self._last_listed

    def get(self, cid: str) -> Candidate | None:
        row = self._conn.execute(
            "SELECT doc FROM candidates WHERE org_id = %s AND (shared OR user_id = %s) AND id = %s",
            (self.org_id, self.user_id, cid),
        ).fetchone()
        return row[0] if row else None

    # --- mutations ---------------------------------------------------------
    @staticmethod
    def _audit(c: Candidate, action: str, actor: str = "human-gate", note: str | None = None) -> None:
        key = "auditTrail" if "auditTrail" in c or "audit_trail" not in c else "audit_trail"
        entry = {"action": action, "timestamp": _now(), "provenance": c.get("provenance", ""), "actor": actor}
        if note:
            entry["note"] = note
        c.setdefault(key, []).append(entry)

    def promote(self, cid: str, target: str | None = None) -> Candidate:
        c = self.get(cid)
        if c is None:
            raise KeyError(cid)
        nxt = _NEXT_STATE.get(_state(c))
        if nxt is None:
            raise PromotionError(f"cannot promote from state {_state(c)!r}")
        if target is not None and target != nxt:
            raise PromotionError(f"expected target {nxt!r}, got {target!r}")
        c["state"] = nxt
        self._audit(c, f"promoted_to_{nxt}")
        self._upsert(c)
        return c

    def reject(self, cid: str, reason: str | None = None) -> Candidate:
        c = self.get(cid)
        if c is None:
            raise KeyError(cid)
        c["state"] = "decayed"
        self._audit(c, "rejected", note=reason)
        self._upsert(c)
        return c

    def resolve(self, pair_id: str, keep_id: str) -> Candidate:
        a, _, b = pair_id.partition("__")
        loser_id = b if keep_id == a else a
        kept, loser = self.get(keep_id), self.get(loser_id)
        if kept is None:
            raise KeyError(keep_id)
        # Drop the a<->b contradiction link from both sides; decay the loser.
        self._strip_link(kept, loser_id)
        self._audit(kept, "kept_over_contradiction", note=f"superseded {loser_id}")
        self._upsert(kept)
        if loser is not None:
            self._strip_link(loser, keep_id)
            loser["state"] = "decayed"
            self._audit(loser, "superseded", note=f"lost contradiction to {keep_id}")
            self._upsert(loser)
        return kept

    @staticmethod
    def _strip_link(c: Candidate, other_id: str) -> None:
        for key in ("contradiction_ids", "contradictions"):
            if key in c and isinstance(c[key], list):
                c[key] = [x for x in c[key] if _link_id(x) != other_id]
