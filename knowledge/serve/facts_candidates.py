"""Candidate read model projected over the ``facts`` spine.

This replaces the deleted candidate stores (``store.py`` /
``postgres_store.py``). Instead of a separate ``candidates`` table, the
dashboard "candidate" surface is now a projection of the tenant's facts graph
(:class:`PostgresVectorGraph`). The ``facts.state`` column carries the
proposed/active/rejected lifecycle, ``facts.meta`` carries dashboard-only fields
(``title``, ``auditTrail``), and the relationship links (``contradiction`` /
``contradicted_by`` / ``supersedes``) live in the ``fact_edges`` table.

Tenancy is bound at construction (one facade per ``(org_id, user_id)``), so the
methods here — unlike the old explicitly-tenanted stores — take no org/user
arguments. The candidate ``id`` equals the raw fact ``id`` (no ``pipe_``/
``cand_`` namespace), so contradiction links and lifecycle ops need no id
translation.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
    default_write_policy,
)
from knowledge.serve.contradiction_adapter import serialize_clusters, serialize_pairs
from knowledge.serve.pipeline_adapter import fact_to_candidate

Candidate = dict[str, Any]

# Human-gate promotion funnel: a proposed candidate is approved straight to
# active (the intermediate "suggested" step was removed). "active" and "rejected"
# are terminal — not in this map, so promoting from them raises.
_NEXT_STATE = {"proposed": "active"}


# Opt-in auto-resolution (Mem0-style): when enabled, a freshly-detected
# contradiction whose underlying slot clash is *high confidence* (a deterministic
# numeric or stance clash — the same signal ClaimConflictDetector resolves without
# an LLM) is auto-resolved via the existing supersede path (newest fact wins, loser
# rejected, edge flipped to contradicted_by) instead of being left PENDING for a
# human. Low-confidence (gray-zone judge) contradictions always stay pending. Off
# by default so existing behavior/tests are unchanged.
_AUTO_RESOLVE_ENV = "PRAXIS_AUTO_RESOLVE_CONTRADICTIONS"

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def auto_resolve_enabled() -> bool:
    """True iff the opt-in auto-resolution flag is set (default off)."""
    return os.getenv(_AUTO_RESOLVE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _deterministic_clash(attribute: str, a: str, b: str) -> bool:
    """High-confidence clash test mirroring ClaimConflictDetector._incompatible.

    A stance attribute with two different poles, or two values that both carry
    numbers and disagree, is a deterministic (no-LLM) contradiction — i.e. high
    confidence. Everything else (free-text / synonym gray zone) is NOT, so it stays
    pending for manual review.
    """
    an = " ".join(a.lower().split())
    bn = " ".join(b.lower().split())
    if an == bn:
        return False
    if attribute.strip().lower() == "stance":
        return True
    na, nb = _NUM_RE.findall(a), _NUM_RE.findall(b)
    return bool(na and nb and na != nb)


class PromotionError(ValueError):
    """Raised when a candidate can't be promoted from its current state."""


class DeletionError(ValueError):
    """Raised when a candidate can't be deleted from its current state.

    Deletion is gated to ``proposed``/``rejected`` facts; an ``active`` fact must
    be rejected first (FR-014). Surfaced as HTTP 409 at the route.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalidate(graph: Any, loser_id: str, winner_id: str | None = None) -> None:
    """Close a superseded fact's bi-temporal validity window (additive helper).

    Delegates to ``graph.invalidate`` (set ``invalid_at`` to the winner's
    ``valid_at``, falling back to ``now()``) on top of the existing ``rejected``
    state. Guarded with ``hasattr`` so a graph facade lacking the method (e.g. an
    overlay wrapper) degrades to the prior state-only behavior rather than
    failing — invalidation is purely additive.
    """
    invalidate = getattr(graph, "invalidate", None)
    if callable(invalidate):
        invalidate(loser_id, winner_id)


def _audit_entry(
    provenance: str,
    action: str,
    *,
    actor: str = "human-gate",
    note: str | None = None,
) -> dict[str, Any]:
    """Build an audit-trail entry matching the shape used by the old stores."""
    entry: dict[str, Any] = {
        "action": action,
        "timestamp": _now(),
        "provenance": provenance,
        "actor": actor,
    }
    if note:
        entry["note"] = note
    return entry


def _short_title(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= 60 else f"{text[:57].rstrip()}…"


class FactsCandidates:
    """Dashboard candidate facade over a single tenant's facts graph."""

    def __init__(
        self,
        conn: Any,
        org_id: str,
        user_id: str,
        *,
        embedder: Any | None = None,
        policy: Any | None = None,
    ) -> None:
        # ``embedder``/``policy`` are test seams: production uses the real
        # OpenRouter embedder + ConflictFlagger judge (default_write_policy);
        # tests inject a deterministic FakeEmbedder and a no-LLM policy.
        self.graph = PostgresVectorGraph(
            conn,
            org_id,
            user_id,
            embedder=embedder,
            policy=policy if policy is not None else default_write_policy(),
        )

    # --- internal helpers --------------------------------------------------
    def _rival_map(self) -> dict[str, dict[str, str]]:
        """Map each fact id to ``{rival_id: status}`` over both edge kinds.

        ``status`` is ``pending`` for a ``contradiction`` edge (no winner chosen)
        and ``resolved`` for a ``contradicted_by`` edge (a winner is active, the
        loser rejected). Both directions are recorded so a fact sees every rival.
        """
        links: dict[str, dict[str, str]] = {}
        for kind, status in (("contradiction", "pending"), ("contradicted_by", "resolved")):
            for src, dst, _kind in self.graph.all_edges(kind):
                links.setdefault(src, {})[dst] = status
                links.setdefault(dst, {})[src] = status
        return links

    def _to_candidate(
        self, fact: Any, rivals: dict[str, dict[str, str]] | None = None
    ) -> Candidate:
        rival_status = (rivals if rivals is not None else self._rival_map()).get(fact.id)
        return fact_to_candidate(fact, state=fact.state, rivals=rival_status)

    def _has_other_contradictions(self, fact_id: str, exclude: str | None = None) -> bool:
        """FR-008: does ``fact_id`` have a contradiction other than ``exclude``?"""
        return any(rid != exclude for rid in self._rival_map().get(fact_id, {}))

    def _append_audit(
        self,
        fact: Any,
        action: str,
        *,
        actor: str = "human-gate",
        note: str | None = None,
    ) -> None:
        meta = dict(fact.meta or {})
        provenance = str(fact.source or meta.get("provenance", ""))
        trail = list(meta.get("auditTrail", []))
        trail.append(_audit_entry(provenance, action, actor=actor, note=note))
        meta["auditTrail"] = trail
        self.graph.set_meta(fact.id, meta)

    # --- reads -------------------------------------------------------------
    def list(self, state: str | None = None) -> list[Candidate]:
        facts = self.graph.all_facts(state)
        rivals = self._rival_map()
        return [self._to_candidate(f, rivals) for f in facts]

    def get(self, cid: str) -> Candidate | None:
        fact = self.graph.get_fact(cid)
        if fact is None:
            return None
        return self._to_candidate(fact)

    # --- mutations ---------------------------------------------------------
    def create(self, body: dict[str, Any]) -> Candidate:
        body = body or {}
        title = str(body.get("title", "")).strip()
        content = str(body.get("content", "")).strip()
        if not title or not content:
            raise ValueError("title and content are required")
        provenance = str(body.get("provenance") or f"human-gate/manual:{_now()}")
        meta: dict[str, Any] = {
            "title": title,
            "auditTrail": [_audit_entry(provenance, "created")],
        }
        fid = self.graph.write(
            content,
            state="proposed",
            source=provenance,
            meta=meta,
        )
        if fid is None:
            raise ValueError("failed to create candidate")
        # Opt-in: auto-resolve any high-confidence contradiction this add tripped
        # (deterministic numeric/stance clash) so it never lands pending. Low-
        # confidence (gray-zone) contradictions are left for manual review.
        if auto_resolve_enabled():
            self.auto_resolve_high_confidence(prefer_id=fid)
        candidate = self.get(fid)
        if candidate is None:
            raise ValueError(f"candidate {fid} not found after create")
        return candidate

    def promote(self, cid: str, target: str | None = None) -> Candidate:
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
        if fact.state == "rejected":
            if target is not None and target != "active":
                raise PromotionError(f"cannot re-approve to {target!r}")
            return self._reapprove(cid, fact)
        nxt = _NEXT_STATE.get(fact.state)
        if nxt is None:
            raise PromotionError(f"cannot promote from state {fact.state!r}")
        if target is not None and target != nxt:
            raise PromotionError(f"expected target {nxt!r}, got {target!r}")
        self.graph.set_state(cid, nxt)
        self._append_audit(fact, f"promoted_to_{nxt}")
        candidate = self.get(cid)
        assert candidate is not None
        return candidate

    def _reapprove(self, cid: str, fact: Any) -> Candidate:
        """FR-010: flip a rejected fact to active and demote every currently-active
        fact it contradicts (so the pair is never both active, FR-005), keeping the
        link as ``contradicted_by``. Only direct contradictors change — no cascade
        into their other links (FR-009). The response lists each demoted fact with
        its other-contradictions flag for the review notice (FR-008)."""
        self.graph.set_state(cid, "active")
        self._append_audit(fact, "promoted_to_active", note="re-approved")
        rejected_info: list[dict[str, Any]] = []
        for rival_id in self._rival_map().get(cid, {}):
            rival = self.graph.get_fact(rival_id)
            if rival is None or rival.state != "active":
                continue
            self.graph.set_state(rival_id, "rejected")
            # Bi-temporal invalidation (additive): the re-approved fact wins, so
            # close each demoted rival's validity window at the winner's valid_at.
            _invalidate(self.graph, rival_id, cid)
            self.graph.flip_edge_kind(
                cid, rival_id, from_kind="contradiction", to_kind="contradicted_by"
            )
            self._append_audit(rival, "superseded", note=f"demoted by re-approval of {cid}")
            rejected_info.append(
                {
                    "id": rival_id,
                    "hasOtherContradictions": self._has_other_contradictions(
                        rival_id, exclude=cid
                    ),
                }
            )
        candidate = self.get(cid)
        assert candidate is not None
        candidate["rejected"] = rejected_info
        return candidate

    def reject(self, cid: str, reason: str | None = None) -> Candidate:
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
        self.graph.set_state(cid, "rejected")
        self._append_audit(fact, "rejected", note=reason)
        candidate = self.get(cid)
        assert candidate is not None
        # Manual reject has no causing contradiction, so any link counts (FR-008).
        candidate["hasOtherContradictions"] = self._has_other_contradictions(cid)
        return candidate

    def update(self, cid: str, body: dict[str, Any]) -> Candidate:
        body = body or {}
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
        meta = dict(fact.meta or {})
        title = meta.get("title", "")
        text = fact.text
        source = fact.source
        confidence = None
        if "title" in body:
            title = str(body["title"]).strip()
        if "content" in body:
            text = str(body["content"]).strip()
        if "provenance" in body:
            source = str(body["provenance"]).strip()
        if "confidence" in body:
            confidence = float(body["confidence"])
        if not str(title).strip() or not str(text).strip():
            raise ValueError("title and content are required")
        meta["title"] = title
        self.graph.update_fact(
            cid,
            text=text,
            source=source,
            confidence=confidence,
            meta=meta,
        )
        # Re-read so the audit entry sees the updated source/meta.
        fact = self.graph.get_fact(cid)
        assert fact is not None
        self._append_audit(fact, "edited")
        candidate = self.get(cid)
        assert candidate is not None
        return candidate

    def delete(self, cid: str) -> None:
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
        if fact.state == "active":
            raise DeletionError("reject the fact before deleting")
        # proposed/rejected only; fact_edges cascade via ON DELETE CASCADE (FR-015).
        self.graph.delete_fact(cid)

    # --- contradictions ----------------------------------------------------
    def _slot_info_multi(
        self, fact_ids: list[str]
    ) -> dict[str, list[tuple[tuple[str, str], str]]]:
        """Map each fact id to *all* its functional claims' ((subject, attribute), value).

        Reads slots from the claims table (PostgresVectorGraph.claims_for) when
        available, otherwise from in-memory ``Fact.claims`` (VectorGraph). A fact
        can compete on more than one slot (a compound rule), so every functional
        claim is kept — each contradiction edge is later attributed to the slot its
        two facts share. Facts with no functional claim are omitted, so clustering
        degrades gracefully to per-pair clusters when no claims exist.
        """
        out: dict[str, list[tuple[tuple[str, str], str]]] = {}
        claims_for = getattr(self.graph, "claims_for", None)
        for fid in fact_ids:
            claims = None
            if callable(claims_for):
                try:
                    claims = claims_for(fid)
                except Exception:
                    claims = None
            if claims is None:
                fact = self.graph.get_fact(fid)
                claims = getattr(fact, "claims", None) if fact is not None else None
            slots = [
                (claim.slot, claim.value)
                for claim in (claims or [])
                if getattr(claim, "functional", False)
            ]
            if slots:
                out[fid] = slots
        return out

    def contradictions(self) -> list[Candidate]:
        """Clustered pending-contradictions view (FR-013a): one item per conflicting
        claim slot, restricted to pairs where at least one side is still pending.

        Each cluster lists its competing member facts (with their value on the
        slot) and the underlying pairwise ``pairs`` — so the per-pair resolve
        endpoint keeps working unchanged. A 2-fact conflict is a cluster of 2.
        Resolved pairs are discoverable per-fact but drop out of this list.
        """
        candidates = self.list()
        pairs = serialize_pairs(candidates, status_filter="pending")
        member_ids = sorted(
            {side["id"] for p in pairs for side in (p["a"], p["b"])}
        )
        slot_info = self._slot_info_multi(member_ids)
        return serialize_clusters(candidates, slot_info, status_filter="pending")

    def auto_resolve_high_confidence(self, *, prefer_id: str | None = None) -> list[str]:
        """Auto-resolve pending contradictions that are high-confidence clashes.

        Opt-in (caller gates on :func:`auto_resolve_enabled`). For every pending
        contradiction whose two facts share a functional slot with a *deterministic*
        clash (numeric/stance — the same high-confidence signal the structural
        detector resolves without an LLM), settle it through the existing
        :meth:`resolve` supersede path: the winner stays active, the loser is
        rejected, and the edge is flipped to ``contradicted_by`` (reversible).

        The winner is ``prefer_id`` when it is one of the pair (a fresh add wins
        over the stale fact it contradicts, FR-005-style "newest approved truth");
        otherwise the newer fact (``all_facts`` is newest-first). Gray-zone
        (free-text) contradictions are left pending. Returns the resolved pair ids.
        """
        facts = self.graph.all_facts()  # newest-first
        order = {f.id: i for i, f in enumerate(facts)}  # lower index == newer
        slot_info = self._slot_info_multi(list(order))
        candidates = self.list()
        resolved: list[str] = []
        for pair in serialize_pairs(candidates, status_filter="pending"):
            a, b = pair["a"]["id"], pair["b"]["id"]
            shared = {s for s, _ in slot_info.get(a, [])} & {
                s for s, _ in slot_info.get(b, [])
            }
            val_a = dict(slot_info.get(a, []))
            val_b = dict(slot_info.get(b, []))
            high = any(
                _deterministic_clash(slot[1], val_a[slot], val_b[slot])
                for slot in shared
                if slot in val_a and slot in val_b
            )
            if not high:
                continue  # gray-zone -> leave pending for manual review
            if prefer_id in (a, b):
                keep = prefer_id
            else:
                keep = a if order.get(a, 1 << 30) <= order.get(b, 1 << 30) else b
            self.resolve(pair["id"], keep)
            resolved.append(pair["id"])
        return resolved

    def resolve(self, pair_id: str, keep_id: str) -> Candidate:
        a, _, b = pair_id.partition("__")
        loser_id = b if keep_id == a else a
        kept = self.graph.get_fact(keep_id)
        if kept is None:
            raise KeyError(keep_id)
        loser = self.graph.get_fact(loser_id)
        # The kept side wins the dispute and (re)enters the active graph; the loser
        # is rejected. The pair stays linked: flip the pending contradiction edge to
        # contradicted_by rather than deleting it, so the resolution is discoverable
        # and reversible (FR-004) and the loser's text is preserved (SC-001).
        self.graph.set_state(keep_id, "active")
        self._append_audit(kept, "kept_over_contradiction", note=f"superseded {loser_id}")
        if loser is not None:
            self.graph.set_state(loser_id, "rejected")
            # Bi-temporal invalidation (additive to the rejected state): close the
            # loser's validity window at the winner's valid_at so it drops out of
            # default recall but remains for point-in-time `as_of` queries.
            _invalidate(self.graph, loser_id, keep_id)
            self.graph.flip_edge_kind(
                keep_id, loser_id, from_kind="contradiction", to_kind="contradicted_by"
            )
            self._append_audit(
                loser, "superseded", note=f"lost contradiction to {keep_id}"
            )
        candidate = self.get(keep_id)
        assert candidate is not None
        # FR-008: flag whether the rejected loser has a contradiction beyond this one.
        candidate["hasOtherContradictions"] = (
            self._has_other_contradictions(loser_id, exclude=keep_id)
            if loser is not None
            else False
        )
        return candidate

    def resolve_custom(self, cluster_id: str, custom_text: str) -> Candidate:
        """Settle a contradiction cluster by rejecting its members and adding a new,
        user-authored fact that *supersedes* them.

        ``cluster_id`` is the slot-cluster's id — the disputed fact ids joined by
        ``"__"`` (a plain pair ``"a__b"`` is the 2-member case). The button means
        two separable things: (1) reject the disputed facts — the actual human
        decision — and (2) add a fresh fact. The new fact is deliberately **not**
        asserted to contradict the rejected ones (it may be a looser/orthogonal
        rule that doesn't conflict at all); it is linked to each by a directional
        ``supersedes`` edge — a discoverable, reversible replacement relationship
        (FR-004), not a fabricated contradiction. Whether the new fact contradicts
        any *surviving* active fact is decided by the write policy's detector,
        exactly as for any other add.
        """
        text = (custom_text or "").strip()
        if not text:
            raise ValueError("custom_text is required")
        member_ids = [fid for fid in cluster_id.split("__") if fid]
        present = {
            fid: fact
            for fid in member_ids
            if (fact := self.graph.get_fact(fid)) is not None
        }
        if not present:
            raise KeyError(cluster_id)
        ids = list(present)
        # The dispute is settled by supersession, not by one side winning: drop the
        # pending contradiction edges among the members and reject each (text intact).
        for i, x in enumerate(ids):
            for y in ids[i + 1 :]:
                self.graph.remove_edge(x, y, "contradiction")
        for fid, fact in present.items():
            self.graph.set_state(fid, "rejected")
            self._append_audit(fact, "superseded", note="resolved by custom resolution")
        provenance = f"human-gate/custom-resolution:{_now()}"
        meta: dict[str, Any] = {
            "title": _short_title(text),
            "auditTrail": [
                _audit_entry(
                    provenance,
                    "created",
                    note=f"custom resolution superseding {', '.join(ids)}",
                )
            ],
        }
        # A normal active add: the policy extracts the new fact's own claims and
        # detects any genuine contradiction with surviving facts.
        new_id = self.graph.write(text, state="active", source=provenance, meta=meta)
        if new_id is None:
            raise ValueError("failed to create custom resolution candidate")
        # Link the new fact to each superseded fact (directional: new supersedes old).
        # Bi-temporal invalidation (additive): close each superseded member's
        # validity window at the new fact's valid_at, keeping the rows for
        # point-in-time recall.
        for fid in present:
            self.graph.add_edge(new_id, fid, "supersedes")
            _invalidate(self.graph, fid, new_id)
        candidate = self.get(new_id)
        assert candidate is not None
        return candidate
