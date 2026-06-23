"""Run eval cases from the dashboard, by scope, with optional config overrides.

Thin wrapper over the eval harness (``knowledge.evals.run``): list the available
case folders (scopes), and run every case under a chosen scope end-to-end
(seed -> agent -> grade) with the shared seed cache on, so cases that reuse the
same seed don't re-ingest. Per-run config overrides (substrate / embedder /
reader / ingest_model / runner model / reader_top_k / reader_min_score) replace
the case defaults when set; unset fields keep the case's own value. The agent
backend (``fake`` / ``openrouter`` / ``structured`` / ``claude``) is selectable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowledge.evals.eval_def import EvalCase
from knowledge.evals.run import (
    CASES_DIR,
    load_cases,
    partition_by_capability,
    run_case_full,
    select_runner,
    set_seed_cache,
    status_of,
)

VALID_BACKENDS = ("openrouter", "structured", "claude", "fake")

# The case fields a dashboard run may override, with their allowed enum values
# (None => free value: string for models, number for the reader knobs).
OVERRIDE_FIELDS: dict[str, tuple[str, ...] | None] = {
    "substrate": ("in_memory", "vector"),
    "embedder": ("fake", "cached", "live"),
    "reader": ("whole_file", "retrieving"),
    "ingest_model": None,
    "model": None,
    "reader_top_k": None,
    "reader_min_score": None,
}
_INT_FIELDS = {"reader_top_k"}
_FLOAT_FIELDS = {"reader_min_score"}


def _resolve_scope_dir(scope: str | None) -> Path:
    root = CASES_DIR.resolve()
    if not scope or scope in (".", "/"):
        return root
    target = (root / scope).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"scope {scope!r} must stay under knowledge/evals/cases")
    if not target.is_dir():
        raise ValueError(f"no such scope directory: {scope!r}")
    return target


def list_scopes() -> list[dict[str, Any]]:
    """Every case-containing folder (and its ancestors) as a selectable scope."""
    root = CASES_DIR.resolve()
    counts: dict[str, int] = {}
    for case_file in root.rglob("case.yaml"):
        rel_dir = case_file.parent.resolve()
        # Credit the case to its own folder and every ancestor up to the root.
        node = rel_dir
        while True:
            key = "." if node == root else str(node.relative_to(root)).replace("\\", "/")
            counts[key] = counts.get(key, 0) + 1
            if node == root:
                break
            node = node.parent
    return [
        {"scope": scope, "caseCount": counts[scope]}
        for scope in sorted(counts, key=lambda s: (s != ".", s))
    ]


def _clean_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only recognized override fields with a real (non-empty) value."""
    out: dict[str, Any] = {}
    for field, allowed in OVERRIDE_FIELDS.items():
        if raw is None or field not in raw:
            continue
        value = raw[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if field in _INT_FIELDS:
            value = int(value)
        elif field in _FLOAT_FIELDS:
            value = float(value)
        elif allowed is not None and value not in allowed:
            raise ValueError(f"{field} must be one of {allowed}, got {value!r}")
        out[field] = value
    return out


def _apply_overrides(case: EvalCase, overrides: dict[str, Any]) -> EvalCase:
    """Return a re-validated copy of ``case`` with ``overrides`` applied."""
    if not overrides:
        return case
    updated = case.model_copy(update=overrides)
    # Re-run the case validators so an invalid combo (e.g. retrieving reader on a
    # fake embedder) is rejected up front instead of failing mid-run.
    return EvalCase.model_validate(updated.model_dump())


def run_scopes(
    scopes: list[str] | None = None,
    backend: str = "openrouter",
    overrides: dict[str, Any] | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run every case under any of ``scopes`` (deduped by id) and grade each.

    ``force`` ignores the backend's capability gate, so a sandbox case (e.g. the
    application-filling cases) can be graded single-shot on a text backend like
    ``openrouter`` instead of being skipped — lower fidelity, but it runs.
    """
    if backend not in VALID_BACKENDS:
        raise ValueError(f"backend must be one of {VALID_BACKENDS}, got {backend!r}")
    selected = [s for s in (scopes or ["."]) if s] or ["."]

    clean = _clean_overrides(overrides)
    # Gather cases across all selected folders; a case picked via two scopes
    # (e.g. a folder and a case inside it) runs once.
    seen: set[str] = set()
    cases = []
    for scope in selected:
        for case in load_cases(_resolve_scope_dir(scope)):
            if case.id not in seen:
                seen.add(case.id)
                cases.append(case)
    cases.sort(key=lambda c: c.id)
    if limit is not None:
        cases = cases[: max(0, limit)]

    runner, judge = select_runner(backend)
    set_seed_cache(True)
    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            results.append(_run_one(case, runner, judge, clean, force))
    finally:
        set_seed_cache(False)

    return {
        "scopes": selected,
        "backend": backend,
        "overrides": clean,
        "force": force,
        "casesRun": len(results),
        "results": results,
    }


def _run_one(case, runner, judge, overrides, force) -> dict[str, Any]:
    try:
        case = _apply_overrides(case, overrides)
    except Exception as exc:  # invalid override combo for this case
        return {"caseId": case.id, "status": "ERROR", "error": str(exc)}

    if not force:
        runnable, skipped = partition_by_capability([case], runner)
        if not runnable:
            _, reasons = skipped[0]
            return {
                "caseId": case.id,
                "status": "SKIPPED",
                "skipReasons": sorted(str(r) for r in reasons),
            }

    try:
        ctx, judge_result, result = run_case_full(case, runner, judge=judge)
    except Exception as exc:
        return {"caseId": case.id, "status": "ERROR", "error": str(exc)}

    return {
        "caseId": case.id,
        "status": status_of(result),
        "passed": result.passed,
        "rubricScore": result.rubric_score,
        "checks": [
            {"name": c.name, "passed": c.passed, "evidence": c.evidence} for c in result.checks
        ],
        "output": ctx.output,
        "injectedKnowledge": ctx.injected_knowledge,
        "xfailReason": result.xfail_reason,
    }
