"""Deterministic checks for Monica's dashboard and human-gate eval suite."""

from __future__ import annotations

import json
import re
from typing import Any

from knowledge.evals.eval_def import CheckResult, EvalContext

_PROVENANCE_RE = re.compile(r"^logs/.+\.jsonl:\d+$")


def _json_objects(output: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for block in output.split("\n\n"):
        text = block.strip()
        if not text:
            continue
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _candidate(objects: list[dict[str, Any]], candidate_id: str) -> dict[str, Any] | None:
    return next((obj for obj in objects if obj.get("id") == candidate_id), None)


def candidate_contract(ctx: EvalContext, *, candidate_id: str) -> CheckResult:
    """Pass iff a candidate row has the dashboard fields Monica needs for Act 2."""
    objects = _json_objects(ctx.output)
    row = _candidate(objects, candidate_id)
    if row is None:
        return CheckResult(
            name="candidate_contract",
            passed=False,
            evidence=f"candidate {candidate_id!r} not found",
        )

    required = [
        "id",
        "evalCaseId",
        "title",
        "content",
        "state",
        "confidence",
        "provenance",
        "confidenceBreakdown",
        "auditTrail",
    ]
    missing = [field for field in required if field not in row]
    if missing:
        return CheckResult(
            name="candidate_contract",
            passed=False,
            evidence=f"missing fields {missing!r}",
        )

    state_ok = row["state"] in {"proposed", "suggested", "active", "rejected", "decayed"}
    confidence_ok = isinstance(row["confidence"], (int, float)) and 0 <= row["confidence"] <= 1
    provenance_ok = isinstance(row["provenance"], str) and bool(_PROVENANCE_RE.match(row["provenance"]))
    breakdown = row["confidenceBreakdown"]
    breakdown_ok = isinstance(breakdown, dict) and all(
        isinstance(breakdown.get(key), (int, float)) for key in ("frequency", "recency", "breadth")
    )
    audit = row["auditTrail"]
    audit_ok = isinstance(audit, list) and bool(audit) and all(
        isinstance(entry, dict) and entry.get("actor") and entry.get("provenance") for entry in audit
    )
    ok = state_ok and confidence_ok and provenance_ok and breakdown_ok and audit_ok
    return CheckResult(
        name="candidate_contract",
        passed=ok,
        evidence=(
            "candidate contract complete"
            if ok
            else (
                f"state_ok={state_ok}, confidence_ok={confidence_ok}, "
                f"provenance_ok={provenance_ok}, breakdown_ok={breakdown_ok}, audit_ok={audit_ok}"
            )
        ),
    )


def contradiction_pair_contract(
    ctx: EvalContext, *, primary_id: str, rival_id: str
) -> CheckResult:
    """Pass iff two candidate rows identify each other as a contradiction pair."""
    objects = _json_objects(ctx.output)
    primary = _candidate(objects, primary_id)
    rival = _candidate(objects, rival_id)
    if primary is None or rival is None:
        return CheckResult(
            name="contradiction_pair_contract",
            passed=False,
            evidence=f"primary={primary is not None}, rival={rival is not None}",
        )

    primary_ids = set(primary.get("contradictionIds") or primary.get("contradiction_ids") or [])
    rival_ids = set(rival.get("contradictionIds") or rival.get("contradiction_ids") or [])
    ok = rival_id in primary_ids and primary_id in rival_ids
    return CheckResult(
        name="contradiction_pair_contract",
        passed=ok,
        evidence=(
            f"{primary_id} and {rival_id} are linked"
            if ok
            else f"primary links {sorted(primary_ids)}, rival links {sorted(rival_ids)}"
        ),
    )


def mutation_audit_contract(
    ctx: EvalContext,
    *,
    candidate_id: str,
    expected_state: str,
    required_actions: list[str],
) -> CheckResult:
    """Pass iff a human action produces the expected lifecycle and audit evidence."""
    objects = _json_objects(ctx.output)
    row = _candidate(objects, candidate_id)
    if row is None:
        return CheckResult(
            name="mutation_audit_contract",
            passed=False,
            evidence=f"candidate {candidate_id!r} not found",
        )

    audit = row.get("auditTrail")
    if not isinstance(audit, list):
        return CheckResult(
            name="mutation_audit_contract",
            passed=False,
            evidence="auditTrail missing or not a list",
        )

    actions = {str(entry.get("action")) for entry in audit if isinstance(entry, dict)}
    human_actions = [
        entry
        for entry in audit
        if isinstance(entry, dict)
        and str(entry.get("actor", "")).lower() in {"human", "reviewer", "monica"}
        and entry.get("provenance")
    ]
    transitions = [
        entry
        for entry in audit
        if isinstance(entry, dict)
        and entry.get("fromState")
        and entry.get("toState")
    ]
    state_ok = row.get("state") == expected_state
    actions_ok = all(action in actions for action in required_actions)
    human_ok = bool(human_actions)
    transition_ok = bool(transitions)
    ok = state_ok and actions_ok and human_ok and transition_ok
    return CheckResult(
        name="mutation_audit_contract",
        passed=ok,
        evidence=(
            "human mutation audit is visible"
            if ok
            else (
                f"state_ok={state_ok}, actions_ok={actions_ok}, "
                f"human_ok={human_ok}, transition_ok={transition_ok}"
            )
        ),
    )


def low_confidence_confirmation(ctx: EvalContext, *, candidate_id: str, threshold: float) -> CheckResult:
    """Pass iff a low-confidence candidate declares an explicit confirmation gate."""
    objects = _json_objects(ctx.output)
    row = _candidate(objects, candidate_id)
    if row is None:
        return CheckResult(
            name="low_confidence_confirmation",
            passed=False,
            evidence=f"candidate {candidate_id!r} not found",
        )

    review = row.get("reviewPolicy") if isinstance(row.get("reviewPolicy"), dict) else {}
    confidence = row.get("confidence")
    low = isinstance(confidence, (int, float)) and confidence < threshold
    required = review.get("requiresConfirmation") is True
    warning = isinstance(review.get("warning"), str) and bool(review.get("warning", "").strip())
    ok = low and required and warning
    return CheckResult(
        name="low_confidence_confirmation",
        passed=ok,
        evidence=(
            "low-confidence candidate requires confirmation"
            if ok
            else f"low={low}, requiresConfirmation={required}, warning={warning}"
        ),
    )


def data_source_status_contract(ctx: EvalContext) -> CheckResult:
    """Pass iff dashboard mode metadata is enough for mock/live demo narration."""
    objects = _json_objects(ctx.output)
    status = next((obj for obj in objects if obj.get("kind") == "dashboard_data_source_status"), None)
    if status is None:
        return CheckResult(
            name="data_source_status_contract",
            passed=False,
            evidence="dashboard_data_source_status object not found",
        )

    provider = status.get("provider")
    contract = status.get("contractVersion")
    api_base_url = status.get("apiBaseUrl")
    eval_metrics_url = status.get("evalMetricsUrl")
    fallback = status.get("fallback")
    headers = status.get("headers")
    ok = (
        provider in {"mock", "live-api"}
        and contract == "1"
        and (api_base_url is None or isinstance(api_base_url, str))
        and (eval_metrics_url is None or isinstance(eval_metrics_url, str))
        and isinstance(fallback, dict)
        and fallback.get("mockWhenApiBaseMissing") is True
        and isinstance(headers, dict)
        and headers.get("X-Praxis-Contract") == "1"
    )
    return CheckResult(
        name="data_source_status_contract",
        passed=ok,
        evidence=(
            "dashboard data-source status supports mock/live narration"
            if ok
            else (
                f"provider={provider!r}, contract={contract!r}, "
                f"fallback={fallback!r}, headers={headers!r}"
            )
        ),
    )


def metrics_show_compounding_gain(ctx: EvalContext, *, min_reduction: float) -> CheckResult:
    """Pass iff demo metrics show the required correction reduction without success regression."""
    objects = _json_objects(ctx.output)
    metrics = next((obj for obj in objects if obj.get("kind") == "eval_metrics"), None)
    if metrics is None:
        return CheckResult(
            name="metrics_show_compounding_gain",
            passed=False,
            evidence="eval_metrics object not found",
        )

    cold = metrics.get("coldCorrections")
    injected = metrics.get("injectedCorrections")
    cold_success = metrics.get("coldSuccessRate")
    injected_success = metrics.get("injectedSuccessRate")
    if not all(isinstance(v, (int, float)) for v in (cold, injected, cold_success, injected_success)):
        return CheckResult(
            name="metrics_show_compounding_gain",
            passed=False,
            evidence="metrics fields must be numeric",
        )
    reduction = (cold - injected) / cold if cold else 0
    ok = reduction >= min_reduction and injected_success >= cold_success
    return CheckResult(
        name="metrics_show_compounding_gain",
        passed=ok,
        evidence=f"correction reduction={reduction:.2%}, success {cold_success}->{injected_success}",
    )
