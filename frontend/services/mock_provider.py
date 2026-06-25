"""
===============================================================================
FILE: services/mock_provider.py
AUTHOR: Monica Peters
CREATED: 2026-06-18

PURPOSE:
In-memory DataProvider for local development and demo without Matthew's backend.

OPERATIONAL:
- Loads fixtures from mock_data.py
- Does not import pipeline/ or eval/
===============================================================================
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from models.api_key import ApiKey, CreatedApiKey
from models.candidate import Candidate, CandidateState, next_promotion_state
from mock_data import get_mock_candidate_dicts

_MOCK_USER_ID = "mock-user"


class MockDataProvider:
    """Local fixture-backed provider — zero backend required."""

    def __init__(self) -> None:
        self._candidates: dict[str, Candidate] = {
            c.id: c for c in (Candidate.from_mapping(row) for row in get_mock_candidate_dicts())
        }
        self._api_keys: dict[str, ApiKey] = {}
        self._api_key_seq = 0

    def list_candidates(self, state: CandidateState | None = None) -> list[Candidate]:
        items = list(self._candidates.values())
        if state is not None:
            items = [c for c in items if c.state == state]
        return sorted(items, key=lambda c: c.created_at, reverse=True)

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        return self._candidates.get(candidate_id)

    def promote(self, candidate_id: str) -> Candidate:
        candidate = self._require_candidate(candidate_id)
        next_state = next_promotion_state(candidate.state)
        if next_state is None:
            raise ValueError(f"Candidate {candidate_id!r} is already {candidate.state.value}")
        audit = _append_audit(
            candidate,
            action=f"promoted_to_{next_state.value}",
            actor="human-gate",
        )
        updated = _clone_candidate(candidate, state=next_state, extra=audit)
        self._candidates[candidate_id] = updated
        return updated

    def reject(self, candidate_id: str, reason: str | None = None) -> None:
        candidate = self._require_candidate(candidate_id)
        audit = _append_audit(
            candidate,
            action="rejected",
            actor="human-gate",
            note=reason or "",
        )
        updated = _clone_candidate(candidate, state=CandidateState.REJECTED, extra=audit)
        self._candidates[candidate_id] = updated

    def resolve_contradiction(
        self,
        contradiction_id: str,
        *,
        keep: str | list[str] | None = None,
        custom_text: str | None = None,
    ) -> Candidate:
        """H11: settle a cluster by which members to ``keep`` — "all" (every member
        holds; keep all active), "none" (reject all), or a list of ids to keep
        (reject the rest). Cross-links among cluster members are cleared either way.
        Returns the primary surviving candidate (first kept, else first member)."""
        if keep == "defer":
            raise ValueError("Defer is a UI-only action — no mutation performed.")
        if custom_text and custom_text.strip():
            raise ValueError("custom_text resolution is not supported by the mock provider.")

        member_ids = _parse_cluster_members(contradiction_id)
        members = [(mid, self._require_candidate(mid)) for mid in member_ids]
        member_set = set(member_ids)

        if keep == "all":
            keep_set = set(member_ids)
        elif keep == "none":
            keep_set = set()
        elif isinstance(keep, (list, tuple)):
            keep_set = {str(k) for k in keep}
            unknown = keep_set - member_set
            if unknown:
                raise ValueError(f"keep ids not in cluster: {sorted(unknown)}")
        else:
            raise ValueError("keep must be 'all', 'none', or a list of fact ids.")

        for mid, cand in members:
            cleared = [cid for cid in cand.contradiction_ids if cid not in member_set]
            if mid in keep_set:
                audit = _append_audit(
                    cand, action="kept", actor="human-gate",
                    note="contradiction resolved (kept)",
                )
                self._candidates[mid] = _clone_candidate(
                    cand, contradiction_ids=cleared, extra=audit
                )
            else:
                audit = _append_audit(
                    cand, action="rejected", actor="human-gate",
                    note="contradiction resolved (rejected)",
                )
                self._candidates[mid] = _clone_candidate(
                    cand,
                    state=CandidateState.REJECTED,
                    contradiction_ids=cleared,
                    extra=audit,
                )

        primary = next((mid for mid in member_ids if mid in keep_set), member_ids[0])
        return self._candidates[primary]

    def list_api_keys(self) -> list[ApiKey]:
        return sorted(
            self._api_keys.values(),
            key=lambda k: k.created_at,
            reverse=True,
        )

    def create_api_key(self, label: str | None = None) -> CreatedApiKey:
        self._api_key_seq += 1
        key_id = f"key_{self._api_key_seq}"
        raw_key = f"pxk_{secrets.token_hex(16)}"
        normalized = label.strip() if isinstance(label, str) else label
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._api_keys[key_id] = ApiKey(
            id=key_id,
            label=normalized or None,
            user_id=_MOCK_USER_ID,
            created_at=created_at,
            last_used_at=None,
            revoked=False,
        )
        return CreatedApiKey(
            id=key_id,
            key=raw_key,
            label=normalized or None,
            created_at=created_at,
        )

    def revoke_api_key(self, key_id: str) -> ApiKey:
        existing = self._api_keys.get(key_id)
        if existing is None:
            raise KeyError(f"Unknown API key id: {key_id!r}")
        revoked = ApiKey(
            id=existing.id,
            label=existing.label,
            user_id=existing.user_id,
            created_at=existing.created_at,
            last_used_at=existing.last_used_at,
            revoked=True,
        )
        self._api_keys[key_id] = revoked
        return revoked

    def _require_candidate(self, candidate_id: str) -> Candidate:
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(f"Unknown candidate id: {candidate_id!r}")
        return candidate


def _parse_cluster_members(contradiction_id: str) -> list[str]:
    """A contradiction/cluster id is its member fact ids joined by ``"__"`` (a
    plain 2-fact pair is the 2-member case). Returns them in order."""
    members = [m for m in contradiction_id.split("__") if m]
    if len(members) < 2:
        raise ValueError(f"Invalid contradiction id: {contradiction_id!r}")
    return members


def _append_audit(
    candidate: Candidate,
    *,
    action: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    extra = dict(candidate.extra)
    trail = list(extra.get("auditTrail") or extra.get("audit_trail") or [])
    entry: dict[str, Any] = {
        "action": action,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": candidate.provenance,
        "actor": actor,
    }
    if note:
        entry["note"] = note
    trail.append(entry)
    extra["auditTrail"] = trail
    return extra


def _clone_candidate(
    candidate: Candidate,
    *,
    state: CandidateState | None = None,
    contradiction_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Candidate:
    resolved_state = state if state is not None else candidate.state
    return Candidate(
        id=candidate.id,
        title=candidate.title,
        content=candidate.content,
        state=resolved_state,
        confidence=candidate.confidence,
        provenance=candidate.provenance,
        created_at=candidate.created_at,
        confidence_breakdown=candidate.confidence_breakdown,
        contradiction_ids=list(contradiction_ids if contradiction_ids is not None else candidate.contradiction_ids),
        state_label=resolved_state.value,
        extra=dict(extra if extra is not None else candidate.extra),
    )
