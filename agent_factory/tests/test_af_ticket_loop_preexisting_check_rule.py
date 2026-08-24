"""The ticket gate and post-merge verification must not contradict each other.

Verification now refuses to blame a ticket for a failure that reproduces at the pre-merge commit.
The TICKET gate had no such rule, so the two disagreed about the very same failure: verification
says "pre-existing, not this ticket's", while the ticket's own pinned check says "fix all 263 errors
or you may not finish". No ticket can satisfy both, and the only honest move left to a worker is to
block.

Observed 2026-08-24, sports_analysis T1b. Its worker ran every repo-wide gate and PASSED all of them
-- full suite 247 passed / 1 skipped, uv build, ruff, `make check`, `make check-real` 22 passed,
collection, footage, S3 -- except `uv run --with mypy mypy src tests`:

    Found 263 errors in 52 files (checked 148 source files) ... Failures span the vendored player
    tracker, court pipeline, runtime, promotion, model loader, and tests ... Repairing 263
    cross-cutting errors is large enough to require its own ticket and is not safely satisfiable
    from the single T1b contract-conversion context.

Its own slice typechecked clean. Round #1's verification of T1a had already measured the identical
263 on the FIRST PARENT, so they predate the plan entirely. That check is unsatisfiable by any
ticket, and every ticket in the plan would block on it in turn, forever -- the same defect the
`make check-factory`-over-25-pre-existing-failures blocker was, in a different project.

The rule is the same one verification uses, with the same guardrails: it applies only to a check
PROVEN red before the work began, both counts go on the record, and one new failure still fails.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _rule() -> str:
    src = SCRIPT.read_text()
    start = src.index("# ------------------------------------------- a pinned check that was ALREADY RED")
    sel = src[start : src.index("\n\n  round_prompt=", start)]
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write("set -u\n" + sel + '\nprintf %s "$PREEXISTING_RULE"\n')
        path = fh.name
    try:
        res = subprocess.run(["bash", path], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        return res.stdout
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def _prompt_line() -> str:
    lines = SCRIPT.read_text().splitlines()
    return next(line for line in lines if line.strip().startswith("round_prompt="))


# ------------------------------------------------------------------------------- the rule ------

def test_a_check_already_red_at_the_merge_base_is_measured_not_fixed():
    text = _rule()
    assert "ALREADY FAILING before your ticket existed" in text
    assert "FIRST MEASURE whether it was already red" in text
    assert "merge-base" in text


def test_the_pass_criterion_is_a_subset_of_what_was_already_broken():
    text = _rule()
    assert "subset of the failures already present at the merge-base" in text
    assert "that check PASSES for your ticket" in text


def test_one_new_failure_still_fails():
    """Without this the rule is a licence to wave through a regression."""
    text = _rule()
    assert "adds even ONE failure that the merge-base does not have" in text
    assert "the check FAILS and it is yours to fix" in text


def test_it_cannot_be_applied_to_a_check_that_was_green():
    """The single way this rule could hide a regression, named so a worker cannot reach it by
    accident."""
    text = _rule()
    assert "ONLY to a check you have PROVEN was red before you started" in text
    assert "a check that was green at the merge-base is a check you must pass outright" in text


def test_the_evidence_is_required_to_be_checkable():
    """A verdict nobody can re-derive is a claim, not evidence."""
    text = _rule()
    assert "both counts and the merge-base sha in your evidence" in text


def test_blocking_and_lying_are_both_named_as_wrong():
    text = _rule()
    assert "must not record it as passed" in text
    assert "blocking wastes the round" in text


# --------------------------------------------------------------------------- it reaches a worker

def test_the_rule_is_interpolated_into_the_round_prompt():
    line = _prompt_line()
    assert "$PREEXISTING_RULE" in line
    assert line.count("$PREEXISTING_RULE") == 1


def test_the_rule_is_defined_before_the_prompt_is_built():
    src = SCRIPT.read_text()
    assert src.index("PREEXISTING_RULE=") < src.index("  round_prompt=")


def test_the_rule_applies_at_every_round_width():
    """Unlike the sweep amendment, this one is not width-conditional: a repo-wide check carrying
    pre-existing debt is exactly as unsatisfiable on a 1-wide round as on a 16-wide one."""
    src = SCRIPT.read_text()
    rule_at = src.index("PREEXISTING_RULE=")
    # not inside the `if [ "$size" -gt 1 ]` selector
    sel_start = src.index("# WHO RUNS THE REPO-WIDE SWEEP")
    sel_end = src.index("\nfi\n", sel_start)
    assert not (sel_start < rule_at < sel_end), "the rule must not be width-conditional"
