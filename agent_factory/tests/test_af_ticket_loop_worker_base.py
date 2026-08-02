"""Workers must be rebased onto the integration ref before they build.

`isolation: worktree` creates each worker's tree from the repo's DEFAULT branch
(`refs/remotes/origin/HEAD`, i.e. origin/main), never from the branch the run integrates into.
Confirmed 2026-07-31 from the branch reflog: "Created from origin/main".

On a long-running integration branch that gap decides whether the run produces anything.
proposed-side-buildout's `consolidate/all-work` had drifted 351 commits past origin/main, so every
worker authored its change against files the integration branch did not have. The work then would
not apply back -- not by merge, not cherry-picked alone -- and four consecutive green rounds landed
nothing while Praxis reported every ticket finished. Reconciling the branches reset the gap to zero
and it was back to 21 commits within a single round, because every ticket the factory lands widens
it again. So the rebase has to happen per worker, per round, not once by hand.

The ref is resolved with `symbolic-ref`, NOT `rev-parse --abbrev-ref`, and half the tests below
exist to hold that line. On a detached HEAD `--abbrev-ref` does not fail -- it SUCCEEDS and returns
the literal string "HEAD", so any `|| fallback` is unreachable and the worker's first command
becomes `git merge --ff-only HEAD`: a self-merge no-op that skips the rebase while reporting
success. The devbox's sotos-build worktree was detached mid-run when this was found, so the
ref-name form would have protected the very run it was written for.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
SRC = SCRIPT.read_text()

REF_ASSIGN = next(line for line in SRC.splitlines() if line.startswith("INTEGRATION_REF="))

# The resolution expression the driver uses, lifted here so the behavioural tests exercise the real
# thing rather than a paraphrase of it.
RESOLVE = 'git -C {d} symbolic-ref --quiet --short HEAD 2>/dev/null || git -C {d} rev-parse HEAD'


def _repo(d: str, *, detached: bool) -> str:
    """Init a one-commit repo at `d`; return its HEAD sha. Detach if asked."""
    def run(*a):
        return subprocess.run(a, cwd=d, capture_output=True, text=True, check=False)

    run("git", "init", "-q", "-b", "trunk")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    Path(d, "f").write_text("x")
    run("git", "add", "f")
    run("git", "commit", "-qm", "c1")
    sha = run("git", "rev-parse", "HEAD").stdout.strip()
    if detached:
        run("git", "checkout", "-q", "--detach", sha)
    return sha


def _resolve(d: str) -> str:
    return subprocess.run(
        ["bash", "-c", RESOLVE.format(d=d)], capture_output=True, text=True
    ).stdout.strip()


def test_integration_ref_is_resolved_from_the_project_worktree():
    assert 'git -C "$WT"' in REF_ASSIGN


def test_ref_resolution_uses_symbolic_ref_not_abbrev_ref():
    """Scoped to this assignment on purpose. `--abbrev-ref` is fine elsewhere in the driver -- it
    labels worker branches and log lines, where the literal 'HEAD' is harmless. It is only lethal
    HERE, where the value becomes a merge target."""
    assert "symbolic-ref" in REF_ASSIGN
    assert "--abbrev-ref" not in REF_ASSIGN, (
        "the ref-name form silently yields the literal 'HEAD' on a detached worktree"
    )


def test_ref_resolution_falls_back_to_a_sha():
    """A detached worktree has no branch name, so the fallback must be something a worker can
    actually merge FROM. A SHA resolves inside a worker's tree -- they share one object store."""
    assert "rev-parse HEAD" in REF_ASSIGN


def test_resolution_cannot_kill_the_run():
    """`set -e` plus a bare command substitution is how this driver has died twice before."""
    assert "||" in REF_ASSIGN, f"unguarded command substitution: {REF_ASSIGN}"


def test_detached_head_resolves_to_a_sha_not_the_word_head():
    """The regression itself, exercised against a real detached repo rather than asserted on text."""
    with tempfile.TemporaryDirectory() as d:
        sha = _repo(d, detached=True)

        stale = subprocess.run(
            ["git", "-C", d, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        assert stale == "HEAD", "precondition: --abbrev-ref yields the literal HEAD when detached"

        assert _resolve(d) == sha, "a detached worktree must resolve to its SHA"


def test_branch_checkout_still_resolves_to_the_branch_name():
    """The SHA fallback must not cost the readable branch name in the ordinary case."""
    with tempfile.TemporaryDirectory() as d:
        _repo(d, detached=False)
        assert _resolve(d) == "trunk"


def test_dispatch_prompt_orders_the_rebase_before_any_work():
    assert "REBASE FIRST" in SRC
    assert "git merge --ff-only $INTEGRATION_REF" in SRC
    assert "git rebase $INTEGRATION_REF" in SRC, "no fallback when ff-only is refused"


def test_rebase_instruction_precedes_the_build_instructions():
    """Ordering is the whole point -- a rebase told after the edits is worthless."""
    prompt = SRC[SRC.index("/af-build $PROJECT $ids_csv") :]
    assert prompt.index("REBASE FIRST") < prompt.index("do NOT end your turn")


def test_failure_to_rebase_blocks_the_build():
    """Building on the wrong base is unmergeable by construction, so it must not proceed."""
    assert "do NOT build" in SRC
    assert "record the blocker" in SRC


def test_ref_is_interpolated_not_literal():
    """The prompt is inside a double-quoted string, so $INTEGRATION_REF expands. If the prompt ever
    moves into single quotes the workers get a literal '$INTEGRATION_REF' and rebase onto nothing."""
    r = subprocess.run(
        ["bash", "-c", 'INTEGRATION_REF="my-branch"; echo "run: git rebase $INTEGRATION_REF"'],
        capture_output=True,
        text=True,
    )
    assert "git rebase my-branch" in r.stdout

    # And the real prompt line must use a double-quoted send-keys, not a single-quoted one.
    send = next(line for line in SRC.splitlines() if "/af-build $PROJECT $ids_csv" in line)
    assert re.search(r'tmux send-keys -t "\$SESSION" "', send), "prompt is not double-quoted"
