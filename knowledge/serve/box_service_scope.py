"""Job scope completeness (R4): a job targets exactly one prd snapshot and runs
until every requirement in its build scope is finished or blocked.

"Scope" is af-build's own established build target, not a per-job free-form
filter: the ``mvp`` tier crossed with ``automated`` verification -- the same
partition ``agent_factory.build_target.select_build_target`` computes for the
local build-completeness gate. The box service does not import the
``agent_factory`` plugin package (it is not deployed with the backend), so
this module mirrors that selection locally.

A ``post-mvp`` requirement, or an ``mvp`` requirement whose ``verify`` is
``manual`` (or anything unrecognized), is never consulted here -- it can
never hold a job open, matching af-build's own fail-safe routing (an
unrecognized tag goes to triage, never into the automated build set).
"""

from __future__ import annotations

from typing import Any

_TIER_MVP = "mvp"
_VERIFY_AUTOMATED = "automated"

#: A requirement's ``build_state`` counts as "done" for job-scope purposes once it
#: is either genuinely finished or terminally blocked (surfaced for owner action,
#: excluded from further churn) -- see docs/factory-state-contract.md.
_DONE_STATES = frozenset({"finished", "blocked"})


def _norm(value: Any) -> str | None:
    """Lower-case/strip a tag value; a missing or non-string value is unrecognized."""
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    return stripped or None


def in_job_scope(requirement_fact: dict) -> bool:
    """True iff ``requirement_fact`` is in the job's build scope: tier ``mvp`` AND
    verify ``automated`` -- af-build's own ``build_target.select_build_target``
    "build" group. Fail-safe: a missing/unrecognized tier or verify is NOT in
    scope, so a mis-tagged requirement can never silently hold a job open.
    """
    meta = requirement_fact.get("meta") or {}
    return (
        _norm(meta.get("scope")) == _TIER_MVP
        and _norm(meta.get("verify")) == _VERIFY_AUTOMATED
    )


def _build_state(requirement_fact: dict) -> str:
    meta = requirement_fact.get("meta") or {}
    return _norm(meta.get("build_state")) or "incomplete"


def job_scope_complete(requirement_facts: list[dict]) -> bool:
    """R4: True iff every in-scope (mvp + automated) requirement in
    ``requirement_facts`` has reached ``build_state`` "finished" or "blocked".

    A snapshot with no in-scope requirements at all (e.g. claimed with zero
    incomplete tickets) is vacuously complete -- the job should not stay
    running waiting on work that was never in its scope. Post-mvp and
    manual-verify requirements are never inspected, so they can never hold
    the job open.
    """
    return all(
        _build_state(fact) in _DONE_STATES
        for fact in requirement_facts
        if isinstance(fact, dict) and in_job_scope(fact)
    )
