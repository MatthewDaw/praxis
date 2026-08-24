"""Baseline replicates whose measured rope is an EXACT number (R3a).

R3a retired the threshold a model stored at registration: the bar is now recomputed from
the model's own ``baseline_runs`` rows at every comparison. So a fixture that used to write
the threshold in one field and be done has to supply four ledger rows whose sample stdev IS
the bar it wants -- and every suite that adjudicates needs the same four rows.

Three equal values plus one differing by ``d`` have a sample stdev of exactly ``d/2``: for
``[a, a, a, a+d]`` the mean is ``a + d/4``, the squared deviations sum to ``3d^2/4``, and
dividing by ``n-1 = 3`` leaves ``d^2/4``. Choosing ``d = 2 * rope`` therefore lands the rope
bit-for-bit rather than a hair off it, which is what lets a boundary test assert a delta of
EXACTLY one rope.

The arithmetic lives here rather than in each suite because it is one rule, and a suite that
re-derives it is a suite that can get it subtly wrong while still looking right.
"""

from __future__ import annotations

from collections.abc import Sequence

from knowledge.ml_registry.verdict import LedgerRow

#: The commits the replicate rows are keyed by, unless a caller names its own.
ROPE_COMMITS: tuple[str, ...] = ("b1", "b2", "b3", "b4")


def rope_replicates(rope: float, *, at: float = 0.0) -> tuple[float, ...]:
    """Four values centred on ``at`` whose SAMPLE stdev is exactly ``rope``."""
    return (at, at, at, at + 2 * rope)


def rope_ledger(
    rope: float, *, at: float = 0.0, commits: Sequence[str] = ROPE_COMMITS
) -> dict[str, float]:
    """commit -> metric value for those replicates, as a plain ledger-values mapping."""
    return dict(zip(commits, rope_replicates(rope, at=at)))


def rope_ledger_rows(
    rope: float,
    *,
    at: float = 0.0,
    throughput: float,
    commits: Sequence[str] = ROPE_COMMITS,
) -> dict[str, LedgerRow]:
    """commit -> :class:`~knowledge.ml_registry.verdict.LedgerRow` for those replicates.

    ``diff_lines`` is 0: a baseline row is not an arm, so it never has a diff to bound.
    """
    return {
        commit: LedgerRow(value=value, throughput=throughput, diff_lines=0)
        for commit, value in rope_ledger(rope, at=at, commits=commits).items()
    }
