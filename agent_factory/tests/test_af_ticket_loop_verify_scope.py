"""A ticket answers for the diff its own branch introduced, and for nothing else.

Post-merge verification reviews "the combined diff of this round". That diff spans the pre-merge
commit to HEAD — and the integration branch also receives commits from the repository's DEFAULT
branch, which keeps moving while a build runs. Those changes ride into the merged tree belonging to
no ticket in the round, and the verifier, reviewing honestly, attributes them to whichever ticket is
in front of it.

praxis R3a was regressed THREE separate times for changes that came from the default branch and had
nothing to do with its ticket:

  round #3: "the ticket removed runtime tests that rejected caller-supplied stored noise-floor
             values" — real, and R3a fixed it
  round #4: "its branch independently weakened the existing heartbeat wait-loop wiring test ...
             commit 529d9f9" — a tooling commit
  round #1: "Its merged prompt changed the primary base-alignment banner from 'REBASE FIRST' to
             'GET YOUR BASE RIGHT'" — another tooling commit

Each one rebuilt the ticket from scratch, and the third also blocked the seven tickets waiting
behind it. The verifier was right about the change every time and wrong about whose it was.

This is the attribution half of the same root cause the base-commit provenance fix addressed: that
one stopped the STRANDING report counting base commits as ticket work; this one stops the REVIEW
doing it.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _prompt(basebr: str = "origin/main") -> str:
    src = SCRIPT.read_text()
    line = next(
        source_line
        for source_line in src.splitlines()
        if source_line.strip().startswith("local vprompt=")
    )
    body = line.strip()[len("local vprompt=") :]
    prog = (
        "set -u\n"
        f"rnd=7; premerge=abc123def; basebr={basebr}; PROJECT=praxis; ids_csv=R3a,R4b\n"
        "FINDINGS=/tmp/f.json; VERDICT=/tmp/v.json\n"
        f"vprompt={body}\n"
        'printf %s "$vprompt"\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(prog)
        path = fh.name
    try:
        res = subprocess.run(["bash", path], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        return res.stdout
    finally:
        os.unlink(path)


# ------------------------------------------------------------------------------ the regression --

def test_the_verifier_is_told_the_diff_contains_work_that_is_not_the_rounds():
    p = _prompt()
    assert "NOT EVERYTHING IN THAT DIFF IS THIS ROUND'S WORK" in p
    assert "keeps moving while a build runs" in p


def test_the_verifier_must_check_the_default_branch_before_blaming_a_ticket():
    p = _prompt()
    assert "Before you attribute ANY defect to a ticket" in p
    assert "merge-base --is-ancestor" in p
    assert "regress NOBODY for it" in p


def test_the_default_branch_is_named_so_the_check_is_runnable():
    """A rule the verifier cannot execute is prose. It gets the actual ref."""
    p = _prompt(basebr="origin/trunk")
    assert "(origin/trunk)" in p


def test_the_rule_degrades_cleanly_when_there_is_no_separate_default_branch():
    """The sports_analysis shape: the integration ref IS the default branch, so af_base_ref returns
    nothing. The rule must still render as valid prose rather than an empty parenthesis."""
    p = _prompt(basebr="")
    assert "NOT EVERYTHING IN THAT DIFF IS THIS ROUND'S WORK" in p
    assert "()" not in p


def test_the_reason_is_stated_so_the_rule_survives_paraphrase():
    """A verifier that knows only the rule applies it narrowly; one that knows the cost applies it
    to the next case nobody wrote down."""
    p = _prompt()
    assert "R3a was regressed THREE separate times" in p
    assert "answerable for the diff ITS OWN branch introduced" in p


def test_it_does_not_weaken_the_lenses():
    """Attribution is scoped; the search is not. The verifier must still hunt for failures across
    the whole merged tree — it just may not bill them to the wrong ticket."""
    p = _prompt()
    assert "actively look for a failure rather than confirm success" in p
    assert "Lens A integration conflict" in p
    assert "Lens C test integrity" in p


def test_the_base_ref_is_computed_from_the_project_worktree():
    """af_base_ref reads the repo; running it anywhere else would name the wrong branch."""
    src = SCRIPT.read_text()
    assert 'basebr=$(cd "$WT" && af_base_ref 2>/dev/null || true)' in src
