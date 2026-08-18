"""FL11 — the human channel: a dedicated ``/af-learn`` skill callable from any repo.

Covers the ticket's acceptance condition end-to-end:
  * a complaint filed from another repo lands a lesson plus active check in the NAMED project,
    with proof status recorded;
  * bulk mode inserts all requested checks without per-check oversight;
  * an unresolvable project space refuses with guidance and writes NOTHING;
  * an unproven check auto-upgrades to proven on its first live catch (both the machine
    report_only path FL12 already covered, and the FL11-new gating-but-unproven human path).

Shared test doubles (``WhoAmIStub``/``authed``/``recording_request``/``calls_for``/``check_store``)
live in ``tests/conftest.py`` — see it for the ingest-then-read-back double this file's upgrade
test needs.
"""

from __future__ import annotations

from typing import Any

import pytest
from hooks import _praxis

from agent_factory import af_learn, ingestion_api
from conftest import FakeCheckStore, authed, calls_for, recording_request

# --------------------------------------------------------------------------- E9: project resolution

def test_resolve_target_project_uses_explicit_argument_over_env() -> None:
    assert af_learn.resolve_target_project("explicit-proj", env={"FACTORY_PROJECT": "env-proj"}) == "explicit-proj"


def test_resolve_target_project_falls_back_to_factory_project_env() -> None:
    assert af_learn.resolve_target_project(None, env={"FACTORY_PROJECT": "env-proj"}) == "env-proj"


def test_resolve_target_project_strips_a_leading_prd_prefix() -> None:
    assert af_learn.resolve_target_project("prd-my-proj") == "my-proj"
    assert af_learn.resolve_target_project(None, env={"FACTORY_PROJECT": "prd-my-proj"}) == "my-proj"


def test_resolve_target_project_refuses_with_no_argument_and_no_env() -> None:
    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.resolve_target_project(None, env={})


def test_resolve_target_project_refuses_on_blank_strings() -> None:
    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.resolve_target_project("   ", env={"FACTORY_PROJECT": "   "})


# --------------------------------------------------------------------------- R9/E9: learn() single mode

def test_learn_lands_a_lesson_and_active_check_in_the_named_project_with_proof_status_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authed(monkeypatch)
    calls = recording_request(monkeypatch)

    result = af_learn.learn(
        "the deploy script silently swallows a failed migration", project="other-repo-proj",
        drafted_run="pytest tests/test_deploy_migration.py -q", source="matt via af-learn",
    )

    assert result["lesson_id"] is not None
    assert result["check_id"] is not None
    assert result["proof_status"] in ("proven", "unproven")

    lesson_calls = calls_for(calls, "lesson")
    check_calls = calls_for(calls, "check")
    assert lesson_calls and lesson_calls[0]["space"] == _praxis.FACTORY_LEARNINGS_SPACE
    assert check_calls and check_calls[0]["space"] == "other-repo-proj"
    check_meta = check_calls[0]["body"]["meta"]
    assert check_meta["channel"] == "human"
    assert check_meta["proof_status"] in ("proven", "unproven")
    # DF4: lenient human insert lands GATING immediately regardless of proof outcome.
    assert check_meta[ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING


def test_learn_offers_ticket_regression_in_the_same_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    authed(monkeypatch)
    calls = recording_request(monkeypatch)

    af_learn.learn("the login screen drops the session cookie on refresh", project="proj-x",
                   drafted_run="pytest tests/test_login_cookie.py -q", ticket_ids=["t1", "t2"])

    regress_calls = [c for c in calls if c["path"] == "/requirements/regress"]
    assert regress_calls and regress_calls[0]["body"]["ids"] == ["t1", "t2"]


def test_learn_with_no_drafted_check_still_lands_the_lesson_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authed(monkeypatch)
    calls = recording_request(monkeypatch)
    result = af_learn.learn("a bare complaint with nothing provable", project="proj-x")
    assert result["lesson_id"] is not None
    assert result["check_id"] is None
    assert all(c["body"].get("category") != "check" for c in calls if c["body"])


def test_learn_refuses_and_writes_nothing_when_project_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authed(monkeypatch)
    calls: list[Any] = []
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: calls.append((a, kw)) or {})
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: calls.append(("ensure_space", a)) or a[0])

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn("a complaint with no named project", project=None, env={})

    assert calls == [], f"an unresolvable-project learn() call must write NOTHING: {calls}"


# --------------------------------------------------------------------------- R9/E10: learn_bulk()

def test_learn_bulk_inserts_every_requested_check_without_per_check_oversight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authed(monkeypatch)
    calls = recording_request(monkeypatch)

    results = af_learn.learn_bulk(
        [
            {"complaint_text": "complaint one", "drafted_run": "pytest tests/test_one.py -q"},
            {"complaint_text": "complaint two", "drafted_run": "pytest tests/test_two.py -q"},
            {"complaint_text": "complaint three"},  # no drafted check — lesson-only entry
        ],
        project="bulk-proj",
    )

    assert len(results) == 3
    assert all(r["lesson_id"] is not None for r in results)
    assert results[0]["check_id"] is not None
    assert results[1]["check_id"] is not None
    assert results[2]["check_id"] is None

    check_calls = calls_for(calls, "check")
    assert len(check_calls) == 2
    assert all(c["body"]["meta"]["channel"] == "human" for c in check_calls)
    assert all(c["space"] == "bulk-proj" for c in check_calls)


def test_learn_bulk_is_idempotent_within_and_across_batches(check_store: FakeCheckStore) -> None:
    """Regression (the '24 rows from 12 identical entries' repro): identical lesson-only entries
    must yield ONE row each — deduped WITHIN a single batch (in-memory, robust to read-after-write
    lag) and ACROSS repeated batches (exact content-hash against the corpus). R2 is preserved: every
    result still carries a lesson id (the existing one when collapsed), never a dropped complaint."""
    distinct = [{"complaint_text": f"identical complaint number {i}"} for i in range(3)]
    batch = distinct + [dict(e) for e in distinct]  # 6 entries, 3 distinct + 3 in-batch repeats

    def lesson_rows() -> list[dict[str, Any]]:
        return [f for f in check_store.facts.values() if f["category"] == "lesson"]

    first = af_learn.learn_bulk([dict(e) for e in batch], project="dedup-proj")
    assert len(lesson_rows()) == 3, "within-batch dedup must collapse the 3 in-batch repeats"
    assert sum(1 for r in first if r.get("batch_deduped")) == 3

    second = af_learn.learn_bulk([dict(e) for e in batch], project="dedup-proj")
    assert len(lesson_rows()) == 3, "a repeated batch must not write any new lesson rows"

    assert all(r["lesson_id"] is not None for r in first + second), "R2: a lesson id is always returned"
    # The second batch is entirely duplicates — every entry points back at an existing lesson.
    assert all(r.get("lesson_duplicate_of") for r in second)


def test_learn_bulk_refuses_and_writes_nothing_when_project_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authed(monkeypatch)
    calls: list[Any] = []
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: calls.append((a, kw)) or {})

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn_bulk(
            [{"complaint_text": "one"}, {"complaint_text": "two"}], project=None, env={},
        )

    assert calls == [], f"an unresolvable-project learn_bulk() call must write NOTHING: {calls}"


# --------------------------------------------------------------------------- R10: unproven -> proven upgrade

def test_unproven_gating_human_check_upgrades_to_proven_on_first_live_catch(
    check_store: FakeCheckStore,
) -> None:
    """FL11's new case: DF4 already lands a lenient human check as GATING even when unproven — the
    first REAL (non-drafting) pass must clear that unproven flag without touching enforcement
    state (it already gates). Distinct from FL12's existing REPORT_ONLY -> GATING machine-channel
    upgrade, which this must not regress (see test_ingestion_api_fl12.py)."""
    result = af_learn.learn("humans always review before shipping", project="proj-x",
                            drafted_run="pytest tests/test_healthz.py -q")
    assert result["proof_status"] == "unproven"  # no proof_runner wired -> unproven per attempt_proof

    check_fact = check_store.check(result["check_id"])
    assert check_fact["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING

    upgraded = ingestion_api.upgrade_on_first_pass(check_fact["meta"]["check_id"], "proj-x", True)

    assert upgraded["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert upgraded["meta"]["proof_status"] == "proven"


def test_unproven_gating_human_check_is_a_no_op_on_a_failing_execution(
    check_store: FakeCheckStore,
) -> None:
    result = af_learn.learn("humans always review before shipping", project="proj-x",
                            drafted_run="pytest tests/test_healthz.py -q")
    check_fact = check_store.check(result["check_id"])

    upgraded = ingestion_api.upgrade_on_first_pass(check_fact["meta"]["check_id"], "proj-x", False)

    assert upgraded["meta"]["proof_status"] == "unproven"


# --------------------------------------------------------------------------- E9: never writes cross-org

# --------------------------------------------------------------------------- R41: get_lesson() read

def test_get_lesson_returns_the_same_text_and_metadata_a_prior_learn_call_wrote(
    check_store: FakeCheckStore,
) -> None:
    result = af_learn.learn("the deploy script silently swallows a failed migration",
                            project="proj-x", source="matt via af-learn")
    lesson_id = result["lesson_id"]

    read_back = af_learn.get_lesson(lesson_id)

    assert read_back["found"] is True
    assert read_back["lesson_id"] == lesson_id
    assert read_back["text"] == "the deploy script silently swallows a failed migration"
    assert read_back["meta"]["channel"] == "human"
    assert read_back["source"] == "matt via af-learn"


def test_get_lesson_reads_accumulated_metadata_added_after_the_original_write(
    check_store: FakeCheckStore,
) -> None:
    """R42's provenance accumulation is one example of metadata a lesson gains AFTER its original
    ``learn()`` write; ``get_lesson`` must reflect the current state, not a stale write-time copy."""
    result = af_learn.learn("a second complaint about the same deploy script", project="proj-x")
    lesson_id = result["lesson_id"]
    ingestion_api._append_lesson_provenance(lesson_id, source="a later report", channel="human")

    read_back = af_learn.get_lesson(lesson_id)

    assert read_back["meta"]["provenance"][-1]["source"] == "a later report"


def test_get_lesson_returns_a_clear_not_found_result_for_an_unknown_id(
    check_store: FakeCheckStore,
) -> None:
    result = af_learn.get_lesson("no-such-id")
    assert result == {"found": False, "lesson_id": "no-such-id", "reason": "not_found"}


def test_get_lesson_returns_a_clear_wrong_category_result_for_a_non_lesson_id(
    check_store: FakeCheckStore,
) -> None:
    check_store.seed_check("plan-abc123", {"channel": "human"})
    result = af_learn.get_lesson("fact-plan-abc123")
    assert result["found"] is False
    assert result["reason"] == "wrong_category"
    assert result["category"] == "check"


def test_get_lesson_is_scoped_to_the_shared_factory_learnings_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same org/project scoping as ``get_fact`` — reading a lesson by id never needs a caller-named
    project because every lesson lives in the one shared ``FACTORY_LEARNINGS_SPACE``."""
    authed(monkeypatch)
    seen: dict[str, Any] = {}

    def fake_get_fact(cid: str, *, space: str | None = None, snapshot: str | None = None,
                      not_found_ok: bool = False) -> dict[str, Any]:
        seen.update(cid=cid, space=space, snapshot=snapshot, not_found_ok=not_found_ok)
        return {"id": cid, "category": "lesson", "content": "x", "source": None, "meta": {}}

    monkeypatch.setattr(_praxis, "get_fact", fake_get_fact)

    af_learn.get_lesson("some-lesson-id")

    assert seen == {
        "cid": "some-lesson-id",
        "space": _praxis.FACTORY_LEARNINGS_SPACE,
        "snapshot": _praxis.FACTORY_LEARNINGS_SNAPSHOT,
        "not_found_ok": True,
    }


def test_learn_never_writes_to_the_shared_learnings_space_before_project_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stronger E9 check: even the FACTORY_LEARNINGS_SPACE (the one write every successful call
    always makes) must not be touched when the project cannot be resolved."""
    authed(monkeypatch)
    written_spaces: list[str | None] = []

    def fake_request(method: str, path: str, *, space: str | None = None, **kw: Any) -> dict[str, Any]:
        written_spaces.append(space)
        return {}

    monkeypatch.setattr(_praxis, "_request", fake_request)

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn("orphan complaint", project=None, env={})

    assert _praxis.FACTORY_LEARNINGS_SPACE not in written_spaces
    assert written_spaces == []
