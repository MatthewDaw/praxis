"""U9 — the gather-loop orchestrator (the interactive sibling of af-build's autonomous loop).

KTD2: the actor is an end user in a chat, so the loop advances PER USER TURN — request/response, not
a Stop-hook-gated autonomous loop. One turn (:meth:`Conversation.handle_turn`):

1. **guardrail** the message (S9) — a typed refusal (1099 / advice) blocks the turn and fabricates
   nothing; an SSN is redacted and the turn continues;
2. interpret it — the **answer** to a pending question, else a **document** to extract (U7);
3. **settle + default** — record every requirement whose value is now known (cover-from-fact /
   readback / default), driving ``incomplete_requirements`` down via ``record_outcome``;
4. when the MVP target is empty, the **completeness gate** opens → run the evaluator's ``final`` pass
   (U2) and produce the deliverable (U8); else ask the single highest-materiality open requirement
   (U4) through the budgeted ask channel (S1).

Every step emits a typed trace event (S3). Fail-closed on Praxis reads (the control flow reads
completeness live). The completeness gate — NOT a Stop hook — is what permits ``produce``; producing
before completeness is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain import Domain
from .evaluator import bottom_line, evaluate
from .extract import extractor_for
from .formfill import Deliverable, produce_deliverable
from .policy import Budget, Guardrails
from .requirements import (
    ASK,
    DEFAULT,
    FulfillRequirement,
    deps_met,
    default_token,
    rank_open,
    requirement_from_fact,
)
from .session import Session
from .validate import validate_cross_field, validate_field

MVP = "mvp"


@dataclass
class Event:
    """One typed trace event (S3). ``type`` is the event kind; ``data`` carries its fields."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    """What one user turn produced: the trace events, an optional question / refusal, and — when the
    completeness gate opened — the deliverable."""

    events: list[Event] = field(default_factory=list)
    question: str | None = None
    refusal: str | None = None
    deliverable: Deliverable | None = None
    done: bool = False
    blocked: list[str] = field(default_factory=list)


class ProduceBeforeComplete(RuntimeError):
    """Refused: the deliverable cannot be produced while MVP requirements remain incomplete."""


class InvariantViolation(RuntimeError):
    """Refused: the gathered facts violate a cross-field invariant (S6) — never produce from one."""


class Conversation:
    """A live fulfill session's turn-by-turn state machine over a seeded Praxis space."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.domain: Domain = session.domain
        self.client = session.client
        self.project = session.project
        self.budget = Budget.from_domain(self.domain)
        self.guardrails = Guardrails(self.domain)
        self.extractor = extractor_for(self.domain)

        self.facts: dict[str, Any] = {}
        self.cover_sources: dict[str, str] = {}
        self.defaulted_fields: list[str] = []
        self.trace: list[Event] = []
        self.pending_ask: FulfillRequirement | None = None
        self.deliverable: Deliverable | None = None
        self.done = False

    # --- public API ---------------------------------------------------------
    def handle_turn(self, message: str) -> TurnResult:
        """Advance one user turn end-to-end."""
        events: list[Event] = []

        def emit(etype: str, **data: Any) -> None:
            ev = Event(etype, data)
            self.trace.append(ev)
            events.append(ev)

        # 1. guardrails (S9)
        verdict = self.guardrails.scope_check(message)
        if not verdict.allowed:
            if verdict.action == "redact":
                message = self.guardrails.redact(message)
                emit("pii_redacted", rule_id=verdict.rule_id)
            else:
                emit("guardrail_refusal", rule_id=verdict.rule_id, reason=verdict.reason)
                return TurnResult(events=events, refusal=verdict.reason)

        # 2. interpret: answer to a pending ask, else a document to extract
        if self.pending_ask is not None:
            req = self.pending_ask
            check = validate_field(self.domain, req.field, message.strip())
            if not check.ok:
                emit("validation_error", field=req.field, reason=check.reason)
                # re-ask the same question; no budget is spent on a re-ask.
                return TurnResult(events=events, question=self._question_text(req))
            self.pending_ask = None
            self.facts[req.field] = check.value
            self.cover_sources.setdefault(req.field, "user")
            emit("answer_recorded", field=req.field, source="user")
        else:
            extracted = self.extractor.extract(message)
            if extracted.fields:
                for fname, value in extracted.fields.items():
                    self.facts[fname] = value
                    self.cover_sources.setdefault(fname, "w2")
                emit("document_extracted", fields=list(extracted.fields))
            for note in extracted.notes:
                emit("extraction_note", note=note)

        # 3-4. settle, default, then ask-or-produce
        return self._advance(events, emit)

    def produce(self) -> Deliverable:
        """Produce the deliverable. Refused (raises) while any MVP requirement is incomplete, or while
        the gathered facts violate a cross-field invariant (S6)."""
        if self._incomplete_mvp():
            raise ProduceBeforeComplete(
                "cannot produce the deliverable: MVP requirements are still incomplete"
            )
        cf = validate_cross_field(self.domain, self.facts)
        if not cf.ok:
            raise InvariantViolation(cf.reason)
        return self._produce(lambda *a, **k: None)

    # --- gather mechanics ---------------------------------------------------
    def _advance(self, events: list[Event], emit) -> TurnResult:
        self._settle_and_default(emit)

        remaining = self._incomplete_mvp()
        if not remaining:
            return self._gated_produce(events, emit)

        ranked = rank_open(remaining, self.domain, self.facts)
        asks = [r for r in ranked if r.disposition == ASK]
        if asks:
            req = asks[0].req
            if self.budget.can_ask():
                self.budget.spend()
                self.pending_ask = req
                emit("question_asked", field=req.field, remaining=self.budget.remaining)
                return TurnResult(events=events, question=self._question_text(req))
            # budget exhausted: refuse the question, fall back to defaulting what we can (S1).
            emit("budget_exhausted", max=self.budget.max)
            self._default_remaining(emit)
            remaining = self._incomplete_mvp()
            if not remaining:
                return self._gated_produce(events, emit)
            blocked = [r.req.id for r in rank_open(remaining, self.domain, self.facts)]
            return TurnResult(events=events, blocked=blocked)

        # nothing askable this turn (WAIT/TRIAGE only) — surface but do not spin.
        return TurnResult(events=events)

    def _gated_produce(self, events: list[Event], emit) -> TurnResult:
        """The completeness gate has opened — but produce ONLY if no cross-field invariant is
        violated (S6). An invariant-violating state is surfaced as a blocked turn, never a
        deliverable (e.g. withholding > wages must be corrected, not filed)."""
        cf = validate_cross_field(self.domain, self.facts)
        if not cf.ok:
            emit("invariant_violation", field=cf.field, reason=cf.reason)
            return TurnResult(events=events, refusal=cf.reason, blocked=["invariant"])
        deliverable = self._produce(emit)
        return TurnResult(events=events, deliverable=deliverable, done=True)

    def _settle_and_default(self, emit) -> None:
        """Record every MVP requirement whose value is now known: cover-from-fact, the W-2 readback
        (verify=user_confirmed), or a policy default. One action per pass until nothing is actionable
        (each ``record_outcome`` drops the requirement from the live incomplete set)."""
        for _ in range(100):  # bounded; converges well within the requirement count
            target = self._incomplete_mvp()
            if not target:
                return
            ranked = rank_open(target, self.domain, self.facts)
            acted = False
            for rr in ranked:
                req = rr.req
                # (a) a value is already present (document-extracted or just answered) -> record it.
                if req.field in self.facts and self.facts[req.field] is not None:
                    cf = validate_cross_field(self.domain, self.facts)
                    if not cf.ok:
                        emit("validation_error", field=cf.field, reason=cf.reason)
                    self._cover(req, self.facts[req.field],
                                self.cover_sources.get(req.field, "user"), emit)
                    acted = True
                    break
                # (b) a W-2 readback confirmation, auto-affirmed once its deps are covered (prototype).
                if req.verify == "user_confirmed" and deps_met(req, self.domain, self.facts):
                    emit("readback", req=req.id, confirms=req.depends_on)
                    self.facts[req.field] = True
                    self._cover(req, True, "user", emit, kind="readback")
                    acted = True
                    break
                # (c) immaterial / guard-failed with a default -> close by default (free, S5 receipt).
                if rr.disposition == DEFAULT:
                    self._default(req, emit)
                    acted = True
                    break
            if not acted:
                return

    def _default_remaining(self, emit) -> None:
        """Budget-exhaustion fallback: default every still-open MVP requirement that has a default."""
        for _ in range(100):
            acted = False
            for req in self._incomplete_mvp():
                if default_token(req) is not None and req.field not in self.facts:
                    self._default(req, emit)
                    acted = True
                    break
            if not acted:
                return

    def _default(self, req: FulfillRequirement, emit) -> None:
        # _cover owns the facts/cover_sources writes; here we only compute the default value.
        self._cover(req, self._default_value(req), "default", emit, kind="defaulted")

    def _cover(self, req: FulfillRequirement, value: Any, source: str, emit, *, kind: str = "covered") -> None:
        self.facts[req.field] = value
        self.cover_sources[req.field] = source
        self.client.record_outcome(req.fact_id, True)
        if source == "default" and req.field not in self.defaulted_fields:
            self.defaulted_fields.append(req.field)
        emit("requirement_covered", req=req.id, field=req.field, source=source, kind=kind)

    def _produce(self, emit) -> Deliverable:
        results = evaluate(self.domain, self.facts, mode="final")
        deliverable = produce_deliverable(
            self.domain, results,
            facts=self.facts,
            cover_sources=self.cover_sources,
            defaulted_fields=self.defaulted_fields,
        )
        self.deliverable = deliverable
        self.done = True
        emit("deliverable_produced",
             content_hash=deliverable.content_hash,
             bottom_line=bottom_line(self.domain, results))
        return deliverable

    # --- helpers ------------------------------------------------------------
    def _incomplete_mvp(self) -> list[FulfillRequirement]:
        items = self.client.incomplete_requirements(self.project)
        reqs = [requirement_from_fact(f) for f in items]
        return [r for r in reqs if r.scope == MVP]

    def _default_value(self, req: FulfillRequirement) -> Any:
        token = default_token(req) or ""
        key = token.split(":", 1)[1] if ":" in token else req.field
        spec = (self.domain.policy.get("defaults") or {}).get(key, {})
        if isinstance(spec, dict) and "value" in spec:
            return spec["value"]
        # fall back to the field's schema-declared default-when-absent, if any.
        schema = self.domain.field_schemas.get(req.field, {})
        return schema.get("default_when_absent")

    def _question_text(self, req: FulfillRequirement) -> str:
        schema = self.domain.field_schemas.get(req.field, {})
        label = schema.get("label") or req.field.replace("_", " ")
        if schema.get("type") == "enum":
            return f"What is your {label}? ({', '.join(schema.get('values') or [])})"
        return f"What is your {label}?"
