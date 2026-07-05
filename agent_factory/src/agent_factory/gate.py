"""The uniform gate contract — the factory's self-test integration seam (Milestone 1).

Every deterministic verifier the factory runs (the plan done-gate today; the
external-signal ``verify`` gate and the ``memory-audit`` later) reduces to the same
shape: take some component-specific ``input``, decide ``admitted`` yes/no, and report
the rule-IDs that fired as structured :class:`Reason` records. Pinning that shape in
one contract is what lets the meta-eval reason about *any* gate uniformly — coverage,
RED-proof, and harvesting all read :class:`Verdict` objects, never component internals.

Design rules:
- **Structured rule-IDs, never parsed (KTD5).** A :class:`Reason` carries an explicit
  ``rule_id`` field; callers match on the field, not on a string prefix.
- **One registry.** :data:`REGISTRY` maps a component name to its :class:`Gate`; the
  eval harness dispatches through it so adding a component is data, not new dispatch code.
- **Emission rides the existing event log (KTD1).** :func:`emit_gate_result` appends the
  already-defined ``gate_result`` event type — this module never extends the vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Reason:
    """One reason a gate fired, tagged with the stable rule-ID that produced it.

    ``rule_id`` is a structured field (KTD5) — e.g. ``"R-NO-DANGLING"`` — so coverage
    and harvesting can attribute a verdict to a rule without parsing ``message``.
    """

    rule_id: str
    message: str


@dataclass
class Verdict:
    """A gate's decision: ``admitted`` is True only when ``reasons`` is empty.

    ``reasons`` are the structured :class:`Reason` records that fired. ``rule_ids``
    derives the distinct fired rule-IDs (order-preserving) for emission and coverage.
    """

    admitted: bool
    reasons: list[Reason] = field(default_factory=list)

    @property
    def rule_ids(self) -> list[str]:
        """The distinct rule-IDs that fired, in first-seen order."""
        seen: dict[str, None] = {}
        for r in self.reasons:
            seen.setdefault(r.rule_id, None)
        return list(seen)


@runtime_checkable
class Gate(Protocol):
    """A deterministic verifier: component-specific ``input`` -> :class:`Verdict`."""

    def evaluate(self, input: Any) -> Verdict:  # noqa: A002 - contract name
        ...


#: Maps a component name to its :class:`Gate` implementation. Implementations
#: register themselves at import time (see ``plan_gate.PlanGate``); the eval harness
#: dispatches ``produce_verdict`` through this mapping.
REGISTRY: dict[str, Gate] = {}


def register(name: str, gate: Gate) -> Gate:
    """Register ``gate`` under ``name`` (idempotent overwrite) and return it."""
    REGISTRY[name] = gate
    return gate


def emit_gate_result(log: Any, component: str, verdict: Verdict, *, task_id: str) -> dict:
    """Append a ``gate_result`` event recording one gate run, and return the record.

    Reuses the existing ``gate_result`` event type (no vocabulary change). The event
    carries ``{component, admitted, rule_ids, task_id}``; ``task_id`` is the correlation
    key a later ``outcome`` event shares so the harvester can pair a passed gate with a
    failed outcome. ``log`` is any object exposing ``EventLog.append``.
    """
    return log.append(
        "gate_result",
        component=component,
        admitted=verdict.admitted,
        rule_ids=verdict.rule_ids,
        task_id=task_id,
    )
