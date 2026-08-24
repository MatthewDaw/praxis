"""A round must not be killed by a string on a screen.

The build wait loop watches the tmux pane and ends the round when it sees an auth phrase. That is a
pixel match against arbitrary scrollback, and its own history is false positives: the "deliberately
NARROW" comment above the check was written after one killed a healthy session with the API
verified HTTP 200 at that exact second.

The strings are also ordinary CONTENT. `authentication_error` appears in this very driver, in every
provider SDK, and in any log a worker cats. A worker that greps for it puts it on screen with
nothing wrong at all.

Observed 2026-08-24: sports_analysis round #1 died here two minutes in, scored ZERO tickets, and
wrote exactly one line — "auth error, ending wait" — with no record of what it had matched. The
backend had preflighted a live generation ninety seconds earlier and the very next round ran fine,
so the round was almost certainly murdered by its own scrollback, and nothing in the log could ever
settle it.

Two fixes, and the second is only possible because preflight now knows how to ask: the evidence is
always recorded, and the guess is CHECKED against a real generation before it is allowed to end a
round.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _source() -> str:
    return SCRIPT.read_text()


def _wait_branch() -> str:
    """The auth arm of the build wait loop, as shipped."""
    text = _source()
    start = text.index('if af_ihas "$pane" "please run /login')
    return text[start : text.index("\n    fi\n", start) + 8]


def _run(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=120)


def _matchers() -> str:
    """The shipped one-line text matchers. Absent from a harness they are a silent 127, which reads
    as "no match" and inverts the very branch under test."""
    found = re.findall(r"^af_(?:i?has|hasf|hasx)\(\)\{.*$", _source(), re.M)
    assert len(found) == 4, found
    return "\n".join(found)


HARNESS = textwrap.dedent(
    """
    set -uo pipefail
    LOG=/dev/null
    say(){ echo "$*"; }
    BACKEND=codex
    """
) + _matchers() + "\n"


# ---------------------------------------------------------------------- the guess is now checked --

def test_a_healthy_backend_turns_the_match_into_a_false_positive():
    """THE REGRESSION: scrollback containing the phrase must not end the round."""
    program = (
        HARNESS
        + _function("af_say_pane_evidence")
        + "\naf_backend_is_live(){ return 0; }\n"
        + 'pane="$(printf \'%s\\n\' "worker: grep -rn authentication_error src/" "all tests pass")"\n'
        + "for _ in 1; do\n"
        + _wait_branch()
        + '\n  echo "STILL-WAITING"\ndone\n'
    )
    res = _run(program)
    assert "credential is HEALTHY" in res.stdout, res.stdout + res.stderr
    assert "ending wait" not in res.stdout
    assert "STILL-WAITING" not in res.stdout, "a false positive must `continue`, not fall through"


def test_a_dead_backend_still_ends_the_round_with_a_diagnosis():
    program = (
        HARNESS
        + _function("af_say_pane_evidence")
        + "\naf_backend_is_live(){ AF_PROBE_KIND=rejected; return 1; }\n"
        + 'pane="please run /login"\n'
        + "for _ in 1; do\n"
        + _wait_branch()
        + '\n  echo "STILL-WAITING"\ndone\n'
    )
    res = _run(program)
    assert "auth error CONFIRMED by a live probe (rejected)" in res.stdout, res.stdout + res.stderr
    assert "STILL-WAITING" not in res.stdout


def test_an_exhausted_backend_is_reported_as_what_it_is():
    """quota/credit are not "auth" — the operator needs the distinction to know what to do."""
    program = (
        HARNESS
        + _function("af_say_pane_evidence")
        + "\naf_backend_is_live(){ AF_PROBE_KIND=quota; return 1; }\n"
        + 'pane="invalid api key"\n'
        + "for _ in 1; do\n" + _wait_branch() + "\ndone\n"
    )
    res = _run(program)
    assert "CONFIRMED by a live probe (quota)" in res.stdout


# ------------------------------------------------------------------------ the evidence is kept ----

def test_the_matched_line_and_the_pane_are_recorded():
    """One line saying "auth error" is not a record. The next occurrence has to be diagnosable."""
    program = (
        HARNESS
        + _function("af_say_pane_evidence")
        + "\naf_backend_is_live(){ AF_PROBE_KIND=rejected; return 1; }\n"
        + 'pane="$(printf \'%s\\n\' "line one" "boom: authentication_error here" "line three")"\n'
        + "for _ in 1; do\n" + _wait_branch() + "\ndone\n"
    )
    res = _run(program)
    assert "boom: authentication_error here" in res.stdout, "the MATCHING line must be quoted"
    assert "line three" in res.stdout, "and the surrounding pane, for context"
    assert "--- last build pane ---" in res.stdout


def test_the_context_exhausted_branch_records_evidence_too():
    text = _source()
    start = text.index('if af_has "$pane" "100% context used"')
    branch = text[start : text.index("\n    fi\n", start)]
    assert "af_say_pane_evidence" in branch, (
        "the other silent round-killer must say what it saw as well"
    )


# ----------------------------------------------------------------------------- the prober seam ----

def test_the_probe_command_is_captured_rather_than_duplicated():
    """Four backends build four probe commands. A fifth copy for the mid-run re-probe is a copy
    that drifts — and a re-probe of a DIFFERENT credential than the session spends is theatre."""
    src = _source()
    assert 'AF_PROBE_CMD="$cmd"' in src, "recorded where the command is already in hand"
    assert src.count("AF_PROBE_CMD=") == 2, "one declaration and one capture — no hand-written copies"


def test_re_probing_without_a_recorded_command_never_manufactures_a_verdict():
    """With nothing to probe, the honest answer is 'no evidence of failure', not 'it is dead'."""
    program = HARNESS + _function("af_backend_is_live") + '\nAF_PROBE_CMD=""\n' \
        'if af_backend_is_live; then echo LIVE; else echo DEAD; fi\n'
    res = _run(program)
    assert res.stdout.strip() == "LIVE", res.stdout + res.stderr


def test_the_mid_run_probe_does_not_sleep_through_its_backoff():
    """It runs inside the 30s wait loop; the preflight's retry pause would stall the poll."""
    src = _source()
    fn = _function("af_backend_is_live")
    assert "AF_PROBE_RETRY_S=0" in fn
