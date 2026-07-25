"""Unit tests for commit-to-owner attribution on the productivity series (R11).

Covers the ticket's acceptance condition: given a window containing one commit by the owner
and one by another author, S1 reflects only the owner's additions and unattributed_count is 0,
while a commit with an unlinked email increments unattributed_count.
"""

from __future__ import annotations

from knowledge.serve.productivity_attribution import attribute_commit_activity


def test_owner_and_other_author_commits_s1_is_owner_only_and_unattributed_is_zero():
    commit_activity = {
        "acme/repo": [
            {"additions": 10, "author_login": "mattdaw7"},
            {"additions": 7, "author_login": "someone-else"},
        ]
    }

    result = attribute_commit_activity(commit_activity, owner_login="mattdaw7")

    assert result == {"s1": 10, "unattributed_count": 0}


def test_commit_with_unlinked_email_increments_unattributed_count():
    commit_activity = {
        "acme/repo": [
            {"additions": 10, "author_login": "mattdaw7"},
            {"additions": 7, "author_login": "someone-else"},
            {"additions": 4, "author_login": None, "author_email": "nobody@example.com"},
        ]
    }

    result = attribute_commit_activity(
        commit_activity, owner_login="mattdaw7", owner_emails=["mattdaw7@gmail.com"]
    )

    assert result == {"s1": 10, "unattributed_count": 1}


def test_unlinked_login_but_verified_owner_email_counts_toward_s1():
    commit_activity = {
        "acme/repo": [
            {"additions": 6, "author_login": None, "author_email": "MattDaw7@Gmail.com"},
        ]
    }

    result = attribute_commit_activity(
        commit_activity, owner_login="mattdaw7", owner_emails=["mattdaw7@gmail.com"]
    )

    assert result == {"s1": 6, "unattributed_count": 0}


def test_no_commits_is_zero_s1_and_zero_unattributed():
    result = attribute_commit_activity({}, owner_login="mattdaw7")

    assert result == {"s1": 0, "unattributed_count": 0}
