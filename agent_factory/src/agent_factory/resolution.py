"""FL10 (R17) — per-finding resolution keyed to the check that produced it, plus the CHECK-DEFEAT
failure class.

R17: resolution stamps the SPECIFIC finding whose check passed — a rerun passing check A must
never stamp finding B (a sibling check's finding) resolved. Resolution additionally requires the
finding's own recorded symptom to be RE-EVALUATED against the rebuilt state, not inferred from the
check's exit code alone: the merger re-checks what ``regression_detail`` recorded (its
``reason``/``evidence``), not solely whether the pinned check passed this round.

Check-passed-but-symptom-present is a first-class failure class — a CHECK-DEFEAT — feeding R3's
dedup/recurrence taxonomy (:mod:`agent_factory.failure_taxonomy`): it pins the rebuilt state's bad
artifact (FL4's :func:`agent_factory.ingestion_api.pin_artifact`), demotes the defeated check
GATING -> REPORT_ONLY and flags it (FL12/FL18's :func:`agent_factory.ingestion_api.
demote_for_check_defeat`), and routes a machine-strict redraft against that fresh pin (FL5's
:func:`agent_factory.ingestion_api.attempt_fail_then_pass_proof`).

This module never talks to ``hooks._praxis`` directly — only :mod:`agent_factory.ingestion_api`
(the sole writer), :mod:`agent_factory.failure_taxonomy` (R3 classification), and the read-only
finding accessors on ``hooks._ticket_state`` — exactly like :mod:`agent_factory.widening` orchestrates
FL14 on top of the same two modules' primitives.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hooks import _ticket_state as _ts

from agent_factory import failure_taxonomy, ingestion_api

# R17's namesake failure class: recorded via :func:`agent_factory.failure_taxonomy.assign_class`
# so a recurring check-defeat dedups/recounts through the SAME taxonomy every other failure class
# does (R3), rather than living in a parallel, unaggregated bucket of its own.
CHECK_DEFEAT_CLASS_KIND = "check-defeat"


def resolve_findings_for_check(meta: dict[str, Any], check_id: str, *,
                               resolved_by: str | None = None) -> list[dict[str, Any]]:
    """R17 — resolve ONLY the open findings recorded against ``check_id``; every OTHER open
    finding on the ticket (a sibling check's finding, or one with no ``check_id`` recorded at all)
    is left untouched. Returns the full accumulated ``regression_detail`` list, ready to write back
    verbatim, same contract as :func:`hooks._ticket_state.resolve_finding` except for this scoping
    — which is the whole point: a rerun passing check A must never stamp finding B resolved."""
    details = _ts.regression_details(meta)
    for d in details:
        if (not d.get("resolved") and str(d.get("reason") or "").strip()
                and str(d.get("check_id") or "") == str(check_id)):
            d["resolved"] = True
            if resolved_by:
                d["resolved_by"] = resolved_by
    return details


def resolve_or_defeat(
    meta: dict[str, Any],
    check_id: str,
    *,
    check_passed: bool,
    symptom_present: bool,
    project: str,
    ticket_id: str,
    commit_sha: str,
    repo_path: str | Path,
    run_candidates: list[str] | None = None,
    healthy_repo_path: str | Path | None = None,
    resolved_by: str | None = None,
    identity: str | None = None,
    repeat_count: int = ingestion_api.DEFAULT_REPEAT_COUNT,
    redraft_budget: int = ingestion_api.DEFAULT_REDRAFT_BUDGET,
    executor: Callable[[str, Path], bool] | None = None,
) -> dict[str, Any]:
    """R17 — the one call a verification round makes per (finding, check) pair it re-examined.

    ``check_passed`` is this round's real (non-drafting) execution outcome for ``check_id``.
    ``symptom_present`` is the caller's RE-EVALUATION of the finding's recorded symptom
    (``reason``/``evidence``) against the rebuilt state — resolution is never inferred from the
    check's exit code alone (R17's core requirement).

    Three outcomes:

    - check still FAILS -> nothing resolves and nothing defeats; ``status="unresolved"``, every
      finding (this check's and every sibling's) is left exactly as found.
    - check PASSES and the symptom is GONE -> exactly the findings naming ``check_id`` are stamped
      resolved (:func:`resolve_findings_for_check`); a sibling finding from a different check is
      untouched. ``status="resolved"``.
    - check PASSES but the symptom PERSISTS -> a CHECK-DEFEAT (R17's namesake): the rebuilt state's
      artifact is pinned (FL4), the defeat is classified into the taxonomy (FL3, feeding R3), the
      check is demoted GATING -> REPORT_ONLY and flagged (FL12/FL18's
      :func:`ingestion_api.demote_for_check_defeat`), and — when ``run_candidates`` are supplied —
      a machine-strict redraft is attempted against the fresh pin (FL5). The findings naming
      ``check_id`` stay OPEN (the symptom is still there — nothing about it is resolved).
      ``status="check-defeat"``.
    """
    if not check_passed:
        return {"status": "unresolved", "reason": "check-still-failing", "check_id": check_id,
                "regression_detail": _ts.regression_details(meta)}

    if not symptom_present:
        updated = resolve_findings_for_check(meta, check_id, resolved_by=resolved_by)
        return {"status": "resolved", "check_id": check_id, "regression_detail": updated}

    # Check-defeat: the check passed, but the finding's own recorded symptom is still there.
    matching = [d for d in _ts.open_findings(meta) if str(d.get("check_id") or "") == str(check_id)]
    finding = matching[0] if matching else {}
    reason = (f"check-defeat: check {check_id} passed on the rebuilt state but the finding's "
             f"recorded symptom persisted: {finding.get('reason', '')}")
    evidence = str(finding.get("evidence") or "")

    pin_ack = ingestion_api.pin_artifact(
        project=project, ticket_id=ticket_id, commit_sha=commit_sha, repo_path=repo_path,
        evidence_text=evidence, while_gating=True, source="check-defeat",
    )
    artifact_meta = ingestion_api.read_artifact(pin_ack["id"]).get("meta") or {}

    classification = failure_taxonomy.assign_class(
        reason, evidence=evidence, source="check-defeat",
        meta={"kind": CHECK_DEFEAT_CLASS_KIND, "check_id": check_id, "ticket_id": ticket_id},
    )

    demotion = ingestion_api.demote_for_check_defeat(check_id, project, reason=reason, identity=identity)

    redraft = None
    if run_candidates:
        redraft = ingestion_api.attempt_fail_then_pass_proof(
            run_candidates, bad_artifact_meta=artifact_meta,
            healthy_repo_path=healthy_repo_path or repo_path, repeat_count=repeat_count,
            redraft_budget=redraft_budget, executor=executor,
        )

    return {"status": "check-defeat", "check_id": check_id, "artifact": pin_ack,
            "classification": classification, "demotion": demotion, "redraft": redraft,
            "regression_detail": _ts.regression_details(meta)}
