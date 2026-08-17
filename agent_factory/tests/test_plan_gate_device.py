"""Unit tests for the ``R-DEVICE-CLOSED-SET`` rule in the plan done-gate (R16).

af-intake-plan stamps ``meta.device`` on every ticket to name the concurrency lane it counts
against af-build's admission caps (``max_cpu_parallel`` / ``max_gpu_parallel``, see R15). An
absent value defaults to ``"cpu"`` so an already-blessed plan authored before this rule existed
keeps passing; a value outside the closed set (``{"cpu", "gpu"}``) rejects, naming the ticket.

Imports the gate directly (``pythonpath = ["src", "."]`` from pyproject makes
``agent_factory.plan_gate`` importable, the same pattern ``test_plan_gate_decisions.py`` uses).
"""

from agent_factory.plan_gate import (
    DEVICE_CLOSED_SET,
    R_DEVICE_CLOSED_SET,
    Requirement,
    evaluate_plan,
)

# A valid signed contract. R-CONTRACT-SIGNED is plan-level and fails closed on absent evidence, so
# every case here supplies it to keep the device rule the only one under test.
_SIGNED = {"signed": True, "actions_recorded": True}


def _req(device: str = "", req_id: str = "R1") -> Requirement:
    return Requirement(
        id=req_id,
        text="one atomic behavior",
        acceptance="a binary, observable condition",
        source="prd-af-ml-research",
        defines=["x"],
        tags=["scheduler"],
        verify="automated",
        device=device,
    )


def test_absent_device_defaults_to_cpu_and_admits() -> None:
    verdict = evaluate_plan([_req(device="")], project="af-ml-research", contract=_SIGNED)

    assert verdict.admitted
    assert verdict.rule_ids == []


def test_device_cpu_admits() -> None:
    verdict = evaluate_plan([_req(device="cpu")], project="af-ml-research", contract=_SIGNED)

    assert verdict.admitted
    assert verdict.rule_ids == []


def test_device_gpu_admits() -> None:
    verdict = evaluate_plan([_req(device="gpu")], project="af-ml-research", contract=_SIGNED)

    assert verdict.admitted
    assert verdict.rule_ids == []


def test_device_outside_closed_set_rejects_naming_the_ticket() -> None:
    verdict = evaluate_plan([_req(device="tpu", req_id="R7")], project="af-ml-research",
                            contract=_SIGNED)

    assert not verdict.admitted
    assert verdict.rule_ids == [R_DEVICE_CLOSED_SET]
    assert any("R7" in r.message for r in verdict.reasons)


def test_device_is_case_insensitive() -> None:
    verdict = evaluate_plan([_req(device="GPU")], project="af-ml-research", contract=_SIGNED)

    assert verdict.admitted
    assert verdict.rule_ids == []


def test_closed_set_is_exactly_cpu_and_gpu() -> None:
    assert DEVICE_CLOSED_SET == frozenset({"cpu", "gpu"})
