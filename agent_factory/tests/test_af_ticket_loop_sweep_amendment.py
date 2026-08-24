"""The round contract must not forbid a worker the very commands its gate requires.

The round prompt carries a "deliberate amendment": workers skip the repo-wide suite, build,
typecheck and lint, and post-merge verification runs them once on the merged tree. The reason is
measured and good — N workers each running the full suite puts N concurrent suites on a box with a
handful of cores, and one worker burned 26 minutes and 259k tokens without producing a commit.

It was sent UNCONDITIONALLY, including to one-ticket rounds, where its own rationale interpolates to
"1 workers each running the full suite puts 1 concurrent suites on a box" — not a contention
argument, just the ordinary case. At width 1 it buys nothing.

And it is not free, because it CONTRADICTS THE COMPLETION GATE. A ticket's mandatory declared checks
are byte-exact commands and a plan may legitimately declare repo-wide ones. praxis R0b declares
`make check-engine`, `make check-ml-registry` and `make check-lint`; its worker was told to skip
exactly those, so `all_validations_passed` could never honestly go true. What happened (2026-08-24)
is the only correct move left to it:

    R0b implementation and sanctioned scoped gates are green ... Praxis all_validations_passed
    nevertheless remains false because mandatory declared checks require byte-exact repo-wide
    commands ... Those repo-wide commands were not run and were not falsely recorded as passed.

It blocked. R0b is the root of the dependency graph, so that stalled all 14 remaining tickets and
ended the run with an escalation for a human. The worker was right and the contract was
contradictory — which is the general rule these tests encode: a refusal is evidence about the
contract before it is evidence about the worker.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _selector() -> str:
    src = SCRIPT.read_text()
    start = src.index("# WHO RUNS THE REPO-WIDE SWEEP")
    return src[start : src.index("\nfi\n", start) + 4]


def _amendment_for(size: int) -> str:
    """The text the driver would actually send to a round of this width."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(f'set -u\nsize={size}\n{_selector()}\nprintf %s "$SWEEP_AMENDMENT"\n')
        path = fh.name
    try:
        res = subprocess.run(["bash", path], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        return res.stdout
    finally:
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------------------ the regression --

def test_a_one_ticket_round_is_told_to_run_the_repo_wide_gates_itself():
    """THE REGRESSION: at width 1 the worker must be allowed the commands its checks demand."""
    text = _amendment_for(1)
    assert "NO amendment" in text
    assert "Run the repo-wide gates yourself" in text
    assert "full test suite" in text
    assert "YOURS to run" in text


def test_a_one_ticket_round_is_not_told_to_skip_anything():
    text = _amendment_for(1)
    for forbidden in ("What a worker skips", "Deferring the SWEEP", "narrows WHICH tests run"):
        assert forbidden not in text, f"the deferral survived into a 1-wide round: {forbidden!r}"


def test_the_amendment_still_applies_where_it_was_measured():
    """The fix must not delete the amendment: at width 5 the contention it prevents is real."""
    text = _amendment_for(5)
    assert "ONE deliberate amendment" in text
    assert "5 workers each running the full suite" in text
    assert "The repo-wide gates are run ONCE, on the MERGED tree" in text


@pytest.mark.parametrize("size", [2, 8, 16])
def test_every_wide_round_keeps_the_deferral(size: int):
    assert "ONE deliberate amendment" in _amendment_for(size)


def test_the_wide_amendment_never_weakens_the_per_ticket_gate():
    """It narrows WHICH tests run; it must never read as permission to skip a ticket's own."""
    text = _amendment_for(5)
    assert "it does not remove the ticket's gate" in text
    assert "A worker whose own related tests are red is NOT finished" in text


def test_the_prompt_interpolates_the_selected_text():
    """A variable that never reaches the prompt would make all of the above decorative."""
    src = SCRIPT.read_text()
    prompt = next(line for line in src.splitlines() if line.strip().startswith("round_prompt="))
    assert "$SWEEP_AMENDMENT" in prompt
    # and exactly once, so a stale second copy cannot contradict the chosen one
    assert prompt.count("$SWEEP_AMENDMENT") == 1
    assert "ONE deliberate amendment" not in prompt, "the literal must live only in the selector"


def test_the_selector_runs_before_the_prompt_is_built():
    src = SCRIPT.read_text()
    assert src.index("# WHO RUNS THE REPO-WIDE SWEEP") < src.index("  round_prompt=")
