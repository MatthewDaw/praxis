"""Pure unit tests for the S3 net-lines computation (R29): each bucket's net is that
bucket's additions minus its deletions, at matching index -- never a whole-window
average or a cross-bucket total. No DB/GitHub dependency: ``net_lines`` is a pure
function, so these run unconditionally (unlike the DB-backed route/integration tests).
"""

from __future__ import annotations

from knowledge.serve.productivity_attribution import net_lines


def test_net_lines_subtracts_per_bucket_not_in_aggregate():
    # Bucket 1 is net-positive, bucket 2 is exactly even, bucket 3 is net-NEGATIVE
    # (a heavy-deletion/refactor bucket) -- each must resolve independently.
    additions = [12, 5, 3]
    deletions = [4, 5, 10]
    assert net_lines(additions, deletions) == [8, 0, -7]


def test_net_lines_never_clips_a_negative_bucket_to_zero():
    """A bucket sums incorrectly if a refactor-heavy bucket gets floored at 0 instead of
    surfacing as negative -- this is exactly the failure this suite must catch."""
    assert net_lines([0], [15]) == [-15]


def test_net_lines_all_zero_when_no_activity():
    assert net_lines([0, 0, 0], [0, 0, 0]) == [0, 0, 0]


def test_net_lines_empty_buckets():
    assert net_lines([], []) == []


def test_net_lines_preserves_bucket_order_and_count():
    additions = [1, 2, 3, 4]
    deletions = [1, 1, 1, 1]
    result = net_lines(additions, deletions)
    assert len(result) == len(additions)
    assert result == [0, 1, 2, 3]
