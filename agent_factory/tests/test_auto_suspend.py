"""FL13 (R19) — the false-positive auto-suspend signal + the manual kill switch.

Covers the ticket's acceptance condition:
  * the fixture sequence of repeated no-relevant-change regressions trips auto-suspension and
    emits a flag event (:func:`ingestion_api.attempt_auto_suspend`, wired into
    :func:`ingestion_api.regress_by_check` — the entry point for "an already-existing check failed
    this ticket again" — so every such regression checks its own streak in the same motion, never a
    caller's separate responsibility to remember);
  * the kill switch flips a gating check to suspended in one command with the reason recorded
    (:func:`ingestion_api.kill_switch`, already existing — this file locks its behavior in as part
    of the same acceptance condition).
"""

from __future__ import annotations

from typing import Any

import pytest
from hooks import _praxis

from agent_factory import ingestion_api

# --------------------------------------------------------------------------- regression_streak

def test_streak_counts_same_check_same_commit_runs():
    entries = [
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-1", "commit_sha": "sha-a"},
    ]
    assert ingestion_api.regression_streak(entries, "chk-1") == 3


def test_streak_breaks_on_a_different_commit_sha():
    """A DIFFERENT sha is evidence something changed — the run resets, it does not accumulate."""
    entries = [
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-1", "commit_sha": "sha-b"},
        {"check_id": "chk-1", "commit_sha": "sha-b"},
    ]
    assert ingestion_api.regression_streak(entries, "chk-1") == 2


def test_streak_breaks_on_a_different_check():
    entries = [
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-2", "commit_sha": "sha-a"},
    ]
    assert ingestion_api.regression_streak(entries, "chk-2") == 1


def test_missing_sha_ends_the_run_because_there_is_no_evidence_nothing_changed():
    """D3 — a MISSING commit_sha is not evidence of sameness. Both regress entry points default
    ``commit_sha=None`` and the shell writers supply none, so treating "no sha" as "nothing
    changed" degenerated "N regressions with no relevant change" into plain "N regressions" and
    auto-suspended a CORRECT gating check that had caught three genuinely different defects."""
    entries = [
        {"check_id": "chk-1"},
        {"check_id": "chk-1"},
        {"check_id": "chk-1"},
    ]
    assert ingestion_api.regression_streak(entries, "chk-1") == 0


def test_a_sha_less_entry_in_the_middle_truncates_the_run():
    entries = [
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-1"},
        {"check_id": "chk-1", "commit_sha": "sha-a"},
        {"check_id": "chk-1", "commit_sha": "sha-a"},
    ]
    assert ingestion_api.regression_streak(entries, "chk-1") == 2


def test_an_already_resolved_entry_ends_the_run():
    """A finding that was stamped resolved is a CLOSED one -- it is not part of a live
    false-positive run, and counting it inflates the streak toward a wrongful suspension."""
    entries = [
        {"check_id": "chk-1", "commit_sha": "sha-a", "resolved": True},
        {"check_id": "chk-1", "commit_sha": "sha-a", "resolved": False},
        {"check_id": "chk-1", "commit_sha": "sha-a", "resolved": False},
    ]
    assert ingestion_api.regression_streak(entries, "chk-1") == 2


def test_a_correct_check_catching_three_different_defects_is_never_auto_suspended(_stub_transport):
    """The D3 acceptance case, end to end through the decision function: three regressions with no
    recorded sha (exactly what the shell writers produce today) must NOT suspend."""
    entries = [{"check_id": "chk-1", "reason": f"defect {i}"} for i in range(3)]
    result = ingestion_api.attempt_auto_suspend("chk-1", "proj", "ticket-1", entries)
    assert result["status"] == "observed"
    assert result["streak"] == 0
    assert not _stub_transport["patches"], "a healthy check must never be suspended on no evidence"
    assert not _stub_transport["written"], "no false-positive lesson may be written either"


def test_empty_history_streaks_zero():
    assert ingestion_api.regression_streak([], "chk-1") == 0
    assert ingestion_api.regression_streak(None, "chk-1") == 0


# --------------------------------------------------------------------------- attempt_auto_suspend

@pytest.fixture
def _stub_transport(monkeypatch, _reset_check_state):
    """Same pattern as ``test_widening.py``'s ``_no_real_flags``: stub the transport primitives so
    these tests exercise the DECISION logic (streak -> suspend -> lesson + flag), never Praxis.
    NOT autouse — the later end-to-end tests drive the real ``_praxis`` layer via ``_fake_praxis``
    instead, and must not have these lower-level patches (and ``_CHECK_STATE``, a separate store)
    shadow that."""
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: identity or "tester")
    written: list[dict[str, Any]] = []

    def fake_write_insight(text, category, **kw):
        entry = {"text": text, "category": category, **kw}
        written.append(entry)
        return {"id": f"fake-{len(written)}"}

    monkeypatch.setattr(ingestion_api, "_write_insight", fake_write_insight)

    patches: list[dict[str, Any]] = []

    def fake_patch_check(check_id, project, build_patch, *, identity=None):
        current = {"id": check_id, "meta": dict(_CHECK_STATE.get(check_id) or {})}
        patch = build_patch(current) if callable(build_patch) else dict(build_patch)
        _CHECK_STATE.setdefault(check_id, {}).update(patch)
        patches.append({"check_id": check_id, "project": project, "patch": patch})
        return {"id": check_id, "meta": dict(_CHECK_STATE[check_id])}

    monkeypatch.setattr(ingestion_api, "_patch_check", fake_patch_check)

    def fake_fetch_check(check_id, project):
        return {"id": check_id, "meta": dict(_CHECK_STATE.get(check_id) or {})}

    monkeypatch.setattr(ingestion_api, "_fetch_check", fake_fetch_check)
    return {"written": written, "patches": patches}


_CHECK_STATE: dict[str, dict[str, Any]] = {}


@pytest.fixture
def _reset_check_state():
    _CHECK_STATE.clear()
    _CHECK_STATE["chk-1"] = {ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING}
    yield
    _CHECK_STATE.clear()


def _entries(n: int, *, check_id: str = "chk-1", commit_sha: str = "sha-a") -> list[dict[str, Any]]:
    return [{"check_id": check_id, "commit_sha": commit_sha} for _ in range(n)]


def test_below_threshold_only_observes(_stub_transport):
    result = ingestion_api.attempt_auto_suspend("chk-1", "proj", "ticket-1", _entries(2))
    assert result == {"status": "observed", "streak": 2, "threshold": 3}
    assert not _stub_transport["patches"], "must not suspend below threshold"
    assert not _stub_transport["written"], "must not write a lesson below threshold"


def test_fixture_sequence_of_repeated_no_change_regressions_trips_auto_suspension(_stub_transport):
    """The acceptance-condition fixture: three consecutive regressions of one ticket by one check,
    at the SAME commit each time (no relevant change), trip auto-suspension and emit a flag."""
    result = ingestion_api.attempt_auto_suspend("chk-1", "proj", "ticket-1", _entries(3))
    assert result["status"] == "suspended"
    assert result["streak"] == 3

    # the check itself is now suspended, not gating
    assert _CHECK_STATE["chk-1"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_SUSPENDED

    # a suspension flag was emitted (push-not-pull, FL18) alongside the lesson annotation
    kinds = [w["category"] for w in _stub_transport["written"]]
    assert ingestion_api.FLAG_CATEGORY in kinds, "a suspension flag event must be emitted"
    assert ingestion_api.LESSON_CATEGORY in kinds, "the suspension must be recorded as a lesson annotation"

    flag_entry = next(w for w in _stub_transport["written"] if w["category"] == ingestion_api.FLAG_CATEGORY)
    assert flag_entry["meta"]["kind"] == ingestion_api.FLAG_KIND_SUSPENSION
    assert flag_entry["meta"]["check_id"] == "chk-1"


def test_a_change_in_between_resets_the_streak_and_never_suspends(_stub_transport):
    entries = _entries(2, commit_sha="sha-a") + _entries(2, commit_sha="sha-b")
    result = ingestion_api.attempt_auto_suspend("chk-1", "proj", "ticket-1", entries)
    assert result["status"] == "observed"
    assert result["streak"] == 2
    assert not _stub_transport["patches"]


def test_already_suspended_check_is_left_alone(_stub_transport):
    _CHECK_STATE["chk-1"][ingestion_api.M_ENFORCEMENT_STATE] = ingestion_api.STATE_SUSPENDED
    result = ingestion_api.attempt_auto_suspend("chk-1", "proj", "ticket-1", _entries(5))
    assert result["status"] == "already-suspended"
    assert not _stub_transport["patches"], "must not re-invoke suspend on an already-suspended check"


# --------------------------------------------------------------------------- kill switch (manual brake)

def test_kill_switch_flips_a_gating_check_to_suspended_with_the_reason_recorded(_stub_transport):
    result = ingestion_api.kill_switch("chk-1", "proj", "false positive on unrelated flake")
    assert result["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_SUSPENDED
    assert result["meta"]["kill_switch"] is True
    assert result["meta"]["kill_switch_reason"] == "false positive on unrelated flake"

    flag_entry = next(w for w in _stub_transport["written"] if w["category"] == ingestion_api.FLAG_CATEGORY)
    assert flag_entry["meta"]["kill_switch"] is True
    assert flag_entry["meta"]["reason"] == "false positive on unrelated flake"


# --------------------------------------------------------------------------- end-to-end via ingest()

class _WhoAmIStub:
    def __init__(self, ok: bool, principal: str = "user-1") -> None:
        self.ok, self.principal, self.detail = ok, principal, ""


@pytest.fixture
def _fake_praxis(monkeypatch):
    """A full ``_praxis`` double so :func:`ingestion_api.ingest` can be driven end-to-end,
    repeatedly, against one ticket + one check, and actually observe the auto-suspend wiring
    fire on the SAME motion as the regression (not just the isolated decision function)."""
    monkeypatch.setattr(_praxis, "whoami", lambda: _WhoAmIStub(True))
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])

    tickets: dict[str, dict[str, Any]] = {"t1": {"meta": {}}}
    checks: dict[str, dict[str, Any]] = {}
    written: list[dict[str, Any]] = []
    counter = {"n": 0}

    def fake_request(method, path, *, body=None, params=None, space=None, snapshot=None, **kw):
        if method == "POST" and path == "/insights":
            counter["n"] += 1
            fid = f"fake-{counter['n']}"
            entry = dict(body or {})
            entry["id"] = fid
            written.append(entry)
            if entry.get("category") == ingestion_api.CHECK_CATEGORY:
                checks[entry["meta"]["check_id"]] = {"id": fid, "meta": dict(entry["meta"])}
            return {"id": fid, "action": "added"}
        if method == "POST" and path == "/requirements/regress":
            return {"count": len((body or {}).get("ids", []))}
        return {}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "get_fact", lambda cid, **kw: tickets.get(cid, {"meta": {}}))

    def fake_regress_requirements(project, ids, *, detail=None, **kw):
        for tid in ids:
            if detail and tid in detail:
                tickets.setdefault(tid, {"meta": {}})["meta"].update(detail[tid])
        return {"count": len(ids)}

    monkeypatch.setattr(_praxis, "regress_requirements", fake_regress_requirements)

    def fake_write_build_state(cid, patch, **kw):
        # D2: the single reconciled regress path parks a ticket at the regress-cycle cap, which
        # writes build state directly -- this double must cover that write too, or the cap trip
        # would escape to a real backend.
        tickets.setdefault(cid, {"meta": {}})["meta"].update(patch)
        return {}

    monkeypatch.setattr(_praxis, "write_build_state", fake_write_build_state)

    def fake_facts_by(category=None, meta=None, **kw):
        if category == ingestion_api.CHECK_CATEGORY and meta and meta.get("check_id"):
            hit = checks.get(meta["check_id"])
            return [hit] if hit else []
        return []

    monkeypatch.setattr(_praxis, "facts_by", fake_facts_by)

    def fake_patch_meta(cid, patch, **kw):
        for chk in checks.values():
            if chk["id"] == cid:
                chk["meta"].update(patch)
                return chk
        return {}

    monkeypatch.setattr(_praxis, "patch_meta", fake_patch_meta)
    return {"tickets": tickets, "checks": checks, "written": written}


def _drafted_check_id(fake_praxis: dict[str, Any]) -> str:
    """Drafts a real check via :func:`ingestion_api.ingest` and returns its ``meta.check_id``
    SLUG — the identifier :func:`ingestion_api.suspend`/:func:`kill_switch`/:func:`regress_by_check`
    all key off (via :func:`ingestion_api._fetch_check`'s ``facts_by`` lookup), which is DISTINCT
    from the fact storage id ``ingest`` returns as ``result["check_id"]``."""
    result = ingestion_api.ingest(
        "a lesson", "proj", drafted_run="pytest tests/test_x.py -q",
        channel="machine", ticket_ids=["t1"], commit_sha="sha-0",
    )
    assert result["check_id"] is not None
    return next(w["meta"]["check_id"] for w in fake_praxis["written"]
               if w["category"] == ingestion_api.CHECK_CATEGORY)


def test_fixture_sequence_of_repeated_regressions_via_regress_by_check_auto_suspends(_fake_praxis):
    """The literal acceptance fixture: an ALREADY-EXISTING check re-fails the SAME ticket at the
    SAME commit, :data:`ingestion_api.DEFAULT_AUTO_SUSPEND_THRESHOLD` times in a row via the real
    :func:`ingestion_api.regress_by_check` entry point, trips auto-suspension automatically and
    emits a flag event -- no separate call anything has to remember to make."""
    check_id = _drafted_check_id(_fake_praxis)
    result = None
    for _ in range(ingestion_api.DEFAULT_AUTO_SUSPEND_THRESHOLD):
        result = ingestion_api.regress_by_check(
            "proj", "t1", check_id, "still failing", commit_sha="sha-fixed",
        )
    assert result["auto_suspend"]["status"] == "suspended"
    assert _fake_praxis["checks"][check_id]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == (
        ingestion_api.STATE_SUSPENDED
    )
    kinds = [w["category"] for w in _fake_praxis["written"]]
    assert kinds.count(ingestion_api.FLAG_CATEGORY) >= 1, "a suspension flag must have been emitted"
    assert kinds.count(ingestion_api.LESSON_CATEGORY) >= 2, "the suspension is recorded as a lesson too"


def test_a_real_fix_landing_between_regressions_never_auto_suspends(_fake_praxis):
    """A REAL fix landing between regressions (a different commit each time) must never trip the
    false-positive signal -- the check keeps gating."""
    check_id = _drafted_check_id(_fake_praxis)
    result = None
    for i in range(ingestion_api.DEFAULT_AUTO_SUSPEND_THRESHOLD):
        result = ingestion_api.regress_by_check(
            "proj", "t1", check_id, "still failing", commit_sha=f"sha-{i}",
        )
    assert result["auto_suspend"]["status"] == "observed"
    # unproven at draft time (no real proof engine wired here) -> report_only, not gating; the
    # point under test is that it stayed OFF suspended, not which non-suspended state it started in
    assert _fake_praxis["checks"][check_id]["meta"][ingestion_api.M_ENFORCEMENT_STATE] != (
        ingestion_api.STATE_SUSPENDED
    )
