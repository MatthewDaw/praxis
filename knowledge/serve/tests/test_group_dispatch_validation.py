"""Acceptance test for ticket 92e23ab1 (group dispatch validation, R48/R50-adjacent):

given a group dispatch whose two members name the same snapshot, dispatch is
refused naming that snapshot; given an attempt to change membership after
dispatch, it is refused; given members with differing origins, dispatch is
refused. Group membership is fixed at dispatch by the authenticated
dispatching principal and immutable afterward — it cannot be altered by a
build session or the control surface, and all members must share the same
origin and org.
"""

from __future__ import annotations

import dataclasses

import pytest

from knowledge.serve.dispatch import DispatchError, DispatchPayload
from knowledge.serve.group_dispatch import (
    JobGroup,
    attempt_change_group_membership,
    dispatch_group,
    validate_group_dispatch,
)

ORG = "acme-org"
ORIGIN = "git@github.com:acme/widgets.git"
PRINCIPAL = "dispatching-session-1"


def make_payload(snapshot: str, *, origin_url: str = ORIGIN, org: str = ORG) -> DispatchPayload:
    return DispatchPayload(
        project="af-build-remote-jobs",
        snapshot=snapshot,
        origin_url=origin_url,
        build_base_sha="a" * 40,
        pr_base="main",
        org=org,
    )


def test_dispatch_refuses_a_group_whose_two_members_name_the_same_snapshot():
    members = [make_payload("prd-a"), make_payload("prd-b"), make_payload("prd-a")]

    with pytest.raises(DispatchError, match="prd-a"):
        dispatch_group(members, dispatching_principal=PRINCIPAL)


def test_dispatch_refuses_a_group_with_differing_origins():
    members = [
        make_payload("prd-a", origin_url=ORIGIN),
        make_payload("prd-b", origin_url="git@github.com:acme/other.git"),
    ]

    with pytest.raises(DispatchError):
        dispatch_group(members, dispatching_principal=PRINCIPAL)


def test_dispatch_refuses_a_group_with_differing_orgs():
    members = [
        make_payload("prd-a", org=ORG),
        make_payload("prd-b", org="other-org"),
    ]

    with pytest.raises(DispatchError):
        dispatch_group(members, dispatching_principal=PRINCIPAL)


def test_successful_group_dispatch_fixes_membership_at_dispatch():
    members = [make_payload("prd-a"), make_payload("prd-b")]

    group = dispatch_group(members, dispatching_principal=PRINCIPAL)

    assert isinstance(group, JobGroup)
    assert group.member_snapshots == ("prd-a", "prd-b")
    assert group.dispatching_principal == PRINCIPAL
    assert group.group_id


def test_an_attempt_to_change_membership_after_dispatch_is_refused():
    members = [make_payload("prd-a"), make_payload("prd-b")]
    group = dispatch_group(members, dispatching_principal=PRINCIPAL)

    with pytest.raises(DispatchError):
        attempt_change_group_membership(group, ("prd-a", "prd-c"))

    # refusing left the original group completely untouched
    assert group.member_snapshots == ("prd-a", "prd-b")


def test_group_membership_is_structurally_frozen():
    members = [make_payload("prd-a"), make_payload("prd-b")]
    group = dispatch_group(members, dispatching_principal=PRINCIPAL)

    with pytest.raises(dataclasses.FrozenInstanceError):
        group.member_snapshots = ("prd-x",)  # type: ignore[misc]


def test_dispatch_group_requires_a_dispatching_principal():
    members = [make_payload("prd-a"), make_payload("prd-b")]

    with pytest.raises(DispatchError):
        dispatch_group(members, dispatching_principal="")


def test_validate_group_dispatch_requires_at_least_two_members():
    with pytest.raises(DispatchError):
        validate_group_dispatch([make_payload("prd-a")])
