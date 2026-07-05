from agent_factory.validation_target import (
    FAILED,
    PASSED,
    UNRUN,
    ReqRef,
    ValidationCheck,
    checks_from_facts,
    resolve_bindings,
    select_validation_incomplete,
    unbound_checks,
)


def _chk(cid, applies_to, last_result=UNRUN):
    return ValidationCheck(id=cid, applies_to=applies_to, last_result=last_result)


# --- binding -------------------------------------------------------------------


def test_binds_by_exact_requirement_id():
    states = resolve_bindings([_chk("c1", "R7")], [ReqRef("R7"), ReqRef("R8")])
    assert set(states) == {"R7"}
    assert [c.id for c in states["R7"].checks] == ["c1"]


def test_binds_by_class_tag_to_all_matching_requirements():
    checks = [_chk("auth-live", "auth")]
    reqs = [ReqRef("R1", ("auth",)), ReqRef("R2", ("auth", "mvp")), ReqRef("R9", ("messaging",))]
    states = resolve_bindings(checks, reqs)
    assert set(states) == {"R1", "R2"}  # both auth reqs, not the messaging one


def test_unbound_check_is_surfaced():
    checks = [_chk("ghost", "nonexistent-class")]
    assert [c.id for c in unbound_checks(checks, [ReqRef("R1", ("auth",))])] == ["ghost"]


# --- completeness / the regress trigger ----------------------------------------


def test_requirement_complete_only_when_all_bound_checks_passed():
    states = resolve_bindings(
        [_chk("c1", "R1", PASSED), _chk("c2", "R1", PASSED)], [ReqRef("R1")]
    )
    assert states["R1"].complete
    assert select_validation_incomplete(states) == []


def test_a_freshly_added_unrun_check_regresses_the_requirement():
    # THE headline behaviour: dropping a new (unrun) validation onto a requirement makes it
    # validation-incomplete immediately -> it must re-enter the build set.
    states = resolve_bindings([_chk("auth-live", "auth", UNRUN)], [ReqRef("R7", ("auth",))])
    assert not states["R7"].complete
    assert select_validation_incomplete(states) == ["R7"]
    assert [c.id for c in states["R7"].unsatisfied] == ["auth-live"]


def test_a_failing_check_keeps_the_requirement_incomplete():
    states = resolve_bindings([_chk("c1", "R1", FAILED)], [ReqRef("R1")])
    assert select_validation_incomplete(states) == ["R1"]


def test_requirement_with_no_checks_is_absent_from_states():
    states = resolve_bindings([_chk("c1", "R1")], [ReqRef("R1"), ReqRef("R2")])
    assert "R2" not in states  # untouched requirements aren't dragged into validation


# --- checks_from_facts (the bridge from Praxis-stored checks) -------------------


def test_checks_from_facts_builds_from_praxis_facts():
    facts = [{
        "id": "fact1",
        "text": "login works against the live service",
        "meta": {"check_id": "auth-login-live", "applies_to": "auth",
                 "run": "npx playwright test tests/auth/login.spec.ts"},
    }]
    checks = checks_from_facts(facts)
    assert len(checks) == 1
    c = checks[0]
    assert c.id == "auth-login-live" and c.applies_to == "auth"
    assert c.criterion == "login works against the live service"  # from meta.criterion or text
    assert c.run.startswith("npx playwright")
    assert c.last_result == UNRUN  # default -> a fresh check regresses its ticket


def test_checks_from_facts_criterion_prefers_meta_then_text():
    facts = [{"id": "f", "text": "fallback", "meta": {"applies_to": "R1", "criterion": "explicit"}}]
    assert checks_from_facts(facts)[0].criterion == "explicit"


def test_checks_from_facts_skips_facts_without_applies_to():
    facts = [
        {"id": "a", "meta": {"applies_to": "auth"}},
        {"id": "b", "meta": {}},   # no applies_to -> binds to nothing
        {"id": "c"},               # no meta at all
    ]
    assert [c.applies_to for c in checks_from_facts(facts)] == ["auth"]


def test_checks_from_facts_normalizes_bad_last_result():
    facts = [{"id": "a", "meta": {"applies_to": "R1", "last_result": "bogus"}}]
    assert checks_from_facts(facts)[0].last_result == UNRUN
