"""The straggler invariant must survive a LINKED build worktree.

Regression for the failure-learning-loop run of 2026-08-07. That run built in
``/workspace/af-praxis``, which is not a clone but a *linked* git worktree whose ``.git`` is a file
pointing into ``/workspace/praxis/.git/worktrees/af-praxis``. Two consequences broke both halves of
the straggler machinery, every round, and neither could ever clear itself:

  * ``isolation: worktree`` mints each worker tree under the repo's MAIN worktree
    (``/workspace/praxis/.claude/worktrees/agent-*``), never under the build checkout. The scratch
    roots were anchored to the build checkout alone, so ``af_is_scratch`` said no to every real
    worker tree: the broad reporting path listed them as leftovers while the narrow sweep skipped
    them.
  * The main checkout appears in ``git worktree list`` on the base branch, and the "checked out in
    some OTHER worktree => factory work" rule therefore called ``main`` owed-a-merge — so
    ``/workspace/praxis`` itself was reported as a leftover worktree. It is the checkout the driver
    executes from and can never be swept, so the terminal invariant was guaranteed to fail the run
    at drain no matter how well the build went.

Both properties are pinned against a real git repo in the same topology. The negative assertions
matter as much as the positive ones: the main checkout must never become sweepable, or a sweep would
delete the repo every loop runs from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# Pull just the path/branch classification helpers out of the driver and answer one question about
# them. Sourcing the script itself would run the whole loop; these functions are pure.
_HARNESS = r"""
set -u
FNS=$(awk '/^(af_main_worktree|af_scratch_roots|af_scratch_globs|af_is_scratch|af_is_worktree_branch|af_is_human_branch|af_is_owed_merge)\(\)\{/{f=1} f{print} f&&/^\}$/{f=0}' "$SCRIPT")
eval "$FNS"
cd "$WT"
if %(call)s; then echo YES; else echo NO; fi
"""


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> dict:
    """A main worktree, a LINKED build worktree, and a worker tree under the MAIN one."""
    root = tmp_path_factory.mktemp("straggler").resolve()
    main, build = root / "main", root / "build"
    run = lambda *a, **kw: subprocess.run(a, check=True, capture_output=True, cwd=kw.get("cwd", main))
    subprocess.run(("git", "init", "-q", str(main)), check=True, capture_output=True)
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (main / "f").write_text("x")
    run("git", "add", "f")
    run("git", "commit", "-qm", "init")
    base = subprocess.run(("git", "symbolic-ref", "--short", "HEAD"), cwd=main,
                          check=True, capture_output=True, text=True).stdout.strip()
    run("git", "worktree", "add", "-q", "-b", "af-build/praxis", str(build))
    (main / ".claude" / "worktrees").mkdir(parents=True)
    run("git", "worktree", "add", "-q", "-b", "worktree-agent-aXYZ",
        str(main / ".claude" / "worktrees" / "agent-aXYZ"))
    return {"main": main, "build": build, "base": base}


def _ask(repo: dict, call: str) -> bool:
    """True iff the driver's own helper answers yes, run from the linked build worktree."""
    out = subprocess.run(("bash", "-c", _HARNESS % {"call": call}),
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SCRIPT": str(SCRIPT),
                              "WT": str(repo["build"])},
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out in ("YES", "NO"), out
    return out == "YES"


def test_worker_tree_under_main_checkout_is_sweepable(repo):
    """The bug: worker trees land under the MAIN worktree, so they must be recognised there."""
    agent = repo["main"] / ".claude" / "worktrees" / "agent-aXYZ"
    assert _ask(repo, f'af_is_scratch "{agent}"')


def test_worker_tree_under_build_checkout_still_sweepable(repo):
    """The non-linked layout must keep working."""
    assert _ask(repo, f'af_is_scratch "{repo["build"]}/.claude/worktrees/agent-zzz"')


@pytest.mark.parametrize("name", ["main_checkout", "unrelated_path"])
def test_never_sweeps_what_it_must_not(repo, name):
    """A sweep that could delete the main checkout would take out the repo every loop runs from."""
    target = repo["main"] if name == "main_checkout" else repo["main"].parent / "unrelated"
    assert not _ask(repo, f'af_is_scratch "{target}"')


def test_base_branch_is_not_owed_a_merge(repo):
    """The main checkout sitting on the base branch is not factory work left behind."""
    base = repo["base"]
    assert not _ask(repo, f'af_is_worktree_branch "{base}"')
    assert not _ask(repo, f'af_is_owed_merge "{base}"')


def test_real_worker_branch_is_still_owed_a_merge(repo):
    """The whole point of the invariant must survive the fix."""
    assert _ask(repo, 'af_is_worktree_branch worktree-agent-aXYZ')
    assert _ask(repo, 'af_is_owed_merge worktree-agent-aXYZ')
