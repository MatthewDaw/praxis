"""A worker must cut its worktree from the branch the round integrates into.

The prompt used to say only: the worktree is created from the repo DEFAULT branch, so rebase onto
$INTEGRATION_REF first. That reconciles the divergence, but it also drags every commit the default
branch received WHILE THE BUILD RAN into the ticket's branch — and the integration ref carries a
different subset of those same commits, so the rebase can conflict on files neither the worker nor
its ticket ever touched.

Measured 2026-08-24, praxis. Both R3a and R4b hard-stopped:

    git rebase build/research-engine conflicted in
    agent_factory/tests/test_af_ticket_loop_concurrency_guards.py and
    agent_factory/tests/test_af_ticket_loop_heartbeat_outstanding.py while applying c312e71.
    Rebase was aborted; no project code was inspected or changed.

c312e71 was a tooling commit with nothing to do with either ticket. Both workers were correct to
refuse — building on a bad base is unmergeable by construction — and because R3a is the root of the
dependency graph, that blocked all seven remaining tickets and ended the run.

A worktree cut from the integration ref has no divergence to reconcile and cannot hit this at all.
The rebase path stays for harnesses that create the worktree themselves, now with the failure mode
named so a worker can tell a BASE conflict from a real one.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _prompt(integration_ref: str = "build/research-engine", size: int = 2) -> str:
    src = SCRIPT.read_text()
    line = next(line for line in src.splitlines() if line.strip().startswith("round_prompt="))
    body = line.strip()[len("round_prompt=") :]
    prog = (
        "set -u\n"
        f"size={size}; WORKFLOW_CAP=2; PROJECT=praxis; ids_csv=R1,R2\n"
        f"INTEGRATION_REF={integration_ref}\n"
        "PY=/usr/bin/python3; SERVICES=''; SWEEP_AMENDMENT=' A.'; PREEXISTING_RULE=' B.'\n"
        f"round_prompt={body}\n"
        'printf %s "$round_prompt"\n'
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


# ------------------------------------------------------------------------------- the fix ------

def test_the_worker_is_told_to_cut_its_worktree_from_the_integration_ref():
    p = _prompt()
    assert "git worktree add -b <your-branch> <path> build/research-engine" in p
    assert "no divergence to reconcile and cannot conflict" in p


def test_the_integration_ref_is_interpolated_not_left_as_a_variable():
    """A worker handed the literal '$INTEGRATION_REF' would branch from a ref that does not exist."""
    p = _prompt(integration_ref="build/some-other-branch")
    assert "build/some-other-branch" in p
    assert "$INTEGRATION_REF" not in p


# --------------------------------------------------- the fallback, and what it must warn about --

def test_the_rebase_fallback_survives_for_harness_created_worktrees():
    p = _prompt()
    assert "git merge --ff-only build/research-engine" in p
    assert "git rebase build/research-engine instead" in p


def test_the_worker_is_warned_that_a_base_conflict_is_not_its_ticket():
    """Without this a worker reads any conflict as its own problem and hard-stops — which is what
    took out the whole praxis dependency graph."""
    p = _prompt()
    lowered = p.lower()
    assert "files neither you nor your ticket touched" in lowered
    assert "the conflict is in the base and not in your work" in lowered


def test_the_worker_is_told_how_to_resolve_a_base_conflict():
    p = _prompt()
    assert "taking the integration ref's side for any file your ticket does not touch" in p
    assert "only record a blocker if a file you DO touch genuinely conflicts" in p


def test_the_hard_stop_on_an_unusable_base_is_preserved():
    """The refusal itself was correct and must remain: anything built on a bad base cannot land."""
    p = _prompt()
    assert "anything built on that base is unmergeable by construction" in p
