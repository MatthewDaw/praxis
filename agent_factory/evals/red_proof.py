"""RED-proof: per-case falsifiability evidence (the eval harness's U3 leaf; R7, AE2).

A green eval suite proves the cases *agree* with the gate, never that any case would
*catch a regression*. A case that has never been observed failing is decorative — it
inflates coverage without defending a rule. U3 closes that gap by demanding, per case,
recorded RED-proof: evidence that the case actually goes RED somewhere.

RED-proof is dual-sourced (KTD3):

- **fixture** — a hand-authored case names a pinned *broken-gate fixture* (a deliberate
  one-rule mutant) and must fail against it. Running the case's own deterministic
  checks against the broken gate has to flip at least one check to RED; if it does, the
  case demonstrably falsifies, so the rule it guards cannot silently rot.
- **harvested** — a case mined from the event log carries its originating ``gate_result``
  as the RED observation (the escape itself); that event *is* the evidence (KTD2).

A case whose ``red_proof`` is absent, or names no usable fixture, is **decorative** and
is quarantined: :func:`verify_red_proof` reports it and it is excluded from the
RED-proven set that coverage (U2) counts. A case that names a fixture but still *passes*
against it is **bogus** — a non-falsifying RED-proof, surfaced so it can be fixed.

The broken-gate fixtures mirror the isolated-broken-component probe style: construct a
gate that is wrong in exactly one way, then assert the case detects the wrongness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_factory.gate import Gate, Verdict
from agent_factory.plan_gate import (
    R_ACCEPT_BINARY,
    R_NO_DANGLING,
    R_NO_VAGUE,
    PlanGate,
)
from evals.case_def import EvalCase
from evals.checks import CheckResult, resolve_check

# RED-proof verdicts a case can earn against its declared evidence.
VERIFIED = "verified"      # the case demonstrably goes RED (falsifiability shown)
DECORATIVE = "decorative"  # no usable red_proof; quarantined, excluded from coverage
BOGUS = "bogus"            # names a fixture but does not flip the verdict (non-falsifying)


@dataclass(frozen=True)
class BrokenGate:
    """A pinned ``plan_gate`` mutant with exactly one rule disabled.

    Runs the real :class:`~agent_factory.plan_gate.PlanGate` and then suppresses every
    :class:`~agent_factory.gate.Reason` whose ``rule_id`` is ``disabled_rule``. This
    mirrors the "force this check to always pass" mutation (U5): an input that should be
    rejected *for that rule alone* is now wrongly admitted. A reject case exercising only
    ``disabled_rule`` therefore goes RED against this gate — exactly the falsifiability
    evidence U3 records.
    """

    disabled_rule: str

    def evaluate(self, input: Any) -> Verdict:  # noqa: A002 - contract name
        full = PlanGate().evaluate(input)
        kept = [r for r in full.reasons if r.rule_id != self.disabled_rule]
        return Verdict(admitted=not kept, reasons=kept)


#: One broken-gate fixture per ``plan_gate`` rule (OQ4: start per-rule). Each wrongly
#: admits inputs that violate only its disabled rule; a case naming the fixture must fail
#: against it. Keyed by the rule-ID the fixture breaks.
BROKEN_GATES: dict[str, Gate] = {
    R_ACCEPT_BINARY: BrokenGate(R_ACCEPT_BINARY),
    R_NO_VAGUE: BrokenGate(R_NO_VAGUE),
    R_NO_DANGLING: BrokenGate(R_NO_DANGLING),
}


@dataclass(frozen=True)
class RedProofResult:
    """The outcome of auditing one case's RED-proof.

    ``status`` is one of :data:`VERIFIED` / :data:`DECORATIVE` / :data:`BOGUS`;
    ``fixture`` is the named broken-gate fixture (``None`` for absent or harvested
    evidence); ``detail`` is a human-readable explanation for the coverage report.
    """

    case_id: str
    status: str
    fixture: str | None
    detail: str

    @property
    def verified(self) -> bool:
        """True iff the case demonstrably goes RED against its evidence."""
        return self.status == VERIFIED

    @property
    def decorative(self) -> bool:
        """True iff the case declares no usable RED-proof (quarantined)."""
        return self.status == DECORATIVE

    @property
    def bogus(self) -> bool:
        """True iff the named fixture fails to flip the verdict (non-falsifying)."""
        return self.status == BOGUS


def _run_checks(case: EvalCase, gate: Gate) -> list[CheckResult]:
    """Run every deterministic check of ``case`` against ``gate``'s verdict."""
    verdict = gate.evaluate(case.input)
    return [resolve_check(c.ref)(verdict, **c.params) for c in case.deterministic_checks]


def verify_red_proof(case: EvalCase) -> RedProofResult:
    """Audit one case's RED-proof and classify it verified / decorative / bogus.

    For a ``fixture`` proof, runs the case's own deterministic checks against the named
    broken-gate fixture: if at least one check flips to RED the proof is :data:`VERIFIED`,
    otherwise it is :data:`BOGUS` (the fixture does not falsify the case). A ``harvested``
    proof is verified by its originating event (KTD3) — the escape itself is the RED
    observation. Absent or unusable evidence is :data:`DECORATIVE`.
    """
    proof = case.red_proof
    if not proof:
        return RedProofResult(
            case.id, DECORATIVE, None,
            "no red_proof declared; case cannot demonstrate falsifiability",
        )

    # Harvested evidence: the originating gate_result IS the RED observation (KTD2/KTD3).
    if proof.get("kind") == "harvested" or "event" in proof:
        return RedProofResult(
            case.id, VERIFIED, None,
            f"harvested RED evidence: {proof.get('event', proof)!r}",
        )

    fixture = proof.get("fixture")
    gate = BROKEN_GATES.get(fixture) if fixture is not None else None
    if gate is None:
        return RedProofResult(
            case.id, DECORATIVE, fixture,
            f"red_proof names no usable broken-gate fixture ({fixture!r}); "
            f"known fixtures: {sorted(BROKEN_GATES)}",
        )

    results = _run_checks(case, gate)
    failing = [r.name for r in results if not r.passed]
    if failing:
        return RedProofResult(
            case.id, VERIFIED, fixture,
            f"case goes RED against broken {fixture} (failing checks: {failing})",
        )
    return RedProofResult(
        case.id, BOGUS, fixture,
        f"case still PASSES against broken {fixture}; red_proof does not falsify it",
    )


def audit_red_proofs(cases: list[EvalCase]) -> list[RedProofResult]:
    """Audit every case's RED-proof (preserving input order)."""
    return [verify_red_proof(c) for c in cases]


def red_proven_case_ids(cases: list[EvalCase]) -> set[str]:
    """The IDs of cases whose RED-proof is :data:`VERIFIED`.

    Coverage (U2) reads only these when computing ``rules_without_red_case`` so a
    decorative or bogus case cannot prop up a rule's RED-coverage claim.
    """
    return {r.case_id for r in audit_red_proofs(cases) if r.verified}


def decorative_results(cases: list[EvalCase]) -> list[RedProofResult]:
    """The audit results for cases that are quarantined as decorative (AE2 report)."""
    return [r for r in audit_red_proofs(cases) if r.decorative]
