"""Persistent candidate store backing the dashboard API.

Candidate records are kept verbatim in the dashboard's JSON shape (the same one
``frontend-react`` parses) and persisted to a JSON file. On first run the store
seeds itself from the dashboard's mock fixture, so the live API serves familiar
data — but now mutable and durable across restarts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DEFAULT_STORE = HERE / "data" / "candidates.json"
PIPELINE_SEED = HERE / "data" / "pipeline-candidates.json"
SEED_FIXTURE = Path(__file__).resolve().parents[2] / "frontend-react" / "public" / "mock-candidates.json"

_NEXT_STATE = {"proposed": "suggested", "suggested": "active"}

Candidate = dict[str, Any]


def _cid(c: Candidate) -> str:
    return str(c.get("id", ""))


def _state(c: Candidate) -> str:
    return str(c.get("state", "proposed"))


def contradiction_ids(c: Candidate) -> list[str]:
    raw = c.get("contradiction_ids") or c.get("contradictions") or []
    return [str(x.get("id") if isinstance(x, dict) else x) for x in raw]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromotionError(ValueError):
    """Raised when a candidate can't be promoted from its current state."""


class CandidateStore:
    def __init__(
        self,
        path: Path = DEFAULT_STORE,
        seed: Path | None = None,
        *,
        pipeline_seed: Path = PIPELINE_SEED,
        fixture_seed: Path = SEED_FIXTURE,
    ) -> None:
        self.path = Path(path)
        self.pipeline_seed = Path(pipeline_seed)
        self.fixture_seed = Path(fixture_seed)
        self.seed = Path(seed) if seed is not None else self._default_seed()
        self._candidates: list[Candidate] = self._load()

    def _default_seed(self) -> Path:
        if self.pipeline_seed.exists():
            return self.pipeline_seed
        if self.pipeline_seed.parent.joinpath("pipeline-insights.json").exists():
            from knowledge.serve.pipeline_adapter import export_pipeline_candidates

            export_pipeline_candidates(output_path=self.pipeline_seed)
            if self.pipeline_seed.exists():
                return self.pipeline_seed
        return self.fixture_seed

    # --- persistence -------------------------------------------------------
    def _load(self) -> list[Candidate]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        seeded = (
            json.loads(self.seed.read_text(encoding="utf-8-sig"))
            if self.seed.exists()
            else []
        )
        self._candidates = seeded
        self._persist()
        return seeded

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._candidates, indent=2), encoding="utf-8")

    # --- reads -------------------------------------------------------------
    # org_id/user_id are accepted (and ignored) so this single-tenant JSON store
    # is interchangeable with PostgresCandidateStore behind the same routes.
    def list(
        self,
        org_id: str = "default",
        user_id: str = "default",
        state: str | None = None,
    ) -> list[Candidate]:
        return [c for c in self._candidates if state is None or _state(c) == state]

    def get(
        self,
        org_id: str = "default",
        user_id: str = "default",
        cid: str = "",
    ) -> Candidate | None:
        return next((c for c in self._candidates if _cid(c) == cid), None)

    # --- mutations ---------------------------------------------------------
    def _audit(self, c: Candidate, action: str, actor: str = "human-gate", note: str | None = None) -> None:
        key = "auditTrail" if "auditTrail" in c or "audit_trail" not in c else "audit_trail"
        entry = {"action": action, "timestamp": _now(), "provenance": c.get("provenance", ""), "actor": actor}
        if note:
            entry["note"] = note
        c.setdefault(key, []).append(entry)

    def promote(
        self,
        org_id: str = "default",
        user_id: str = "default",
        cid: str = "",
        target: str | None = None,
    ) -> Candidate:
        c = self.get(cid=cid)
        if c is None:
            raise KeyError(cid)
        nxt = _NEXT_STATE.get(_state(c))
        if nxt is None:
            raise PromotionError(f"cannot promote from state {_state(c)!r}")
        if target is not None and target != nxt:
            raise PromotionError(f"expected target {nxt!r}, got {target!r}")
        c["state"] = nxt
        self._audit(c, f"promoted_to_{nxt}")
        self._persist()
        return c

    def reject(
        self,
        org_id: str = "default",
        user_id: str = "default",
        cid: str = "",
        reason: str | None = None,
    ) -> Candidate:
        c = self.get(cid=cid)
        if c is None:
            raise KeyError(cid)
        c["state"] = "decayed"
        self._audit(c, "rejected", note=reason)
        self._persist()
        return c

    def resolve(
        self,
        org_id: str = "default",
        user_id: str = "default",
        pair_id: str = "",
        keep_id: str = "",
    ) -> Candidate:
        a, _, b = pair_id.partition("__")
        loser_id = b if keep_id == a else a
        kept, loser = self.get(cid=keep_id), self.get(cid=loser_id)
        if kept is None:
            raise KeyError(keep_id)
        # Drop the a<->b contradiction link from both sides; decay the loser.
        self._strip_link(kept, loser_id)
        self._audit(kept, "kept_over_contradiction", note=f"superseded {loser_id}")
        if loser is not None:
            self._strip_link(loser, keep_id)
            loser["state"] = "decayed"
            self._audit(loser, "superseded", note=f"lost contradiction to {keep_id}")
        self._persist()
        return kept

    @staticmethod
    def _strip_link(c: Candidate, other_id: str) -> None:
        for key in ("contradiction_ids", "contradictions"):
            if key in c and isinstance(c[key], list):
                c[key] = [x for x in c[key] if _link_id(x) != other_id]


def _link_id(x: Any) -> str:
    return str(x.get("id") if isinstance(x, dict) else x)
