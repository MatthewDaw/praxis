"""Offline U6 tests: three-arm orchestration → plain cost records.

Runs fully offline — ``run_arm``, ``grade``, and ``ingest`` are all stubbed, so no
real ``claude``, ``git``, Docker, backend, or venv is touched. The tests assert the
canonical record shapes (per-arm + per-instance meta) U7/U8 depend on, that
``run_arm`` owns the rework (U6 faithfully records its cumulative ``cost_usd``), that
ingestion is attached once per instance, that retrieval overhead lives on treatment
only, and that ``record_key`` is collision-free.

    uv run pytest knowledge/evals/swebench/tests/test_experiment.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.evals.swebench.experiment import (
    _arm_record,
    record_key,
    run_experiment,
    run_one,
)
from knowledge.evals.swebench.grader import GradeResult
from knowledge.evals.swebench.instances import Instance, load_candidates
from knowledge.evals.swebench.runner import ArmResult

FIX = Path(__file__).parent / "fixtures"


def _instances() -> list[Instance]:
    records = json.loads((FIX / "rebench_sample.json").read_text(encoding="utf-8"))
    return load_candidates(records)


def _instance() -> Instance:
    return {i.instance_id: i for i in _instances()}["sympy__sympy-fake-0001"]


def _resolved_grade(instance, patch) -> GradeResult:
    return GradeResult(resolved=True, fail_to_pass={}, pass_to_pass={}, empty_patch=False)


def _arm_result(arm: str, *, resolved=True, cost=1.0, rework_rounds=0) -> ArmResult:
    return ArmResult(
        arm=arm,
        patch="diff --git a/x b/x\n+1\n",
        resolved=resolved,
        cost_usd=cost,
        tokens=1000,
        turns=5,
        rework_rounds=rework_rounds,
        retrieval_overhead={"query_embed_ms": 0, "mcp_round_trip_ms": 0} if arm == "treatment" else None,
    )


def _stub_run_arm(per_arm: dict[str, ArmResult]):
    """A run_arm stub returning a canned ArmResult per arm; ignores grade/kwargs."""
    def run_arm(instance, arm, *, grade, k_rework=1, **kwargs):
        # exercise the grade callback path (run_arm normally calls it) without asserting
        grade(per_arm[arm].patch)
        return per_arm[arm]
    return run_arm


# ---------------------------------------------------------------------------
# Record shape: one (instance, trial) → exactly a treatment + a control record.
# ---------------------------------------------------------------------------
def test_run_one_yields_treatment_and_control_records():
    inst = _instance()
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment", resolved=True, cost=1.5),
        "control": _arm_result("control", resolved=False, cost=2.0),
    })

    records = run_one(inst, 0, grade=_resolved_grade, run_arm=run_arm)

    assert len(records) == 2
    arms = {r["arm"] for r in records}
    assert arms == {"treatment", "control"}
    # every canonical field present on each record
    expected_keys = {
        "instance_id", "trial", "arm", "resolved", "agent_cost",
        "tokens", "turns", "rework_rounds", "retrieval_overhead",
    }
    for r in records:
        assert set(r) == expected_keys
        assert r["instance_id"] == "sympy__sympy-fake-0001"
        assert r["trial"] == 0


def test_arm_record_maps_armresult_fields():
    inst = _instance()
    res = _arm_result("treatment", resolved=True, cost=3.25, rework_rounds=1)
    rec = _arm_record(inst, 2, res)
    assert rec == {
        "instance_id": "sympy__sympy-fake-0001",
        "trial": 2,
        "arm": "treatment",
        "resolved": True,
        "agent_cost": 3.25,  # == ArmResult.cost_usd
        "tokens": 1000,
        "turns": 5,
        "rework_rounds": 1,
        "retrieval_overhead": {"query_embed_ms": 0, "mcp_round_trip_ms": 0},
    }


# ---------------------------------------------------------------------------
# Cost-to-correct propagation: run_arm owns rework; U6 records its cost_usd.
# ---------------------------------------------------------------------------
def test_failing_control_records_cumulative_cost_to_correct():
    """A failing control whose run_arm returns first_pass+rework cost is recorded verbatim."""
    inst = _instance()
    first_pass, rework = 2.0, 1.3
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment", resolved=True, cost=1.0),
        # run_arm reworked once and reports the cumulative cost-to-correct.
        "control": _arm_result("control", resolved=False, cost=first_pass + rework, rework_rounds=1),
    })

    records = run_one(inst, 0, grade=_resolved_grade, run_arm=run_arm, k_rework=1)
    control = next(r for r in records if r["arm"] == "control")
    assert control["agent_cost"] == first_pass + rework
    assert control["rework_rounds"] == 1


def test_passing_control_records_first_pass_cost_only():
    inst = _instance()
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment", resolved=True, cost=1.0),
        "control": _arm_result("control", resolved=True, cost=2.0, rework_rounds=0),
    })
    records = run_one(inst, 0, grade=_resolved_grade, run_arm=run_arm)
    control = next(r for r in records if r["arm"] == "control")
    assert control["agent_cost"] == 2.0
    assert control["rework_rounds"] == 0


# ---------------------------------------------------------------------------
# Retrieval overhead only on treatment.
# ---------------------------------------------------------------------------
def test_retrieval_overhead_only_on_treatment():
    inst = _instance()
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment"),
        "control": _arm_result("control"),
    })
    records = run_one(inst, 0, grade=_resolved_grade, run_arm=run_arm)
    treat = next(r for r in records if r["arm"] == "treatment")
    control = next(r for r in records if r["arm"] == "control")
    assert isinstance(treat["retrieval_overhead"], dict)
    assert control["retrieval_overhead"] is None


def test_arm_record_nulls_control_overhead_even_if_present():
    """Defensive: a control ArmResult carrying overhead is still nulled in the record."""
    inst = _instance()
    res = ArmResult(
        arm="control", patch="", resolved=True, cost_usd=1.0, tokens=1, turns=1,
        rework_rounds=0, retrieval_overhead={"leaked": 1},
    )
    rec = _arm_record(inst, 0, res)
    assert rec["retrieval_overhead"] is None


# ---------------------------------------------------------------------------
# Ingestion meta: once per instance, not per trial.
# ---------------------------------------------------------------------------
class _FakeIngest:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, instance):
        self.calls.append(instance.instance_id)

        class _Res:
            ingestion_cost = None
            facts_ingested = 14
        return _Res()


def test_ingestion_attached_once_per_instance_regardless_of_trials():
    instances = _instances()[:2]
    ingest = _FakeIngest()
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment"),
        "control": _arm_result("control"),
    })

    out = run_experiment(
        instances, trials=3, grade=_resolved_grade, ingest=ingest, run_arm=run_arm,
    )

    # ingest called exactly once per instance (not per trial)
    assert ingest.calls == [i.instance_id for i in instances]
    # one meta entry per instance, regardless of trials
    assert len(out["instances"]) == 2
    meta_ids = [m["instance_id"] for m in out["instances"]]
    assert meta_ids == [i.instance_id for i in instances]
    for m in out["instances"]:
        assert set(m) == {"instance_id", "ingestion_cost", "facts_ingested"}
        assert m["ingestion_cost"] is None
        assert m["facts_ingested"] == 14
    # 2 instances × 3 trials × 2 arms
    assert len(out["records"]) == 2 * 3 * 2


def test_no_ingest_yields_null_meta_per_instance():
    instances = _instances()[:1]
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment"),
        "control": _arm_result("control"),
    })
    out = run_experiment(instances, trials=2, grade=_resolved_grade, run_arm=run_arm)
    assert len(out["instances"]) == 1
    assert out["instances"][0] == {
        "instance_id": instances[0].instance_id,
        "ingestion_cost": None,
        "facts_ingested": 0,
    }


# ---------------------------------------------------------------------------
# Arm/trial keying: no collision across instances/trials/arms.
# ---------------------------------------------------------------------------
def test_record_keys_are_unique_across_instances_trials_arms():
    instances = _instances()[:2]
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment"),
        "control": _arm_result("control"),
    })
    out = run_experiment(instances, trials=2, grade=_resolved_grade, run_arm=run_arm)

    keys = [record_key(r) for r in out["records"]]
    assert len(keys) == len(set(keys)), "record_key collision — a record would overwrite another"
    # the key is exactly (instance_id, trial, arm)
    for r in out["records"]:
        assert record_key(r) == (r["instance_id"], r["trial"], r["arm"])


def test_workers_threadpool_produces_same_records():
    instances = _instances()[:2]
    run_arm = _stub_run_arm({
        "treatment": _arm_result("treatment"),
        "control": _arm_result("control"),
    })
    out = run_experiment(instances, trials=2, grade=_resolved_grade, run_arm=run_arm, workers=4)
    keys = {record_key(r) for r in out["records"]}
    assert len(keys) == 2 * 2 * 2
