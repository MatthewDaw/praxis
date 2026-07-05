"""Validation-check harness: the coding-agent half of the coverage spine.

See ``docs/coverage-spine/05-coverage-engine.md`` (validation instantiation). A **validation
check** is a live assertion bound to a requirement: "to count this ticket done, THIS must
pass" (e.g. "login works end-to-end against the live service via the playwright-cli"). **What
gets tested lives ENTIRELY in Praxis** (the validation graph: `category="check"`,
`scope="validation"`, with `meta.applies_to` + `meta.run`); a skill inserts a check there, and
the harness pulls the applicable checks from Praxis for any situation and runs them. Nothing
about *what* is tested lives in a file — only *how* (this module + the skills).

The forcing loop this enables:

    add a validation bound to a requirement  (it starts ``unrun``)
      -> the requirement is now validation-INCOMPLETE (a bound check isn't passing)
      -> the factory regresses the ticket (record_outcome "failed") so it re-enters the build set
      -> the coding agent re-picks it, RUNS the check, and must make it pass
      -> only when every bound check passes does the ticket count complete again

This module is the **deterministic core**: bind checks to requirements and decide which
requirements are validation-incomplete. The Praxis writes (regress / record pass) and running
the check command are the skill's job (af-build); they consume this.

Pure: checks come in as Praxis fact dicts (the skill queries Praxis via the knowledge-port
policy, docs/af-memory-policy.md); this
module makes no Praxis calls and no file I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PASSED = "passed"
FAILED = "failed"
UNRUN = "unrun"
_RESULTS = (PASSED, FAILED, UNRUN)


@dataclass(frozen=True)
class ValidationCheck:
    """One live validation bound to a requirement (or a class of them).

    ``applies_to`` is a requirement id (exact) OR a class/tag (e.g. ``"auth"``) that matches
    every requirement carrying that tag. ``run`` is the command/test that proves it (e.g. a
    playwright-cli invocation). ``last_result`` is the latest outcome of running it.
    """

    id: str
    applies_to: str
    criterion: str = ""
    run: str = ""
    last_result: str = UNRUN


@dataclass(frozen=True)
class ReqRef:
    """A requirement reduced to what binding needs: its id and its class tags."""

    id: str
    tags: tuple[str, ...] = ()


@dataclass
class ValidationState:
    """Per-requirement validation outcome (which checks bind, and whether all pass)."""

    requirement_id: str
    checks: list[ValidationCheck] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """A requirement is validation-complete only when EVERY bound check has passed."""
        return all(c.last_result == PASSED for c in self.checks)

    @property
    def unsatisfied(self) -> list[ValidationCheck]:
        return [c for c in self.checks if c.last_result != PASSED]


def resolve_bindings(
    checks: list[ValidationCheck], requirements: list[ReqRef]
) -> dict[str, ValidationState]:
    """Bind each check to the requirements it applies to (by id OR class tag).

    Returns only requirements that have ≥1 bound check, keyed by requirement id. A check whose
    ``applies_to`` matches no requirement is simply unbound here (the caller can surface those
    as orphans).
    """
    states: dict[str, ValidationState] = {}
    for req in requirements:
        bound = [c for c in checks if c.applies_to == req.id or c.applies_to in req.tags]
        if bound:
            states[req.id] = ValidationState(requirement_id=req.id, checks=bound)
    return states


def select_validation_incomplete(states: dict[str, ValidationState]) -> list[str]:
    """The requirement ids that have ≥1 bound validation check NOT yet passing.

    These must (re)enter the build set. A newly-added check (``unrun``) lands a requirement
    here immediately — that is the regress trigger.
    """
    return [rid for rid, st in states.items() if not st.complete]


def unbound_checks(
    checks: list[ValidationCheck], requirements: list[ReqRef]
) -> list[ValidationCheck]:
    """Checks whose ``applies_to`` matched no requirement (a binding/typo bug to surface)."""
    bound_ids = {
        c.id
        for req in requirements
        for c in checks
        if c.applies_to == req.id or c.applies_to in req.tags
    }
    return [c for c in checks if c.id not in bound_ids]


def checks_from_facts(facts: list[dict]) -> list[ValidationCheck]:
    """Build validation checks from Praxis fact dicts (`category="check"`, `scope="validation"`).

    The skill queries Praxis for the active validation checks and hands the raw facts here. Each
    fact's `meta` carries `applies_to` (a requirement id or class tag), `run` (the command), and
    `check_id`; the criterion is `meta.criterion` or the fact `text`. The checks themselves never
    live in a file — this is the bridge from Praxis-stored checks to the pure binding/selection
    logic above. A fact with no `applies_to` is skipped (it binds to nothing).
    """
    out: list[ValidationCheck] = []
    for f in facts:
        meta = f.get("meta") or {}
        applies_to = str(meta.get("applies_to", "")).strip()
        if not applies_to:
            continue
        result = str(meta.get("last_result", UNRUN)).strip().lower()
        out.append(
            ValidationCheck(
                id=str(meta.get("check_id") or f.get("id") or ""),
                applies_to=applies_to,
                criterion=str(meta.get("criterion") or f.get("text", "")),
                run=str(meta.get("run", "")),
                last_result=result if result in _RESULTS else UNRUN,
            )
        )
    return out
