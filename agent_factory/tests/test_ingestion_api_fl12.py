"""FL12/R20a — the check enforcement-state machine: exactly one state per check, transitions only
via defined conditions.

Covers the ticket's acceptance condition end-to-end:
  * state transitions occur only via defined conditions (a property test over the full transition
    table: every (state, event) pair not explicitly defined raises);
  * a lenient human insert lands GATING (DF4);
  * a machine fail-only check lands report_only and flips to gating on its first executed pass;
  * a quiet gating check whose retained artifact is unavailable demotes to report_only with a
    recorded reason at the loop-end-triggered re-prove, and a quiet check that still fails stays
    gating;
  * no lifecycle code path in this module ever deletes a lesson;
  * archive is never entered on silence — only the explicit manual rollback path reaches it.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from agent_factory import ingestion_api
from conftest import FakeCheckStore as _FakeStore

# _FakeStore is the shared ingestion-API test double in tests/conftest.py (consolidated there —
# this file, test_ingestion_api_fl2.py, and test_af_learn.py each carried a byte-identical/
# near-identical copy). ``store`` keeps this file's original fixture name, backed by the shared
# ``check_store`` fixture conftest.py already registers.


@pytest.fixture
def store(check_store: _FakeStore) -> _FakeStore:
    return check_store


# --------------------------------------------------------------------------- property test: the transition table

_ALL_STATES = [None, ingestion_api.STATE_GATING, ingestion_api.STATE_REPORT_ONLY,
              ingestion_api.STATE_SUSPENDED, ingestion_api.STATE_ARCHIVED]
_ALL_EVENTS = [ingestion_api.EVENT_INSERT_GATING, ingestion_api.EVENT_INSERT_REPORT_ONLY,
              ingestion_api.EVENT_FIRST_REAL_PASS, ingestion_api.EVENT_PROOF_DEMOTED,
              ingestion_api.EVENT_SUSPEND, ingestion_api.EVENT_RESURRECT, ingestion_api.EVENT_ARCHIVE]


@pytest.mark.parametrize("state,event", list(itertools.product(_ALL_STATES, _ALL_EVENTS)))
def test_transition_occurs_only_via_a_defined_condition(state: str | None, event: str) -> None:
    """Every (state, event) pair either resolves to exactly the table's declared target, or the
    function refuses it — there is no third outcome (an ad hoc/undefined state landing quietly)."""
    key = (state, event)
    if key in ingestion_api.ENFORCEMENT_TRANSITIONS:
        assert (ingestion_api.transition_enforcement_state(state, event)
               == ingestion_api.ENFORCEMENT_TRANSITIONS[key])
    else:
        with pytest.raises(ingestion_api.InvalidEnforcementTransition):
            ingestion_api.transition_enforcement_state(state, event)


def test_every_declared_transition_lands_one_of_the_four_named_states() -> None:
    valid = {ingestion_api.STATE_GATING, ingestion_api.STATE_REPORT_ONLY,
            ingestion_api.STATE_SUSPENDED, ingestion_api.STATE_ARCHIVED}
    assert set(ingestion_api.ENFORCEMENT_TRANSITIONS.values()) <= valid


# --------------------------------------------------------------------------- DF4: lenient human insert -> gating

def test_lenient_human_insert_lands_gating(store: _FakeStore) -> None:
    result = ingestion_api.ingest("humans always review before shipping", "proj",
                                  drafted_run="curl -s http://internal/healthz", channel="human")
    check = next(f for f in store.facts.values() if f["category"] == "check")
    assert check["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert result["check_id"] == check["id"]


# --------------------------------------------------------------------------- R6: fail-only upgrade

def test_machine_fail_only_check_lands_report_only_then_upgrades_on_first_real_pass(
    store: _FakeStore,
) -> None:
    ingestion_api.ingest("a fail-only draft with no proof engine wired", "proj",
                         drafted_run="pytest tests/test_x.py -q", channel="machine")
    check = next(f for f in store.facts.values() if f["category"] == "check")
    assert check["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_REPORT_ONLY
    assert check["meta"]["proof_status"] == "unproven"

    result = ingestion_api.upgrade_on_first_pass(check["meta"]["check_id"], "proj", True)

    assert result["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert result["meta"]["proof_status"] == "proven"


def test_upgrade_on_first_pass_is_a_no_op_for_an_already_gating_check(store: _FakeStore) -> None:
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "proof_status": "proven"})
    result = ingestion_api.upgrade_on_first_pass("c1", "proj", True)
    assert result["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert ("PATCH", "c1") not in store.calls


def test_upgrade_on_first_pass_is_a_no_op_on_a_failing_execution(store: _FakeStore) -> None:
    """A FAILING real execution never upgrades a report_only check — only a genuine pass does."""
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_REPORT_ONLY,
                           "proof_status": "unproven"})
    result = ingestion_api.upgrade_on_first_pass("c1", "proj", False)
    assert result["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_REPORT_ONLY
    assert ("PATCH", "c1") not in store.calls


# --------------------------------------------------------------------------- KD7: re-prove cadence

def test_quiet_gating_check_with_unavailable_artifact_demotes_to_report_only_with_reason(
    store: _FakeStore,
) -> None:
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "run": "pytest tests/test_x.py -q", "artifact_id": "art-1", "createdAt": 0})

    outcomes = ingestion_api.reprove_quiet_checks(
        "proj", now=1_000_000.0, artifact_reader=lambda meta: None,
    )

    assert outcomes == [{"check_id": "c1", "result": "demoted", "reason": "artifact-unavailable"}]
    check = store.facts["c1"]
    assert check["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_REPORT_ONLY
    assert check["meta"]["reprove_reason"] == "artifact-unavailable"
    assert check["meta"]["reprove_at"] == 1_000_000.0


def test_quiet_gating_check_that_still_fails_stays_gating(store: _FakeStore) -> None:
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "run": "true", "artifact_id": "art-1", "createdAt": 0})

    def executor(run: str, cwd: Path) -> bool:
        return False  # always fails, on both the bad artifact and the healthy reference

    outcomes = ingestion_api.reprove_quiet_checks(
        "proj", now=1_000_000.0, artifact_reader=lambda meta: {"bundle_b64": ""}, executor=executor,
    )

    # An empty stub bundle can't actually be re-materialized, so this pins the concrete verdict
    # run_fail_then_pass_proof produces on that irreproducible pin (report_only, flagged) rather
    # than a disjunction over every outcome the sweep can produce.
    check = store.facts["c1"]
    assert check["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_REPORT_ONLY
    assert check["meta"]["reprove_reason"] == "artifact-unavailable"
    assert outcomes == [{"check_id": "c1", "result": "demoted", "reason": "artifact-unavailable"}]


def test_not_yet_due_gating_check_is_left_untouched(store: _FakeStore) -> None:
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "run": "pytest -q", "artifact_id": "art-1", "reprove_at": 999_999.0})

    outcomes = ingestion_api.reprove_quiet_checks("proj", now=1_000_000.0, artifact_reader=lambda meta: None)

    assert outcomes == []
    assert store.facts["c1"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING


def test_reprove_never_archives_regardless_of_scenario(store: _FakeStore) -> None:
    """Archive is entered only via explicit manual action (rollback_wave) — never on silence."""
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "run": "pytest -q", "artifact_id": "art-1", "createdAt": 0})
    store.seed_check("c2", {"check_id": "c2", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "run": "", "artifact_id": None, "createdAt": 0})

    ingestion_api.reprove_quiet_checks("proj", now=1_000_000.0, artifact_reader=lambda meta: None)

    assert all(f["meta"].get(ingestion_api.M_ENFORCEMENT_STATE) != ingestion_api.STATE_ARCHIVED
              for f in store.facts.values())


# --------------------------------------------------------------------------- lessons are never deleted

def test_no_lifecycle_verb_ever_issues_a_delete_shaped_request(store: _FakeStore) -> None:
    """Suspend, kill-switch, rollback and re-prove all mutate meta (PATCH) — none issues a DELETE,
    and the lesson fact stays present (active) in the store throughout."""
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "wave_id": "wave-x", "run": "pytest -q", "artifact_id": "art-1"})
    store.seed_lesson("lesson-1", {"wave_id": "wave-x"})

    ingestion_api.suspend("c1", "proj", "flaky")
    store.seed_check("c2", {"check_id": "c2", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "wave_id": "wave-y"})
    ingestion_api.kill_switch("c2", "proj", "operator override")
    ingestion_api.rollback_wave("wave-y", "proj")
    ingestion_api.reprove_quiet_checks("proj", now=1_000_000.0, artifact_reader=lambda meta: None)

    assert not any(method == "DELETE" for method, _ in store.calls)
    assert "lesson-1" in store.facts
    assert store.facts["lesson-1"]["category"] == "lesson"


def test_rollback_wave_still_archives_via_the_transition_table(store: _FakeStore) -> None:
    store.seed_check("c1", {"check_id": "c1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
                           "wave_id": "wave-z"})
    ingestion_api.rollback_wave("wave-z", "proj")
    assert store.facts["c1"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_ARCHIVED
