from datetime import datetime, timedelta, timezone

from agent_factory.af_audit_queue import audit_queue


def test_queue_accounts_for_every_original_once_and_keeps_new_cases_separate():
    now = datetime.now(timezone.utc)
    ids = list("ABCDEFGHIJ")
    statuses = ["passed", "failed", "passed", "passed", "blocked", "not_run", "not_run", "not_run", "blocked", "not_run"]
    cases = [{"case_id": key, "initial": {"status": state}, "initial_recorded_at": (now - timedelta(minutes=10-i)).isoformat(),
              "scenario": {"route": "/games", "role": "coach"}} for i, (key, state) in enumerate(zip(ids, statuses))]
    cases[0]["initial_recorded_at"] = (now - timedelta(minutes=25)).isoformat()
    cases[-1]["scenario"]["role"] = "player"
    meta = {"inventory_ids": ids, "initial_build": "pinned", "queue_deferrals": {"identity": ["F"], "external": []},
            "scheduling_assignments": {"G": {"status": "reserved"}}}
    cases.append({"case_id": "extra", "initial": {"status": "passed"}, "scenario": {}})
    result = audit_queue({"cases": cases, "run_meta": meta}, now=now)
    progress = result["progress"]
    assert (progress["completed"], progress["total"], progress["percent_complete"]) == (4, 10, 40)
    assert progress["blocked"] == progress["assigned"] == 1
    assert result["batches"][0]["case_ids"] == ["E", "I"]
    assert result["additional_cases"] == ["extra"]
    assert result["eta"]["runnable_hours"] > 0 and result["eta"]["full_audit_hours"] is None
    assert sorted(i for b in result["batches"] for i in b["case_ids"]) == ids[4:]


def test_queue_handles_old_runs_and_missing_timing_without_inventing_eta():
    assert audit_queue({"run_meta": {}})["state"] == "awaiting_inventory_reconciliation"
    result = audit_queue({"run_meta": {"inventory_ids": ["A"], "initial_build": "old"},
                          "cases": [{"case_id": "A", "initial": {"status": "blocked"}, "scenario": {}}]})
    assert result["batches"][0]["state"] == "blocked"
    assert result["eta"]["runnable_hours"] is None


def test_next_visits_favors_new_pages_and_combines_pending_and_retry():
    from agent_factory.af_audit_queue import next_visits
    cases = [{"case_id": key, "initial": {"status": state}, "scenario": {"route": route}}
             for key, state, route in [("A", "blocked", "/used"), ("B", "not_run", "/used"),
                                       ("C", "not_run", "/new"), ("D", "not_run", "/reserved")]]
    status = {"cases": cases, "run_meta": {"inventory_ids": list("ABCD"), "initial_build": "one",
              "queue_deferrals": {"identity": []}, "scheduling_assignments": {"D": {"status": "reserved"}}}}
    visits = next_visits(status, audit_queue(status))
    assert [v["case_ids"] for v in visits] == [["C"], ["A", "B"]]
