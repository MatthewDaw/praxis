"""A truncating pipeline under `set -euo pipefail` is a silent run-killer.

`head -N` exits after N lines and SIGPIPEs whatever is writing to it. The driver runs
`set -euo pipefail`, so the pipeline inherits that 141 and `set -e` terminates the WHOLE LOOP --
with no error line, because the thing that failed was a diagnostic print.

It happened. 2026-08-24, praxis: integrate_round warns about stranded commits and lists them with

    git log --format='    %h %s' "HEAD..$br" 2>/dev/null | head -5 | tee -a "$LOG"

At 08:31 there were FOUR stranded commits, head never truncated, nothing was SIGPIPE'd, and the
round completed normally. At 08:57 there were NINE. head closed the pipe on the fifth, git log died
141, and the loop terminated in the middle of its merge stage. The log shows the five subject lines,
then nothing -- no "integrated", no verification, no exit reason -- just the EXIT trap's cleanup,
which reads exactly like a healthy run that ended. This file's own comments describe that failure
mode ("the run just stops mid-round, looking from the outside exactly like a healthy loop that went
quiet"); this was another instance of it, hiding in a `say`.

The dormancy is the point: a landmine keyed to how much output there happens to be will pass every
test and every rehearsal until the day the input grows.

`sed -n '1,Np'` truncates the same way and reads its input to the end, so there is nothing to
SIGPIPE.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _run(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)


def test_the_hazard_is_real_under_the_options_the_driver_uses():
    """Establish the mechanism itself, so the rest of this file is not arguing from theory.

    The producer has to be one that is STILL WRITING when head exits. `seq 1 100` is not: a hundred
    short lines land in the 64KB pipe buffer and seq is gone before head closes anything, so that
    version of this demonstration passes and proves nothing. It was written that way first.
    """
    prog = textwrap.dedent(
        """
        set -euo pipefail
        seq 1 200000 | head -5 >/dev/null
        echo SURVIVED
        """
    )
    res = _run(prog)
    assert "SURVIVED" not in res.stdout, (
        "if this ever passes, bash stopped propagating SIGPIPE through pipefail and these tests "
        "are measuring the wrong thing"
    )
    assert res.returncode == 141, res.returncode


def test_the_old_shape_is_not_reliably_safe_and_the_new_one_is():
    """Whether a SMALL producer dies is a RACE, and that is the honest characterisation.

    An earlier version of this file asserted that nine commits deterministically kill the pipeline,
    on the strength of one manual reproduction that did. It does not: with nine short subjects and
    two pipeline stages it usually survives, because git finishes writing before head stops reading.
    Adding a third stage (`| tee`) and longer subjects flips it. That is a race, and a race is
    exactly why the bug slept through months of four-commit rounds and then killed a run on a nine.

    So this asserts the thing that is actually true and actually matters: the old shape can fail and
    the replacement cannot. The producer here is large enough to make the old shape's failure
    reliable; the point is that the new shape is safe at EVERY size.
    """
    old = _run("set -euo pipefail; seq 1 200000 | head -5 >/dev/null; echo SURVIVED")
    assert "SURVIVED" not in old.stdout, "the mechanism no longer reproduces at all"

    for producer in ("seq 1 9", "seq 1 200000", "seq 1 1000000"):
        res = _run(f"set -euo pipefail; {producer} | sed -n '1,5p' >/dev/null; echo SURVIVED")
        assert "SURVIVED" in res.stdout, f"the replacement failed for {producer}: {res.stderr}"


# --------------------------------------------------------------------------------- the driver ----

def _stranding_lines() -> str:
    src = SCRIPT.read_text()
    i = src.index('say "  subjects seen:"')
    return src[i : src.index("\n      continue\n", i)]


def test_the_stranding_report_cannot_kill_the_run(tmp_path: Path):
    """THE REGRESSION, driven through a real git repo with more commits than the report shows."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["bash", "-c",
                    "git init -q && git config user.email t@t && git config user.name t"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["bash", "-c", "echo base > a.txt && git add -A && git commit -qm base"],
                   cwd=repo, check=True, capture_output=True)
    subprocess.run(["bash", "-c", "git checkout -q -b other"], cwd=repo, check=True,
                   capture_output=True)
    for i in range(9):  # more than the five the report prints -- four never truncated, nine did
        subprocess.run(["bash", "-c", f"echo {i} >> a.txt && git add -A && git commit -qm 'commit {i}'"],
                       cwd=repo, check=True, capture_output=True)
    subprocess.run(["bash", "-c", "git checkout -q -"], cwd=repo, check=True, capture_output=True)

    program = (
        textwrap.dedent(
            f"""
            set -euo pipefail
            cd {repo}
            LOG=/dev/null
            say(){{ echo "$*"; }}
            br=other
            known="R0a"
            """
        )
        + _stranding_lines()
        + '\necho "REACHED-THE-NEXT-STAGE"\n'
    )

    res = _run(program)

    assert "REACHED-THE-NEXT-STAGE" in res.stdout, (
        "the stranding report terminated the run before the merge stage:\n"
        f"rc={res.returncode}\n{res.stdout}{res.stderr}"
    )
    assert res.returncode == 0
    assert "commit 8" in res.stdout, "and it must still actually report the subjects"


def test_no_truncating_pipeline_is_left_unguarded():
    """Every remaining `head -N` must be defused by `|| true`, or a future input growth revives
    exactly this bug somewhere else."""
    offenders = []
    for n, line in enumerate(SCRIPT.read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "| head -" in line and "|| true" not in line:
            offenders.append(f"{n}: {stripped[:110]}")
    assert not offenders, (
        "truncating pipelines that pipefail can turn into a run-ending failure:\n  "
        + "\n  ".join(offenders)
    )
