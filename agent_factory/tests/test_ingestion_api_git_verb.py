"""git is admitted to run bodies ONLY as a read-only ``git diff`` -- the one way to write a check whose
whole logic is its own command, which a worker cannot defeat by editing project code."""

from __future__ import annotations

import pytest

from agent_factory.ingestion_api import RunBodyRejected, _validate_run_body

ACCEPTED = [
    "git diff --quiet 7ae4c3a1 HEAD -- . :^mvpvue",
    "git diff --name-only HEAD",
    "git diff --exit-code --stat main HEAD -- src",
]

REJECTED = [
    "git push origin main",
    "git checkout -- .",
    "git -C .. diff --quiet",
    "git -c core.pager=cat diff",
    "git diff --output=report.txt HEAD",
    "git diff --ext-diff HEAD",
    "git diff --textconv HEAD",
    "git diff --no-index a b",
    "git diff --quiet HEAD -- /etc/passwd",
]


@pytest.mark.parametrize("body", ACCEPTED)
def test_read_only_git_diff_is_accepted(body: str) -> None:
    assert _validate_run_body(body, channel="human")


@pytest.mark.parametrize("body", REJECTED)
def test_every_other_git_shape_is_refused(body: str) -> None:
    with pytest.raises(RunBodyRejected):
        _validate_run_body(body, channel="human")
