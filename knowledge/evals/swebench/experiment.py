"""U6: three-arm trial orchestration + plain cost records for the PR-knowledge pilot.

WHY this module is thin: it does no reduction, no stats, no Docker, and no agent
work of its own. It *composes* the units already built —

* the U5 runner (:func:`knowledge.evals.swebench.runner.run_arm`) which ALREADY owns
  the repro-test rework loop: it grades after every round, reworks up to ``k_rework``
  times, and returns ``ArmResult.cost_usd`` as cumulative in-arm spend (= cost-to-correct
  when the arm resolves, total spend across attempts when it does not — read with
  ``resolved``). U6 does **not** re-implement rework — it builds the grade callback and faithfully
  records whatever ``run_arm`` reports.
* the U2 grader (:func:`knowledge.evals.swebench.grader.grade`) as the correctness
  oracle: the callback ``run_arm`` wants is ``lambda patch: grade(instance, patch).resolved``.
* (optionally) the U3 ingest (:func:`knowledge.evals.swebench.ingest.run_ingest`),
  injected as a seam so the unit layer stays fully offline.

Mirrors ``cases/dom/pr_knowledge_dogfood/analyze.py``'s ``run_experiment``/``_one``
shape (in-process orchestration, never the ``knowledge.evals.run`` CLI — that CLI
calls ``write_baseline`` and would clobber ``results/baseline.jsonl``). All reduction
is deferred to U7; U6 only produces records.

Two record shapes (consumed by U7/U8) — both plain dicts:

* per-arm record: one per (instance, trial, arm). ``agent_cost`` is
  ``ArmResult.cost_usd``; ``retrieval_overhead`` is non-null only on treatment.
* per-instance meta: one per instance (NOT per trial), carrying the amortized
  ``ingestion_cost`` line + ``facts_ingested``.

Records are keyed on ``(instance_id, trial, arm)`` — :func:`record_key` — guarding
against the autodistill arm/trial-collision bug where two records overwrite each other.
"""

from __future__ import annotations

from typing import Callable

from knowledge.evals.swebench.grader import GradeResult
from knowledge.evals.swebench.instances import Instance
from knowledge.evals.swebench.runner import ArmResult, run_arm as _run_arm

# instance, patch -> GradeResult. U2's real grader; tests stub it.
GradeFn = Callable[[Instance, str], GradeResult]

ARMS = ("treatment", "control")


def record_key(record: dict) -> tuple[str, int, str]:
    """The uniqueness key for a per-arm record: ``(instance_id, trial, arm)``.

    Keying on all three is the guard against the autodistill collision bug — two
    records for the same instance must not overwrite each other across trials/arms.
    """
    return (record["instance_id"], record["trial"], record["arm"])


def _arm_record(instance: Instance, trial: int, arm_result: ArmResult) -> dict:
    """Pure: an :class:`ArmResult` → the canonical per-arm dict.

    ``retrieval_overhead`` is nulled for the control arm (it only makes sense for the
    treatment arm, which is the only one that talks to the Praxis MCP).
    """
    is_treatment = arm_result.arm == "treatment"
    return {
        "instance_id": instance.instance_id,
        "trial": trial,
        "arm": arm_result.arm,
        "resolved": arm_result.resolved,
        "agent_cost": arm_result.cost_usd,
        "tokens": arm_result.tokens,
        "turns": arm_result.turns,
        "rework_rounds": arm_result.rework_rounds,
        "retrieval_overhead": arm_result.retrieval_overhead if is_treatment else None,
    }


def run_one(
    instance: Instance,
    trial: int,
    *,
    grade: GradeFn,
    run_arm: Callable[..., ArmResult] = _run_arm,
    k_rework: int = 1,
    **arm_kwargs,
) -> list[dict]:
    """Run treatment + control for one (instance, trial); return both per-arm records.

    Each arm gets the grade callback ``lambda patch: grade(instance, patch).resolved``
    — ``run_arm`` calls it after each round and owns the rework loop internally, so
    U6 never re-grades or re-reworks here. Mirrors dogfood ``_one``'s per-trial
    progress print.
    """
    def grade_fn(patch: str) -> bool:
        return grade(instance, patch).resolved

    records: list[dict] = []
    for arm in ARMS:
        result = run_arm(instance, arm, grade=grade_fn, k_rework=k_rework, **arm_kwargs)
        # Catch an arm/result desync early: the record's arm (and its retrieval-overhead
        # nulling) keys off result.arm, so a runner that returned the wrong arm would
        # silently mislabel the record. They must agree.
        assert result.arm == arm, f"run_arm returned arm={result.arm!r} for requested {arm!r}"
        records.append(_arm_record(instance, trial, result))

    treat = next(r for r in records if r["arm"] == "treatment")
    control = next(r for r in records if r["arm"] == "control")
    print(
        f"  {instance.instance_id} trial {trial + 1}: "
        f"treat_ok={treat['resolved']} ctrl_ok={control['resolved']} "
        f"treat_cost={treat['agent_cost']} ctrl_cost={control['agent_cost']}",
        flush=True,
    )
    return records


def run_experiment(
    instances: list[Instance],
    *,
    trials: int,
    grade: GradeFn,
    ingest: Callable[[Instance], object] | None = None,
    run_arm: Callable[..., ArmResult] = _run_arm,
    k_rework: int = 1,
    workers: int = 1,
    **arm_kwargs,
) -> dict:
    """Per instance: ingest once (optional) → run ``trials`` × (treatment + control).

    Returns ``{"records": [<per-arm records>], "instances": [<per-instance meta>]}``.
    Ingestion is attached **once per instance** (its ``ingestion_cost`` is the amortized
    line, not a per-trial charge), so ``instances`` has exactly one entry per instance
    regardless of ``trials``. Mirrors dogfood's optional ``ThreadPoolExecutor`` ``workers``
    over the per-(instance, trial) jobs; ingest stays serial (one space per instance)
    so it runs before any of that instance's trials.
    """
    instance_meta: list[dict] = []
    jobs: list[tuple[Instance, int]] = []
    for instance in instances:
        if ingest is not None:
            res = ingest(instance)
            instance_meta.append({
                "instance_id": instance.instance_id,
                "ingestion_cost": getattr(res, "ingestion_cost", None),
                "facts_ingested": getattr(res, "facts_ingested", 0),
            })
        else:
            instance_meta.append({
                "instance_id": instance.instance_id,
                "ingestion_cost": None,
                "facts_ingested": 0,
            })
        jobs.extend((instance, trial) for trial in range(trials))

    def _one(job: tuple[Instance, int]) -> list[dict]:
        instance, trial = job
        return run_one(
            instance, trial,
            grade=grade, run_arm=run_arm, k_rework=k_rework, **arm_kwargs,
        )

    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_one, jobs))
    else:
        results = [_one(job) for job in jobs]

    records: list[dict] = [r for batch in results for r in batch]
    return {"records": records, "instances": instance_meta}
