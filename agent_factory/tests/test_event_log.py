import pytest

from agent_factory.event_log import EventLog


def test_append_and_read_roundtrip(tmp_path):
    log = EventLog("run-1", root=tmp_path)
    log.append("run_start", goal="build a thing")
    ev = log.append("memory_write", tool="praxis_add_insight", fact="X is true")
    assert ev["seq"] == 2
    events = log.read()
    assert [e["type"] for e in events] == ["run_start", "memory_write"]
    assert events[0]["seq"] == 1 and events[1]["seq"] == 2
    assert all(e["run_id"] == "run-1" and "ts" in e for e in events)


def test_parent_seq_records_causality(tmp_path):
    log = EventLog("run-2", root=tmp_path)
    a = log.append("tool_call", tool="run_tests")
    log.append("gate_result", parent_seq=a["seq"], passed=False)
    events = log.read()
    assert events[1]["parent_seq"] == events[0]["seq"]


def test_reopen_resumes_sequence(tmp_path):
    EventLog("run-3", root=tmp_path).append("run_start")
    reopened = EventLog("run-3", root=tmp_path)
    ev = reopened.append("note", text="resumed")
    assert ev["seq"] == 2  # continues, does not restart at 1


def test_unknown_event_type_rejected(tmp_path):
    log = EventLog("run-4", root=tmp_path)
    with pytest.raises(ValueError):
        log.append("not_a_real_type")


def test_invalid_run_id_rejected(tmp_path):
    with pytest.raises(ValueError):
        EventLog("bad/id", root=tmp_path)
