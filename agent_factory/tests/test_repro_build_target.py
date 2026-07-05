"""Reproduction: the forced build-completeness gate's UNFILTERED completion target is
unsatisfiable autonomously, and ``select_build_target`` is the fix that makes it satisfiable.

Context (CONSTITUTION / af-build): the autonomous build runs under a forced
completeness gate that keeps working until ``incompleteCount`` reaches 0 — i.e. until every
requirement in its completion target has earned a "succeeded" outcome. The Praxis query
``incomplete_requirements(project)`` returns ALL active requirements, so if the gate uses
that set directly as its target it inherits two structural defects:

  (a) post-mvp requirements (``meta.scope == "post-mvp"``) keep the gate chasing scope
      forever, and
  (b) manual-verify requirements (``meta.verify == "manual"``) can NEVER earn an automated
      "succeeded" outcome — so they can never leave the incomplete set.

Either defect alone means ``incompleteCount`` never reaches 0 and the build NEVER finishes.

These tests model the gate as a tiny solver and prove:

  - TRAP: with the unfiltered "all active requirements" target, there EXISTS a requirement
    that can never leave the incomplete set, so the gate is unsatisfiable autonomously.
  - FIX: ``select_build_target`` yields a build set of ONLY mvp+automated requirements
    (post-mvp excluded, manual deferred), every member of which can earn an automated
    success — so the gate's target is satisfiable and the build terminates.
"""

from agent_factory.build_target import Requirement, select_build_target


def _fact(req_id: str, scope: str | None, verify: str | None) -> dict:
    """A realistic raw Praxis requirement fact (the shape ``incomplete_requirements`` returns)."""
    meta: dict = {"requirement_id": req_id, "surfaces": [], "acceptance": ""}
    if scope is not None:
        meta["scope"] = scope
    if verify is not None:
        meta["verify"] = verify
    return {"category": "requirement", "meta": meta}


# --- The realistic mixed plan the gate is handed -------------------------------------------
# R13: a normal in-scope, automatically-verifiable requirement (the only kind the gate can finish)
# R20: in-scope for the MVP but human-verified — no automated success signal exists
# R47: out-of-scope post-mvp work — chasing it means the gate never "finishes" the project
_PLAN = [
    _fact("R13", "mvp", "automated"),
    _fact("R20", "mvp", "manual"),
    _fact("R47", "post-mvp", "manual"),
]


def _can_ever_auto_succeed(req: Requirement) -> bool:
    """Can the autonomous gate ever drive this requirement to a "succeeded" outcome?

    Models the two hard limits of an automated forced-build loop:
      - a manual-verify requirement has no automated success signal -> never succeeds, and
      - a post-mvp requirement is outside the build's scope -> the gate is never tasked to
        finish it, so it is never driven to success either.
    Only an mvp + automated requirement can ever be completed autonomously.
    """
    return req.tier == "mvp" and req.verify == "automated"


def _gate_terminates(completion_target: list[Requirement]) -> bool:
    """The forced gate finishes IFF every requirement in its target can reach incomplete=0.

    The loop runs until ``incompleteCount == 0``; a requirement that can never auto-succeed
    stays in the incomplete set forever. So the gate terminates iff EVERY target member can
    eventually auto-succeed.
    """
    return all(_can_ever_auto_succeed(req) for req in completion_target)


def test_trap_unfiltered_target_is_unsatisfiable_autonomously():
    """(a) TRAP: the raw ``incomplete_requirements`` target can never reach incompleteCount=0.

    The unfiltered completion target is simply ALL active requirements — the gate does no
    partitioning. We show it contains post-mvp and/or manual members, and that at least one
    of them can NEVER leave the incomplete set, so the gate is structurally unsatisfiable.
    """
    # What incomplete_requirements(project) would return: every active requirement, untouched.
    unfiltered_target = [
        Requirement(
            id=f["meta"]["requirement_id"],
            tier=f["meta"].get("scope"),
            verify=f["meta"].get("verify"),
        )
        for f in _PLAN
    ]

    # The target is polluted with exactly the two kinds the gate cannot finish.
    assert any(r.tier == "post-mvp" for r in unfiltered_target), "expected a post-mvp item"
    assert any(r.verify == "manual" for r in unfiltered_target), "expected a manual item"

    # There EXISTS a requirement that can never leave the incomplete set...
    never_completable = [r for r in unfiltered_target if not _can_ever_auto_succeed(r)]
    assert {r.id for r in never_completable} == {"R20", "R47"}

    # ...therefore incompleteCount never reaches 0 and the forced gate NEVER finishes.
    assert _gate_terminates(unfiltered_target) is False


def test_fix_selected_build_target_is_satisfiable_and_excludes_never_completable():
    """(b) FIX: ``select_build_target`` makes the gate's target satisfiable and terminating."""
    target = select_build_target(_PLAN)

    # The build set is ONLY mvp + automated: post-mvp excluded, manual deferred.
    assert [r.id for r in target.build] == ["R13"]
    assert [r.id for r in target.deferred_manual] == ["R20"]
    assert [r.id for r in target.excluded_post_mvp] == ["R47"]
    assert target.needs_triage == []

    # Partition is disjoint and total (every requirement accounted for exactly once).
    buckets = (
        target.build
        + target.deferred_manual
        + target.excluded_post_mvp
        + target.needs_triage
    )
    assert {r.id for r in buckets} == {"R13", "R20", "R47"}
    assert len(buckets) == 3

    # Every never-completable item (manual/post-mvp) is excluded from the build set...
    build_ids = {r.id for r in target.build}
    assert "R20" not in build_ids  # manual -> deferred, not built
    assert "R47" not in build_ids  # post-mvp -> excluded, not built
    assert all(_can_ever_auto_succeed(r) for r in target.build)

    # ...so the gate's completion target is now satisfiable and the build TERMINATES.
    assert _gate_terminates(target.build) is True
