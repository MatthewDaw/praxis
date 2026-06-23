"""Candidate read model projected over the ``facts`` spine.

This replaces the deleted candidate stores (``store.py`` /
``postgres_store.py``). Instead of a separate ``candidates`` table, the
dashboard "candidate" surface is now a projection of the tenant's facts graph
(:class:`PostgresVectorGraph`). The ``facts.state`` column carries the
proposed/active/decayed lifecycle, ``facts.meta`` carries dashboard-only fields
(``title``, ``auditTrail``, ``supersedes``), and contradiction links live in
the ``fact_edges`` table.

Tenancy is bound at construction (one facade per ``(org_id, user_id)``), so the
methods here — unlike the old explicitly-tenanted stores — take no org/user
arguments. The candidate ``id`` equals the raw fact ``id`` (no ``pipe_``/
``cand_`` namespace), so contradiction links and lifecycle ops need no id
translation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
    default_write_policy,
)
from knowledge.serve.contradiction_adapter import serialize_pairs
from knowledge.serve.pipeline_adapter import fact_to_candidate

Candidate = dict[str, Any]

# Human-gate promotion funnel: a proposed candidate is approved straight to
# active (the intermediate "suggested" step was removed). "active" and "decayed"
# are terminal — not in this map, so promoting from them raises.
_NEXT_STATE = {"proposed": "active"}


class PromotionError(ValueError):
    """Raised when a candidate can't be promoted from its current state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    def _rival_map(self) -> dict[str, list[str]]:
        """Map each fact id to the ids it contradicts (both edge directions)."""
        links: dict[str, set[str]] = {}
        for src, dst, _kind in self.graph.all_edges("contradiction"):
            links.setdefault(src, set()).add(dst)
            links.setdefault(dst, set()).add(src)
        return {k: sorted(v) for k, v in links.items()}

    def _to_candidate(self, fact: Any, rivals: dict[str, list[str]] | None = None) -> Candidate:
        rival_ids = (rivals or self._rival_map()).get(fact.id)
        return fact_to_candidate(fact, state=fact.state, rival_ids=rival_ids)

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
        candidate = self.get(fid)
        if candidate is None:
            raise ValueError(f"candidate {fid} not found after create")
        return candidate

    def promote(self, cid: str, target: str | None = None) -> Candidate:
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
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

    def reject(self, cid: str, reason: str | None = None) -> Candidate:
        fact = self.graph.get_fact(cid)
        if fact is None:
            raise KeyError(cid)
        self.graph.set_state(cid, "decayed")
        self._append_audit(fact, "rejected", note=reason)
        candidate = self.get(cid)
        assert candidate is not None
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
        if self.graph.get_fact(cid) is None:
            raise KeyError(cid)
        self.graph.delete_fact(cid)

    # --- contradictions ----------------------------------------------------
    def contradictions(self) -> list[Candidate]:
        return serialize_pairs(self.list())

    def resolve(self, pair_id: str, keep_id: str) -> Candidate:
        a, _, b = pair_id.partition("__")
        loser_id = b if keep_id == a else a
        kept = self.graph.get_fact(keep_id)
        if kept is None:
            raise KeyError(keep_id)
        loser = self.graph.get_fact(loser_id)
        # Drop the a<->b contradiction edge; the kept side wins the dispute and
        # (re)enters the active graph, the loser is decayed.
        self.graph.remove_edge(keep_id, loser_id, "contradiction")
        self.graph.set_state(keep_id, "active")
        self._append_audit(kept, "kept_over_contradiction", note=f"superseded {loser_id}")
        if loser is not None:
            self.graph.set_state(loser_id, "decayed")
            self._append_audit(
                loser, "superseded", note=f"lost contradiction to {keep_id}"
            )
        candidate = self.get(keep_id)
        assert candidate is not None
        return candidate

    def resolve_custom(self, pair_id: str, custom_text: str) -> Candidate:
        """Decay both sides + drop their edge, then create a fresh active fact."""
        text = (custom_text or "").strip()
        if not text:
            raise ValueError("custom_text is required")
        a, _, b = pair_id.partition("__")
        fact_a = self.graph.get_fact(a)
        fact_b = self.graph.get_fact(b)
        if fact_a is None and fact_b is None:
            raise KeyError(pair_id)
        self.graph.remove_edge(a, b, "contradiction")
        for fid, fact in ((a, fact_a), (b, fact_b)):
            if fact is None:
                continue
            self.graph.set_state(fid, "decayed")
            self._append_audit(fact, "superseded", note="resolved by custom resolution")
        provenance = f"human-gate/custom-resolution:{_now()}"
        meta: dict[str, Any] = {
            "title": _short_title(text),
            "supersedes": [cid for cid in (a, b) if cid],
            "auditTrail": [
                _audit_entry(
                    provenance, "created", note=f"custom resolution superseding {a}, {b}"
                )
            ],
        }
        fid = self.graph.write(text, state="active", source=provenance, meta=meta)
        if fid is None:
            raise ValueError("failed to create custom resolution candidate")
        candidate = self.get(fid)
        assert candidate is not None
        return candidate
