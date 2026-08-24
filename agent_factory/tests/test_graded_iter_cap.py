"""The graded iteration cap is a BACKSTOP, and it was set below what working tickets need.

`verify_graded_check` has three escalation rules. Two are sharp and do the real work:

  * advise-only defects escalate IMMEDIATELY as unremediable — there is no enforce-tier lever the
    worker could pull, so re-grading cannot flip the verdict;
  * a round that fails to REDUCE the defect count blocks after ONE non-improving iteration.

The third is the raw iteration cap. Because the first two already catch spinning and futility, the
cap only ever bites work that is steadily improving — so setting it below what improving work
actually needs turns a safety net into the main cause of blocked tickets.

Measured 2026-08-24 across every graded ticket in both builds:

    finished: 1, 1, 1, 2, 2, 2, 2, 3, 4, 5     R1a took 4, T8 took 5
    blocked:  3, 3                             R3b (ONE defect left), T6a

R1a and T8 exceeded the cap of 3 and finished anyway, because `should_block` is a returned value
with no enforcement and their workers pushed past it. R3b and T6a honoured it and blocked. Whether
a ticket shipped therefore came down to whether its worker obeyed a suggestion. R3b is the ticket
that lands the campaign terminal outcomes, and it stopped with acceptance and every executable gate
GREEN and a single advisory defect outstanding.
"""

from __future__ import annotations

import importlib
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "hooks"))
import _graded_verify as gv  # noqa: E402


def test_the_cap_clears_the_observed_ceiling():
    """5 is the most any FINISHED ticket needed. A cap at or below that blocks working tickets."""
    assert gv.DEFAULT_GRADED_ITER_CAP > 5, (
        "tickets have finished at 4 and 5 iterations; a cap of "
        f"{gv.DEFAULT_GRADED_ITER_CAP} blocks work that was converging"
    )


def test_the_cap_is_still_finite():
    """It is a backstop, not a licence to grade forever."""
    assert gv.DEFAULT_GRADED_ITER_CAP <= 20


def test_the_cap_is_overridable_without_editing_code():
    """So a project that genuinely needs a different bound does not have to patch the hook."""
    import os

    prev = os.environ.get("AF_GRADED_ITER_CAP")
    os.environ["AF_GRADED_ITER_CAP"] = "11"
    try:
        importlib.reload(gv)
        assert gv.DEFAULT_GRADED_ITER_CAP == 11
    finally:
        if prev is None:
            os.environ.pop("AF_GRADED_ITER_CAP", None)
        else:
            os.environ["AF_GRADED_ITER_CAP"] = prev
        importlib.reload(gv)


# ------------------------------------------------- the guards the cap is allowed to rely on ----

def test_non_convergence_still_blocks_after_one_bad_iteration():
    """This is what actually stops a spin, and raising the cap must not touch it: a check whose
    defect count does not FALL is escalated immediately, however much headroom remains."""
    src = (gv.__file__ and open(gv.__file__).read()) or ""
    assert "not converging" in src
    assert "last_defects is not None and last_defects > 0 and n_defects >= last_defects" in src


def test_advise_only_defects_still_escalate_immediately():
    """The other sharp guard: feedback that cannot flip the verdict must not burn the cap. Note
    this is a SEPARATE exit — raising the cap does nothing for a ticket blocked here, which is why
    sports_analysis T6a stays blocked and needs its own decision."""
    src = open(gv.__file__).read()
    assert "unremediable, no enforce-tier defect to fix" in src
    i_advise = src.index("all(d.tier == DEFECT_TIER_ADVISE")
    i_cap = src.index("if iters >= cap")
    assert i_advise < i_cap, "the unremediable check must precede the cap, or advice burns the cap"


def test_an_unchanged_diff_still_escalates_within_the_cap():
    """R33: re-grading a byte-identical diff must count toward escalation, or a worker could
    resubmit the same code forever without ever advancing `iters`."""
    src = open(gv.__file__).read()
    assert "cache_repeats" in src
    assert "if repeats >= cap" in src
