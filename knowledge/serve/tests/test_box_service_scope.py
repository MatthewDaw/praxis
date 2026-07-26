"""A job's build scope is af-build's own mvp+automated build target: the job
stays "not complete" while any in-scope requirement is neither finished nor
blocked, and post-mvp / manual-verify requirements never hold it open (R4)."""

from __future__ import annotations

from knowledge.serve.box_service_scope import in_job_scope, job_scope_complete


def _req(*, scope="mvp", verify="automated", build_state="incomplete", rid="R1"):
    return {
        "id": rid,
        "meta": {
            "scope": scope,
            "verify": verify,
            "build_state": build_state,
            "requirement_id": rid,
        },
    }


def test_incomplete_mvp_automated_ticket_keeps_job_running():
    facts = [_req(build_state="incomplete")]

    assert job_scope_complete(facts) is False


def test_in_progress_mvp_automated_ticket_keeps_job_running():
    facts = [_req(build_state="in_progress")]

    assert job_scope_complete(facts) is False


def test_job_complete_when_every_in_scope_ticket_is_finished():
    facts = [_req(rid="R1", build_state="finished"), _req(rid="R2", build_state="finished")]

    assert job_scope_complete(facts) is True


def test_job_complete_when_in_scope_ticket_is_blocked_not_finished():
    facts = [_req(build_state="blocked")]

    assert job_scope_complete(facts) is True


def test_mixed_scope_incomplete_mvp_automated_still_blocks_even_if_others_done():
    facts = [
        _req(rid="R1", build_state="finished"),
        _req(rid="R2", build_state="incomplete"),
    ]

    assert job_scope_complete(facts) is False


def test_post_mvp_ticket_never_holds_job_open():
    facts = [_req(scope="post-mvp", build_state="incomplete")]

    assert job_scope_complete(facts) is True


def test_manual_verify_mvp_ticket_never_holds_job_open():
    facts = [_req(verify="manual", build_state="incomplete")]

    assert job_scope_complete(facts) is True


def test_unrecognized_tier_or_verify_never_holds_job_open():
    facts = [
        _req(scope=None, build_state="incomplete"),
        _req(verify=None, build_state="incomplete"),
        _req(scope="weird-tier", build_state="incomplete"),
    ]

    assert job_scope_complete(facts) is True


def test_empty_snapshot_is_vacuously_complete():
    assert job_scope_complete([]) is True


def test_non_dict_entries_are_ignored():
    facts = ["not-a-dict", _req(build_state="finished")]

    assert job_scope_complete(facts) is True


def test_in_job_scope_predicate_is_case_and_whitespace_tolerant():
    fact = {"meta": {"scope": " MVP ", "verify": " Automated "}}

    assert in_job_scope(fact) is True


def test_in_job_scope_predicate_false_for_missing_meta():
    assert in_job_scope({}) is False
