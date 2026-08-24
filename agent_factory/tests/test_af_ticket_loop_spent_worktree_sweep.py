"""A fully-merged worker worktree must be REMOVABLE, or the straggler invariant is a lie.

Observed at praxis round #1: the loop reported

    STRAGGLER INVARIANT VIOLATED at round #1 — resolution ran and did NOT clear these:
      leftover worktree /workspace/praxis-build-r0a-d3a01c6b (branch af-build/praxis-r0a-d3a01c6b)

while that branch had ZERO commits not already on the integration branch. ``--resolve-orphans``
duly ran, found nothing to land, and the violation stood — an invariant nothing could satisfy.

Two guards collided:

  * ``af_stragglers`` reported any worktree whose branch ``af_is_owed_merge`` claimed, and that
    predicate answers YES for every branch checked out in a worktree, merged or not.
  * ``sweep_worktrees`` would only remove trees under a scratch ROOT. That instinct was sound — an
    early version was one ancestry check away from deleting the factory checkout all three loops
    execute from — but as a rule it was a path prefix, and a worker tree made elsewhere could never
    be swept by anything, ever.

The fix replaces the path test with ownership by FACT plus proof the tree is spent. These tests
build real git repositories and run the SHIPPED shell functions against them, because the failure
was in what the functions did to a real worktree layout; every function involved reads clean.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# Every helper the sweep reaches. Extracted rather than re-implemented: a copy would prove only
# that this file agrees with itself.
_FUNCS = (
    "af_main_worktree", "af_scratch_roots", "af_scratch_globs", "af_is_scratch",
    "af_is_human_branch", "af_is_worktree_branch", "af_is_factory_named",
    "af_worktree_is_removable", "af_force_remove_worktree", "af_stragglers",
    "af_dir_in_use", "af_is_owed_merge", "sweep_worktrees",
)


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _matchers() -> str:
    """The one-line text matchers. Missing from a harness they are a silent 127 that reads as
    "no match" and inverts whatever guard is under test."""
    return "\n".join(re.findall(r"^af_(?:i?has|hasf|hasx)\(\)\{.*$", SCRIPT.read_text(), re.M))


def _constants() -> str:
    """Module-level constants the extracted functions read. Missing, `set -u` kills the function
    mid-loop and it reports no holder at all — inverting the guard under test."""
    return "\n".join(re.findall(r"^_AF_NOT_A_HOLDER=.*$", SCRIPT.read_text(), re.M))


def _sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project checkout on `build/research-engine`, as the loop's worktree actually sits."""
    wt = tmp_path / "project"
    wt.mkdir()
    _sh("git init -q -b main && git config user.email t@t && git config user.name t", wt)
    (wt / "f.txt").write_text("one\n")
    _sh("git add -A && git commit -qm 'base'", wt)
    _sh("git checkout -q -b build/research-engine", wt)
    return wt


def _worker_tree(repo: Path, path: Path, branch: str, *, merge_back: bool) -> Path:
    """A worker's worktree on its own branch; optionally already merged into the integration ref."""
    _sh(f"git worktree add -q -b {branch} {path}", repo)
    (path / "f.txt").write_text("worker\n")
    _sh("git add -A && git commit -qm 'work (R0a)'", path)
    if merge_back:
        _sh(f"git merge -q --no-edit {branch}", repo)
    return path


def _run(repo: Path, body: str, *, known_ids: str = " R0a ") -> subprocess.CompletedProcess[str]:
    preamble = textwrap.dedent(
        f"""
        set -euo pipefail
        say(){{ echo "$*"; }}
        WT={repo}
        INTEGRATION_REF=build/research-engine
        AF_KNOWN_IDS="{known_ids}"
        """
    )
    program = preamble + _matchers() + "\n" + _constants() + "\n" + "\n".join(_function(f) for f in _FUNCS) + "\n" + body
    return subprocess.run(["bash", "-c", program], cwd=repo, capture_output=True, text=True,
                          timeout=120)


# ------------------------------------------------------------------------------ the regression --

def test_a_spent_worker_tree_is_swept(repo: Path, tmp_path: Path):
    """THE REGRESSION: merged branch, tree outside any scratch root, previously unremovable."""
    tree = _worker_tree(repo, tmp_path / "project-r0a-d3a01c6b",
                        "af-build/praxis-r0a-d3a01c6b", merge_back=True)

    res = _run(repo, "sweep_worktrees")

    assert not tree.exists(), f"the spent tree survived the sweep\n{res.stdout}{res.stderr}"
    assert "purged integrated worktree" in res.stdout


def test_after_the_sweep_the_straggler_invariant_can_actually_be_satisfied(repo: Path, tmp_path: Path):
    """The point of the fix: `af_stragglers` goes from reporting forever to reporting nothing."""
    _worker_tree(repo, tmp_path / "project-r0a-d3a01c6b",
                 "af-build/praxis-r0a-d3a01c6b", merge_back=True)

    before = _run(repo, "af_stragglers").stdout
    assert "leftover worktree" in before, "precondition: it IS reported while the tree is there"

    _run(repo, "sweep_worktrees")
    after = _run(repo, "af_stragglers").stdout.strip()
    assert after == "", f"invariant still unsatisfiable after resolution:\n{after}"


# ------------------------------------------------------- everything the guard must still refuse --

def test_a_tree_holding_unmerged_work_is_never_deleted(repo: Path, tmp_path: Path):
    """Nothing is ever deleted to reach a green invariant. Unmerged work stays put."""
    tree = _worker_tree(repo, tmp_path / "project-r9z-deadbeef",
                        "af-build/praxis-r9z-deadbeef", merge_back=False)

    _run(repo, "sweep_worktrees", known_ids=" R9z ")

    assert tree.exists(), "a tree with commits not on HEAD must survive"
    assert (tree / "f.txt").read_text() == "worker\n"


def test_the_factory_checkout_is_never_removed(repo: Path, tmp_path: Path):
    """The near-miss that motivated the original path guard: the main checkout is on `main`, and
    a build branch is normally AHEAD of it, so a naive ancestry test would delete the repo root."""
    main_wt = _sh("git worktree list --porcelain | awk '/^worktree /{print $2; exit}'", repo)

    _run(repo, "sweep_worktrees")

    assert Path(main_wt).exists()
    assert repo.exists()


def test_a_sibling_project_tree_is_not_ours_to_remove(repo: Path, tmp_path: Path):
    """A merged tree on a branch carrying NO id this project owns is someone else's business."""
    tree = _worker_tree(repo, tmp_path / "sibling", "af-build/other-x9-cafe", merge_back=True)

    _run(repo, "sweep_worktrees", known_ids=" R0a ")

    assert tree.exists(), "ownership is a fact from Praxis, not a prefix guess"


def test_a_human_branch_is_refused_even_when_fully_merged(repo: Path, tmp_path: Path):
    tree = _worker_tree(repo, tmp_path / "human", "release/2026-08", merge_back=True)

    _run(repo, "sweep_worktrees", known_ids=" R0a release/2026-08 ")

    assert tree.exists(), "a human-owned branch must be refused before ownership is even consulted"


def test_a_tree_in_use_by_a_live_process_is_skipped(repo: Path, tmp_path: Path):
    """A worker's evals are executing against those files; the tree is not spent yet."""
    tree = _worker_tree(repo, tmp_path / "project-r0a-inuse",
                        "af-build/praxis-r0a-inuse", merge_back=True)
    holder = subprocess.Popen(["sleep", "30"], cwd=tree)
    try:
        # Popen returns before the child has necessarily chdir'd, and the guard reads /proc/<pid>/cwd.
        # Without this wait the test races the kernel and the assertion becomes a coin flip.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == tree.resolve():
                break
            time.sleep(0.05)
        else:
            pytest.fail("the holder process never took up residence in the tree")

        res = _run(repo, "sweep_worktrees")
        assert tree.exists(), res.stdout + res.stderr
        assert "IN USE" in res.stdout
    finally:
        holder.kill()
        holder.wait()


# ---------------------------------------------------------------------------- the naming fact ----

@pytest.mark.parametrize(
    ("branch", "ids", "owned"),
    [
        ("af-build/praxis-r0a-d3a01c6b", " R0a ", True),    # the real shape, lower-cased slug
        ("af-build/praxis-R0a-d3a01c6b", " R0a ", True),
        ("build/HIP-23", " HIP-23 ", True),                 # exact segment, the old form
        ("worktree-agent-9f2", " ", True),                  # harness-minted, no id needed
        ("build/login-redesign", " R0a ", False),           # a human's branch
        ("af-build/other-x9-cafe", " R0a ", False),         # a sibling project's ticket
        ("af-build/praxis-r0abc-d3a", " R0a ", False),      # substring, not a whole token
    ],
)
def test_factory_ownership_is_decided_by_owned_ticket_ids(repo: Path, branch: str, ids: str,
                                                          owned: bool):
    res = _run(repo, f'if af_is_factory_named "{branch}"; then echo YES; else echo NO; fi',
               known_ids=ids)
    assert res.stdout.strip() == ("YES" if owned else "NO"), res.stdout + res.stderr


def test_the_liveness_guard_survives_pipefail(repo: Path, tmp_path: Path):
    """The plumbing bug, pinned on its own so a refactor cannot reintroduce it.

    The driver runs under `set -o pipefail`. `readlink /proc/*/cwd | grep -q <path>` therefore
    reports FAILURE whenever any single /proc entry is unreadable — which a handful of root-owned
    processes guarantee on every real box — even when grep matched. The guard silently inverted:
    it never once protected a live worker's tree.

    Asserting on the shape rather than only on behaviour, because the behavioural test above can
    pass by luck on a box where readlink happens to succeed for every process (a container running
    as root, say), and this is exactly the class of bug that reads correct.
    """
    # Comments are stripped first: the fix's own comment QUOTES the broken spelling in order to
    # explain it, and a naive substring search would match that and fail forever.
    code = "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "readlink /proc/*/cwd 2>/dev/null | grep -q" not in code, (
        "the liveness check must not put readlink in a pipeline whose status pipefail can steal"
    )

    # And the behaviour, under the same options the driver uses.
    tree = _worker_tree(repo, tmp_path / "project-r0a-pf", "af-build/praxis-r0a-pf",
                        merge_back=True)
    holder = subprocess.Popen(["sleep", "30"], cwd=tree)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == tree.resolve():
                break
            time.sleep(0.05)
        res = _run(repo, "sweep_worktrees")
        assert "IN USE" in res.stdout, res.stdout + res.stderr
        assert tree.exists()
    finally:
        holder.kill()
        holder.wait()


def test_af_dir_in_use_answers_yes_for_a_live_holder_and_no_otherwise(repo: Path, tmp_path: Path):
    """The predicate on its own, since two of its three callers go straight to `rm -rf`."""
    busy = tmp_path / "busy"
    idle = tmp_path / "idle"
    neighbour = tmp_path / "busybody"   # shares a prefix with `busy` — must NOT protect it
    for d in (busy, idle, neighbour):
        d.mkdir()

    holder = subprocess.Popen(["sleep", "30"], cwd=neighbour)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == neighbour.resolve():
                break
            time.sleep(0.05)

        def asks(d: Path) -> str:
            return _run(repo, f'if af_dir_in_use "{d}"; then echo YES; else echo NO; fi').stdout.strip()

        assert asks(neighbour) == "YES"
        assert asks(busy) == "NO", "a prefix neighbour must not be mistaken for a live holder"
        assert asks(idle) == "NO"
    finally:
        holder.kill()
        holder.wait()


def test_the_orphan_directory_reaper_will_not_rm_rf_a_live_tree(repo: Path, tmp_path: Path):
    """The most dangerous of the three callers: it deletes with `rm -rf`, not `git worktree remove`.

    An orphan is a directory under a scratch root with NO worktree registration — precisely the
    state a tree is in when its registration was pruned while the worker kept building.
    """
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True)
    orphan = root / "agent-deadbeef"
    orphan.mkdir()
    (orphan / "work.txt").write_text("a worker's in-flight output\n")

    holder = subprocess.Popen(["sleep", "30"], cwd=orphan)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == orphan.resolve():
                break
            time.sleep(0.05)

        res = _run(repo, "sweep_worktrees")

        assert orphan.exists(), f"rm -rf'd a live worker's tree\n{res.stdout}{res.stderr}"
        assert (orphan / "work.txt").exists()
        assert "IN USE" in res.stdout
    finally:
        holder.kill()
        holder.wait()


def test_the_orphan_directory_reaper_still_removes_a_dead_tree(repo: Path, tmp_path: Path):
    """The fix must not make the reaper inert — leaked trees are what filled a 98GB volume."""
    root = repo / ".claude" / "worktrees"
    root.mkdir(parents=True)
    orphan = root / "agent-cafef00d"
    orphan.mkdir()
    (orphan / "junk.txt").write_text("residue\n")

    res = _run(repo, "sweep_worktrees")

    assert not orphan.exists(), res.stdout + res.stderr
    assert "removed orphaned worktree dir" in res.stdout


# ------------------------------------------------- a parked spare must not veto a merged tree ---

def test_an_idle_spare_does_not_hold_a_worktree(repo: Path, tmp_path: Path):
    """THE HAZARD CREATED BY FIXING THE LIVENESS CHECK.

    `claude bg-spare` is a pre-warmed Claude Code spare parked on a claim socket. It does no work,
    its cwd is wherever it was started, and it never exits on its own. Counting it as a holder makes
    it a PERMANENT veto on removing that tree.

    Observed on two consecutive praxis rounds: with the round's real workers gone and both branches
    fully merged (0 commits not on HEAD), two spares — one 2h23m old, predating the round, with
    2m48s of CPU across its whole life — kept the trees alive. Every round logged STRAGGLER
    INVARIANT VIOLATED, and at drain that FAILS the run over trees whose commits had all landed.

    While the liveness check was broken by pipefail it never fired, so these trees were swept
    regardless and nobody could discover a spare could veto one. Fixing the check is what exposed it.
    """
    tree = _worker_tree(repo, tmp_path / "project-r0a-spare", "af-build/praxis-r0a-spare",
                        merge_back=True)
    # argv is what distinguishes a spare, so the HOLDING process must carry it itself. A wrapper
    # script that shells out to `sleep` does not work: the process whose cwd sits in the tree is
    # then the child, whose argv is just "sleep 30".
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "bg-spare", "--bg-spare", "/tmp/x.claim.sock"],
        cwd=tree)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == tree.resolve():
                break
            time.sleep(0.05)

        res = _run(repo, "sweep_worktrees")

        assert not tree.exists(), (
            "an idle spare vetoed removal of a fully-merged tree:\n" + res.stdout + res.stderr)
        assert "purged integrated worktree" in res.stdout
    finally:
        holder.kill()
        holder.wait()


def test_a_real_worker_still_holds_the_tree_and_is_named(repo: Path, tmp_path: Path):
    """The guard must still guard — and say WHO, because 'IN USE by a live process' cost two rounds
    of walking /proc by hand to identify the culprit."""
    tree = _worker_tree(repo, tmp_path / "project-r0a-busy2", "af-build/praxis-r0a-busy2",
                        merge_back=True)
    holder = subprocess.Popen(["sleep", "30"], cwd=tree)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(f"/proc/{holder.pid}/cwd").resolve() == tree.resolve():
                break
            time.sleep(0.05)

        res = _run(repo, "sweep_worktrees")

        assert tree.exists(), res.stdout + res.stderr
        assert "IN USE" in res.stdout
        assert f"pid {holder.pid}" in res.stdout, "the holder must be named"
        assert "sleep" in res.stdout, "and identifiable from its argv"
    finally:
        holder.kill()
        holder.wait()


def test_af_dir_in_use_does_not_clobber_its_callers_loop_variable(repo: Path, tmp_path: Path):
    """A near-miss worth pinning forever.

    The orphan reaper runs `for d in "$root"/*` and calls af_dir_in_use inside that loop, then
    `rm -rf "$d"`. af_dir_in_use scans /proc with its own `for d in /proc/[0-9]*`. Without
    `local d`, the callee overwrites the caller's loop variable and the reaper deletes the LAST
    /PROC ENTRY instead of the orphan directory.

    It surfaced only because /proc refuses the unlink ("rm: cannot remove
    '/proc/959265/task/959265/fd/0': Permission denied"). Against any other value of `d` it would
    have silently removed the wrong directory — and this reaper is one of the two callers that go
    straight to `rm -rf`.
    """
    program = (
        textwrap.dedent(
            f"""
            set -uo pipefail
            say(){{ echo "$*"; }}
            WT={repo}
            """
        )
        + _matchers() + "\n" + _constants() + "\n"
        + _function("af_dir_in_use")
        + '\nd=SENTINEL\naf_dir_in_use /definitely/not/a/real/path || true\necho "d=$d"\n'
    )
    res = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)
    assert "d=SENTINEL" in res.stdout, (
        "af_dir_in_use clobbered its caller's `d` — the orphan reaper rm -rf's that variable\n"
        + res.stdout + res.stderr
    )
