"""Deterministic checks + per-component runners for the eval harness.

A check is ``Callable[[verdict, **params], CheckResult]`` resolved from a
``CheckRef.ref`` of the form ``"module:function"``. A component runner turns a
case's ``input`` block into the verdict the checks assert against.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from agent_factory.gate import REGISTRY, Verdict

# Importing plan_gate registers the "plan_gate" Gate into REGISTRY (import side effect).
import agent_factory.plan_gate  # noqa: F401

GateVerdict = Verdict  # backward-compatible alias for the shared verdict type


@dataclass
class CheckResult:
    name: str
    passed: bool
    evidence: str = ""


# --- component dispatch: case.input -> produced verdict ------------------------


def produce_verdict(component: str, case_input: dict) -> Verdict:
    """Dispatch ``case_input`` to the registered gate for ``component``.

    Raises ``ValueError`` for an unknown component (same contract as before, now
    sourced from the shared :data:`~agent_factory.gate.REGISTRY`).
    """
    try:
        gate = REGISTRY[component]
    except KeyError:
        raise ValueError(f"unknown component {component!r}")
    return gate.evaluate(case_input)


# --- checks --------------------------------------------------------------------


def _messages(verdict: Verdict) -> list[str]:
    """The human-readable message text of each reason (rule-ID lives on the Reason)."""
    return [r.message for r in verdict.reasons]


def gate_admits(verdict: GateVerdict) -> CheckResult:
    """Pass iff the gate admitted the plan (no rejection reasons)."""
    return CheckResult(
        name="gate_admits",
        passed=verdict.admitted,
        evidence="admitted" if verdict.admitted else f"rejected: {_messages(verdict)}",
    )


def gate_rejects(verdict: GateVerdict, reason_contains: str | None = None) -> CheckResult:
    """Pass iff the gate rejected the plan; if ``reason_contains`` is given, also
    require some reason message to contain that substring (case-insensitive)."""
    messages = _messages(verdict)
    if verdict.admitted:
        return CheckResult(name="gate_rejects", passed=False, evidence="unexpectedly admitted")
    if reason_contains is None:
        return CheckResult(name="gate_rejects", passed=True, evidence=str(messages))
    needle = reason_contains.lower()
    hit = any(needle in m.lower() for m in messages)
    return CheckResult(
        name="gate_rejects",
        passed=hit,
        evidence=(
            f"reason matching {reason_contains!r} found"
            if hit
            else f"no reason matched {reason_contains!r}; got {messages}"
        ),
    )


def resolve_check(ref: str):
    """Resolve a ``"module:function"`` ref to the callable."""
    module_name, func_name = ref.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, func_name)
