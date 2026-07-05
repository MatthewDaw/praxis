"""Tests for the event-log escape harvester (U6; R9-R11, AE4/AE5, SC4).

Each test builds a synthetic event log with the real :class:`EventLog`/``emit_gate_result``
so the gate_result/outcome correlation keys are exactly the frozen ones, then drives
:func:`harvest` into a throwaway quarantine dir (never the repo's real ``_quarantine/``).
"""

from pathlib import Path

from agent_factory.event_log import EventLog
from agent_factory.gate import Reason, Verdict, emit_gate_result
from evals.case_def import discover_cases, load_case
from evals.harvest import harvest

# A representative offending gate input (a plan the gate wrongly admitted).
_GATE_INPUT = {
    "requirements": [
        {
            "id": "R7",
            "text": "Coaches can export the team report.",
            "acceptance": "export produces a CSV with one row per athlete.",
            "defines": ["team report"],
            "references": ["team streak"],
        }
    ],
    "out_of_scope": [],
}


def _make_run(tmp_path: Path, run_id: str = "run-esc") -> EventLog:
    """A run log: a plan carrying the gate input, an admitted gate, then a FAILED outcome."""
    log = EventLog(run_id, root=tmp_path / "runs")
    log.append("plan", task_id="T1", input=_GATE_INPUT)
    emit_gate_result(log, "plan_gate", Verdict(admitted=True), task_id="T1")
    log.append("outcome", task_id="T1", failed=True)
    return log


def test_false_admit_is_harvested(tmp_path):
    """Covers AE4: passed gate then failed outcome -> one proposed draft with input + red_proof."""
    log = _make_run(tmp_path)
    quarantine = tmp_path / "_quarantine"

    created = harvest(log.dir, quarantine_dir=quarantine)

    assert len(created) == 1, created
    case_path = created[0]
    assert case_path.exists()

    case = load_case(case_path)
    assert case.component == "plan_gate"
    assert case.status == "proposed"
    assert case.input == _GATE_INPUT  # seeded with the offending gate input
    assert case.red_proof is not None
    assert case.red_proof["kind"] == "harvested"
    assert case.red_proof["run_id"] == "run-esc"
    assert case.red_proof["task_id"] == "T1"
    # red_proof references the originating events by seq (gate before outcome).
    assert case.red_proof["gate_result_seq"] < case.red_proof["outcome_seq"]


def test_reharvest_is_idempotent(tmp_path):
    """Covers AE5: re-running over the same log adds no duplicate draft."""
    log = _make_run(tmp_path)
    quarantine = tmp_path / "_quarantine"

    first = harvest(log.dir, quarantine_dir=quarantine)
    second = harvest(log.dir, quarantine_dir=quarantine)

    assert len(first) == 1
    assert second == []
    drafts = list(quarantine.rglob("case.yaml"))
    assert len(drafts) == 1


def test_passed_gate_then_success_is_not_harvested(tmp_path):
    """A passed gate with a later SUCCEEDED outcome is no escape -> nothing harvested."""
    log = EventLog("run-ok", root=tmp_path / "runs")
    log.append("plan", task_id="T1", input=_GATE_INPUT)
    emit_gate_result(log, "plan_gate", Verdict(admitted=True), task_id="T1")
    log.append("outcome", task_id="T1", failed=False)
    quarantine = tmp_path / "_quarantine"

    created = harvest(log.dir, quarantine_dir=quarantine)

    assert created == []
    assert list(quarantine.rglob("case.yaml")) == []


def test_false_reject_is_never_harvested(tmp_path):
    """KTD2: a rejected gate (admitted=False) is never harvested, even with a failed outcome."""
    log = EventLog("run-reject", root=tmp_path / "runs")
    log.append("plan", task_id="T1", input=_GATE_INPUT)
    rejected = Verdict(admitted=False, reasons=[Reason("R-NO-DANGLING", "dangling 'team streak'")])
    emit_gate_result(log, "plan_gate", rejected, task_id="T1")
    log.append("outcome", task_id="T1", failed=True)
    quarantine = tmp_path / "_quarantine"

    created = harvest(log.dir, quarantine_dir=quarantine)

    assert created == []
    assert list(quarantine.rglob("case.yaml")) == []


def test_proposed_case_is_excluded_from_green_locking(tmp_path):
    """Covers SC4: a harvested case is 'proposed' and excluded from the active locking set."""
    log = _make_run(tmp_path)
    quarantine = tmp_path / "_quarantine"
    harvest(log.dir, quarantine_dir=quarantine)

    cases = discover_cases(quarantine)
    assert cases, "harvested draft should be discoverable"
    assert all(c.status == "proposed" for c in cases)

    # The green-locking run only counts active cases; the proposed draft must not appear.
    active = [c for c in cases if c.status == "active"]
    assert active == []
