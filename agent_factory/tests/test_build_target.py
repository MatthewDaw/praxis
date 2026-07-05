from agent_factory.build_target import (
    BuildTarget,
    Requirement,
    requirement_from_fact,
    select_build_target,
)


def _fact(req_id: str, scope: str | None, verify: str | None) -> dict:
    """A realistic raw Praxis requirement fact (the shape the plan emits)."""
    meta: dict = {"requirement_id": req_id, "surfaces": [], "acceptance": ""}
    if scope is not None:
        meta["scope"] = scope
    if verify is not None:
        meta["verify"] = verify
    return {"category": "requirement", "meta": meta}


def test_mvp_automated_goes_to_build():
    target = select_build_target([Requirement("R13", "mvp", "automated")])
    assert [r.id for r in target.build] == ["R13"]
    assert target.deferred_manual == []
    assert target.excluded_post_mvp == []
    assert target.needs_triage == []


def test_mvp_manual_goes_to_deferred_manual():
    target = select_build_target([Requirement("R5", "mvp", "manual")])
    assert [r.id for r in target.deferred_manual] == ["R5"]
    assert target.build == []


def test_post_mvp_is_excluded_regardless_of_verify():
    target = select_build_target(
        [
            Requirement("R47", "post-mvp", "manual"),
            Requirement("R48", "post-mvp", "automated"),
        ]
    )
    assert [r.id for r in target.excluded_post_mvp] == ["R47", "R48"]
    assert target.build == []
    assert target.deferred_manual == []


def test_missing_tier_routes_to_triage_not_build():
    target = select_build_target([Requirement("R1", None, "automated")])
    assert [r.id for r in target.needs_triage] == ["R1"]
    assert target.build == []


def test_missing_verify_routes_to_triage_not_build():
    target = select_build_target([Requirement("R2", "mvp", None)])
    assert [r.id for r in target.needs_triage] == ["R2"]
    assert target.build == []


def test_unknown_tag_values_route_to_triage_not_build():
    target = select_build_target(
        [
            Requirement("R3", "mvp", "human"),  # unrecognized verify
            Requirement("R4", "milestone-2", "automated"),  # unrecognized tier
        ]
    )
    assert {r.id for r in target.needs_triage} == {"R3", "R4"}
    assert target.build == []


def test_mixed_realistic_list_partitions_correctly():
    requirements = [
        _fact("R13", "mvp", "automated"),       # -> build
        _fact("R20", "mvp", "manual"),          # -> deferred_manual
        _fact("R47", "post-mvp", "manual"),     # -> excluded_post_mvp
        _fact("R50", "mvp", None),              # -> needs_triage (missing verify)
    ]
    target = select_build_target(requirements)
    assert [r.id for r in target.build] == ["R13"]
    assert [r.id for r in target.deferred_manual] == ["R20"]
    assert [r.id for r in target.excluded_post_mvp] == ["R47"]
    assert [r.id for r in target.needs_triage] == ["R50"]


def test_empty_input_yields_empty_groups():
    target = select_build_target([])
    assert target == BuildTarget()
    assert target.build == []
    assert target.deferred_manual == []
    assert target.excluded_post_mvp == []
    assert target.needs_triage == []


def test_requirement_from_fact_pulls_tier_and_verify_from_meta():
    req = requirement_from_fact(_fact("R13", "mvp", "automated"))
    assert req == Requirement(id="R13", tier="mvp", verify="automated")


def test_requirement_from_fact_tolerates_missing_meta():
    req = requirement_from_fact({"category": "requirement", "id": "X1"})
    assert req == Requirement(id="X1", tier=None, verify=None)


def test_tag_values_are_normalized():
    # Case/whitespace drift in the tags must not knock a valid requirement out of build.
    target = select_build_target([_fact("R9", " MVP ", "Automated")])
    assert [r.id for r in target.build] == ["R9"]
