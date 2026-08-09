"""FL10 (R17) — per-finding resolution keyed to the check that produced it, plus the CHECK-DEFEAT
failure class.

R17: resolution stamps the SPECIFIC finding whose check passed — a rerun passing check A must
never stamp finding B (a sibling check's finding) resolved. Resolution additionally requires the
finding's own recorded symptom to be RE-EVALUATED against the rebuilt state, not inferred from the
check's exit code alone: the merger re-checks what ``regression_detail`` recorded (its
``reason``/``evidence``), not solely whether the pinned check passed this round.

A finding therefore has exactly one legitimate answerer, decided by whether it names a check
(:func:`finding_check_id`):

- ATTRIBUTED (``check_id`` recorded — findings minted by
  :func:`agent_factory.ingestion_api.regress_for_check`): answered only by THAT check passing with
  its symptom re-evaluated away (:func:`resolve_findings_for_check`);
- UNATTRIBUTED (no ``check_id`` — every finding ``scripts/af-ticket-loop.sh`` writes itself, from
  conflict resolution and post-merge verification): answered only by the verification ROUND
  (:func:`resolve_unattributed_findings`, :func:`resolve_findings_for_round`). No single check may
  answer one — that finding exists precisely because it outranks "all your checks are green".

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

import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agent_factory._hooks import _ticket_state as _ts

from agent_factory import failure_taxonomy, ingestion_api

# R17's namesake failure class: recorded via :func:`agent_factory.failure_taxonomy.assign_class`
# so a recurring check-defeat dedups/recounts through the SAME taxonomy every other failure class
# does (R3), rather than living in a parallel, unaggregated bucket of its own.
CHECK_DEFEAT_CLASS_KIND = "check-defeat"

#: The value :func:`finding_check_id` returns for a finding that names no check at all. Both
#: findings the LOOP writes are of this shape (``scripts/af-ticket-loop.sh``'s conflict-resolution
#: and post-merge-verification regress passes emit ``{"round", "source", "reason", "evidence",
#: "required_fix"}`` with NO ``check_id``); only findings minted by
#: :func:`agent_factory.ingestion_api.regress_for_check` carry one.
UNATTRIBUTED = ""

#: Where a finding may record a MACHINE-EVALUABLE reproduction of its symptom (D2). Exit 0 means
#: the symptom REPRODUCED — i.e. it is still present. Absent on every finding either writer emits
#: today, which is why :func:`evaluate_symptom` has an explicit "undecidable" answer instead of a
#: silent default.
SYMPTOM_PROBE_KEYS = ("symptom_probe", "symptom_command", "repro_command")


def finding_check_id(finding: dict[str, Any]) -> str:
    """The check a finding is attributed to, or :data:`UNATTRIBUTED` (``""``) when it names none.

    Looks in the finding itself and in a nested ``meta`` — ``regress_for_check`` writes the flat
    key, but a finding round-tripped through a fact's meta can arrive nested."""
    for source in (finding, finding.get("meta") or {}):
        if not isinstance(source, dict):
            continue
        for key in ("check_id", "checkId"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return UNATTRIBUTED


def _is_open(finding: dict[str, Any]) -> bool:
    """The same open-finding predicate ``hooks._ticket_state.open_findings`` applies."""
    return not finding.get("resolved") and bool(str(finding.get("reason") or "").strip())


def _stamp(details: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool],
           resolved_by: str | None) -> list[dict[str, Any]]:
    for d in details:
        if _is_open(d) and predicate(d):
            d["resolved"] = True
            if resolved_by:
                d["resolved_by"] = resolved_by
    return details


def resolve_findings_for_check(meta: dict[str, Any], check_id: str, *,
                               resolved_by: str | None = None) -> list[dict[str, Any]]:
    """R17 — resolve ONLY the open findings ATTRIBUTED to ``check_id``. Every other open finding is
    left untouched: a sibling check's finding (a rerun passing check A must never stamp finding B
    resolved), and — deliberately — an UNATTRIBUTED finding.

    Why unattributed findings are NOT resolved here: a finding with no ``check_id`` was written by
    the verification ROUND (the loop's conflict-resolution and post-merge-verification passes), not
    by any one check, and it exists precisely because the completion gate reads only pinned checks —
    it is the judgement that survives "all your checks are green" (see
    ``hooks._ticket_state.open_findings``). Letting one passing check answer it would re-open the
    exact hole the finding was invented to plug. The round that authored those findings answers them
    instead, via :func:`resolve_findings_for_round`.

    Returns the full accumulated ``regression_detail`` list, ready to write back verbatim (same
    contract as :func:`hooks._ticket_state.resolve_finding`)."""
    scope = str(check_id or "").strip()
    if not scope:
        raise ValueError("check_id is required; use resolve_findings_for_round() to answer the "
                         "unattributed findings a verification round authored")
    return _stamp(_ts.regression_details(meta), lambda d: finding_check_id(d) == scope, resolved_by)


def resolve_unattributed_findings(meta: dict[str, Any], *,
                                  resolved_by: str | None = None) -> list[dict[str, Any]]:
    """Resolve ONLY the open findings that name no check — the ones the loop's own regress passes
    wrote. Their author is the verification round, so the round is what answers them; no check's
    exit code ever can. Every attributed finding is left untouched."""
    return _stamp(_ts.regression_details(meta),
                  lambda d: finding_check_id(d) == UNATTRIBUTED, resolved_by)


def resolve_findings_for_round(meta: dict[str, Any], *, resolved_by: str | None = None,
                               passed_check_ids: Iterable[str] | None = None
                               ) -> list[dict[str, Any]]:
    """The ROUND-scoped resolver — the drop-in replacement for
    ``hooks._ticket_state.resolve_finding(meta, resolved_by=...)`` that the loop's post-merge
    verification pass calls once it has confirmed a ticket survived the merged tree.

    It resolves strictly less than ``resolve_finding`` does:

    - every UNATTRIBUTED open finding (``check_id`` absent — every finding the loop itself writes)
      resolves: the verification round authored them and is the only thing that can answer them;
    - an ATTRIBUTED open finding resolves ONLY if its check id is in ``passed_check_ids`` — the
      checks this round actually re-ran and saw pass. A check that was not re-run this round has
      proved nothing, so its finding stays open (R17).

    Returns the full accumulated list, ready to write back verbatim."""
    passed = {str(c).strip() for c in (passed_check_ids or []) if str(c).strip()}

    def wanted(d: dict[str, Any]) -> bool:
        cid = finding_check_id(d)
        return cid == UNATTRIBUTED or cid in passed

    return _stamp(_ts.regression_details(meta), wanted, resolved_by)


# --------------------------------------------------------------------------- D2: symptom evaluation

def symptom_probe(finding: dict[str, Any]) -> str | None:
    """The finding's recorded machine-evaluable symptom reproduction, or ``None`` when it recorded
    only prose (``reason``/``evidence``) — which is the case for every finding either production
    writer emits today."""
    for source in (finding, finding.get("meta") or {}):
        if not isinstance(source, dict):
            continue
        for key in SYMPTOM_PROBE_KEYS:
            body = str(source.get(key) or "").strip()
            if body:
                return body
    return None


def _default_probe_runner(run: str, cwd: Path) -> bool:
    """Execute a validated symptom probe as an ARGV VECTOR — never ``shell=True``.

    ``ingestion_api._validate_run_body`` validates the body by PARSING it (``shlex``) and checking
    the resulting argv: verb allowlist, per-verb shape, path containment, metacharacter rejection.
    Those are argv-level guarantees. Handing the raw string to a shell would re-interpret it under
    different rules — a glob or a ``~`` survives validation as one literal token and then expands
    at execution, reaching files the containment check just refused. So the executor re-derives the
    argv with the SAME parser the validator used (``ingestion_api.parse_run_body`` — the one parser)
    and runs that vector with ``shell=False``, exactly as ``ingestion_api._default_runner`` /
    ``_default_worktree_runner`` do. Validation and execution therefore agree by construction."""
    return subprocess.run(ingestion_api.parse_run_body(run), shell=False, cwd=str(cwd),
                          check=False).returncode == 0


def evaluate_symptom(finding: dict[str, Any], repo_path: str | Path, *,
                     executor: Callable[[str, Path], bool] | None = None
                     ) -> tuple[bool | None, str]:
    """Re-evaluate ONE finding's recorded symptom against the rebuilt state at ``repo_path``.

    Returns ``(present, how)``: ``present`` is ``True`` (the symptom REPRODUCED — a check-defeat
    candidate), ``False`` (it is gone), or ``None`` when the finding gives this module nothing
    executable to decide with. ``how`` always names the basis, so an operator reading a result never
    has to guess whether a verdict was measured or assumed.

    The probe is run under the same machine-channel guardrails a drafted check body gets
    (``ingestion_api._validate_run_body``): a probe outside the command allowlist is UNDECIDABLE,
    never executed and never silently treated as "symptom gone". What survives validation is then
    executed as the ARGV VECTOR it was validated as, with no shell in between to re-interpret it
    (:func:`_default_probe_runner`)."""
    probe = symptom_probe(finding)
    if not probe:
        return None, "no-symptom-probe-recorded"
    try:
        body = ingestion_api._validate_run_body(probe, channel="machine")
    except ingestion_api.RunBodyRejected as exc:
        return None, f"symptom-probe-rejected: {exc}"
    do_run = executor or _default_probe_runner
    try:
        reproduced = bool(do_run(body, Path(repo_path)))
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"symptom-probe-errored: {exc}"
    return reproduced, ("symptom-probe-reproduced" if reproduced else "symptom-probe-clean")


def _evaluate_symptoms(findings: list[dict[str, Any]], repo_path: str | Path,
                       executor: Callable[[str, Path], bool] | None
                       ) -> tuple[bool | None, str]:
    """Fold :func:`evaluate_symptom` over every open finding attributed to the check under
    examination. Any finding that still reproduces makes the symptom PRESENT (one surviving symptom
    is a defeat). Otherwise the answer is only ``False`` if EVERY finding was actually measured —
    one undecidable finding makes the whole verdict undecidable rather than a quiet "gone"."""
    if not findings:
        # Nothing attributed to this check is open, so there is no recorded symptom to contradict
        # the pass. Resolving is a no-op here; saying so beats inventing a defeat.
        return False, "no-open-finding-for-check"
    undecided: list[str] = []
    for finding in findings:
        present, how = evaluate_symptom(finding, repo_path, executor=executor)
        if present:
            return True, how
        if present is None:
            undecided.append(how)
    if undecided:
        return None, "; ".join(undecided)
    return False, "symptom-probe-clean"


def resolve_or_defeat(
    meta: dict[str, Any],
    check_id: str,
    *,
    check_passed: bool,
    symptom_present: bool | None = None,
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

    ``symptom_present`` is the RE-EVALUATION of the finding's recorded symptom against the rebuilt
    state — resolution is never inferred from the check's exit code alone (R17's core requirement).
    Leave it ``None`` (the default) and THIS MODULE evaluates it, by running the finding's recorded
    symptom probe (:data:`SYMPTOM_PROBE_KEYS`) against ``repo_path`` under the machine-channel run
    allowlist (:func:`evaluate_symptom`). Pass an explicit ``True``/``False`` only when the caller
    genuinely measured the symptom itself; passing ``False`` is an ASSERTION that the symptom is
    gone, and it resolves findings.

    THE CONTRACT WHEN NEITHER IS AVAILABLE (D2): if ``symptom_present`` is ``None`` and no finding
    carries an evaluable probe, the symptom is UNDECIDABLE. This returns
    ``status="symptom-unevaluated"`` — resolving nothing, defeating nothing — and writes the reason
    to stderr. It deliberately does NOT fall through to "no defeat": defaulting to that would stamp
    findings resolved on a check's exit code alone, the precise failure R17 exists to prevent.

    Four outcomes:

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
    - check PASSES and the symptom cannot be evaluated -> ``status="symptom-unevaluated"``; nothing
      resolves, nothing defeats, and ``symptom_basis`` says why.
    """
    scope = str(check_id or "").strip()
    if not check_passed:
        return {"status": "unresolved", "reason": "check-still-failing", "check_id": scope,
                "symptom_basis": "not-evaluated-check-failed",
                "regression_detail": _ts.regression_details(meta)}

    matching = [d for d in _ts.open_findings(meta) if finding_check_id(d) == scope]

    if symptom_present is None:
        symptom_present, basis = _evaluate_symptoms(matching, repo_path, executor)
    else:
        basis = f"caller-asserted:{bool(symptom_present)}"

    if symptom_present is None:
        reason = (f"symptom for check {scope or '<none>'} could not be re-evaluated against the "
                  f"rebuilt state ({basis}); refusing to resolve on the check's exit code alone")
        print(f"resolution: {reason}", file=sys.stderr)
        return {"status": "symptom-unevaluated", "reason": reason, "check_id": scope,
                "symptom_basis": basis, "regression_detail": _ts.regression_details(meta)}

    if not symptom_present:
        updated = (resolve_findings_for_check(meta, scope, resolved_by=resolved_by) if scope
                   else resolve_unattributed_findings(meta, resolved_by=resolved_by))
        return {"status": "resolved", "check_id": scope, "symptom_basis": basis,
                "regression_detail": updated}

    if not scope:
        # A persisting symptom on a finding no check owns cannot be a CHECK-defeat: there is no
        # check to pin the defeat on, demote, or redraft. The finding simply stays open.
        reason = ("the recorded symptom persists on a finding that names no check — nothing to "
                  "demote, so the finding stays open")
        return {"status": "unresolved", "reason": reason, "check_id": scope,
                "symptom_basis": basis, "regression_detail": _ts.regression_details(meta)}

    # Check-defeat: the check passed, but the finding's own recorded symptom is still there.
    finding = matching[0] if matching else {}
    reason = (f"check-defeat: check {scope} passed on the rebuilt state but the finding's "
             f"recorded symptom persisted: {finding.get('reason', '')}")
    evidence = str(finding.get("evidence") or "")

    pin_ack = ingestion_api.pin_artifact(
        project=project, ticket_id=ticket_id, commit_sha=commit_sha, repo_path=repo_path,
        evidence_text=evidence, while_gating=True, source="check-defeat",
    )
    artifact_meta = ingestion_api.read_artifact(pin_ack["id"]).get("meta") or {}

    classification = failure_taxonomy.assign_class(
        reason, evidence=evidence, source="check-defeat",
        meta={"kind": CHECK_DEFEAT_CLASS_KIND, "check_id": scope, "ticket_id": ticket_id},
    )

    demotion = ingestion_api.demote_for_check_defeat(scope, project, reason=reason, identity=identity)

    redraft = None
    if run_candidates:
        redraft = ingestion_api.attempt_fail_then_pass_proof(
            run_candidates, bad_artifact_meta=artifact_meta,
            healthy_repo_path=healthy_repo_path or repo_path, repeat_count=repeat_count,
            redraft_budget=redraft_budget, executor=executor,
        )

    return {"status": "check-defeat", "check_id": scope, "artifact": pin_ack,
            "classification": classification, "demotion": demotion, "redraft": redraft,
            "symptom_basis": basis, "regression_detail": _ts.regression_details(meta)}
