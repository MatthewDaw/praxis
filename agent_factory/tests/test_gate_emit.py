"""U4: gate_result emission helper.

Emission rides the existing ``gate_result`` event type (no vocabulary change).
"""

from agent_factory.event_log import EVENT_TYPES, EventLog
from agent_factory.gate import (
    REGISTRY,
    emit_gate_result,
)


def test_emit_gate_result_appends_one_gate_result_event(tmp_path):
    log = EventLog("emit-probe", root=tmp_path)
    verdict = REGISTRY["plan_gate"].evaluate(
        {"requirements": [{"id": "R1", "text": "x", "acceptance": "", "source": "prd-team-app"}]}
    )
    rec = emit_gate_result(log, "plan_gate", verdict, task_id="task-1")

    assert rec["type"] == "gate_result"
    assert "gate_result" in EVENT_TYPES
    assert rec["component"] == "plan_gate"
    assert rec["admitted"] is False
    assert rec["rule_ids"] == ["R-ACCEPT-BINARY"]
    assert rec["task_id"] == "task-1"

    events = log.read()
    assert len([e for e in events if e["type"] == "gate_result"]) == 1


def test_emit_is_opt_in_no_log_no_event(tmp_path):
    # Evaluating without passing a log emits nothing (pure unit eval stays log-free).
    log = EventLog("emit-optin", root=tmp_path)
    REGISTRY["plan_gate"].evaluate({"requirements": []})
    assert log.read() == []
