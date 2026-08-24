"""A completed run must leave ZERO worker branches — not fewer, zero.

Worktree removal deliberately keeps the branch, but nothing ever revisited the branch afterwards, so
one was created per worker, per ticket, per round and lived forever. Measured on
/workspace/appeal_engine: 38 branches for ~11 tickets across three runs, 34 of them fully merged into
main and 4 "unmerged" — one already upstream by patch-id, two attempts the post-merge verifier
rejected and rebuilt, one a stale baseline re-anchor. None held recoverable work.

The cost is the lost signal, not the disk. "Unmerged branch" is supposed to mean THIS WORK NEVER
LANDED; buried under dozens of identical `worktree-agent-*` names it means nothing. `reap_branches`
deletes the residue so that a survivor is real — and hard-fails the round when a ticket reads
`finished` while its commits sit on a branch nothing ever merged.

The functions are lifted out of the script and executed against real scratch repos: these are git
semantics (ancestry vs patch-id, refusal to delete a checked-out branch), and a shape-copy test would
prove nothing about them.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# say/praxis_q/regress_ticket are the driver's, and stubbed; everything under test is lifted verbatim.
HARNESS = """
set -uo pipefail
say(){ printf 'LOG %s\\n' "$*"; }
praxis_q(){ "$@"; }
# The driver discards praxis_q's stdout and logs its own line, so record the call out of band.
regress_ticket(){ printf 'REGRESS %s %s\\n' "$1" "$2" >> "$PWD/regress.log"; }
PROJECT=demo
WT="$PWD"
INTEGRATION_REF=main
"""


def _extract(*names: str) -> str:
    """Pull whole function definitions out of the driver by name."""
    src = SCRIPT.read_text().splitlines()
    out = []
    for name in names:
        start = next(i for i, line in enumerate(src) if line.startswith(f"{name}(){{"))
        end = next(i for i in range(start + 1, len(src)) if src[i] == "}")
        out.append("\n".join(src[start : end + 1]))
    assert len(out) == len(names)
    return "\n".join(out)


# The one-line text matchers every pane check now goes through. Missing from a harness they are a
# silent 127 -- these harnesses run without `set -e` -- which reads as "no match" and quietly
# inverts the guard under test, exactly the way the absent af_main_worktree did.
MATCHERS = "\n".join(re.findall(r"^af_(?:i?has|hasf|hasx)\(\)\{.*$", SCRIPT.read_text(), re.M))
assert MATCHERS.count("\n") == 3, "expected the four af_*has helpers"


OWNERSHIP = (
    "af_owned_ids",
    "af_branch_ids",
    "af_is_human_branch",
    "af_is_worktree_branch",
    "af_is_owed_merge",
    "af_is_factory_named",
    "af_branch_is_foreign_era",
    "af_sanitize_branch",
)
FUNCS = MATCHERS + "\n" + _extract(*OWNERSHIP, "reap_branches")

# Everything the terminal invariant needs. `resolve_conflicts` is the one thing that cannot run here
# — it dispatches a tmux agent — so it is stubbed to a no-op AFTER extraction (a later definition
# wins), which is also the pessimistic case the red-path test wants: resolution that fixes nothing.
INVARIANT_FUNCS = MATCHERS + "\n" + _extract(
    *OWNERSHIP,
    # af_main_worktree was ALREADY missing here: af_scratch_roots calls it, and under the harness's
    # `set -uo pipefail` (no -e) a "command not found" is a silent 127 that degrades the scratch-root
    # list instead of failing. The gap only became visible when sweep_worktrees started calling it
    # directly. Extracting it makes the harness run the same code the driver runs.
    "af_main_worktree",
    "af_dir_in_use",
    "af_worktree_is_removable",
    "af_scratch_roots",
    "af_scratch_globs",
    "af_is_scratch",
    "af_force_remove_worktree",
    "sweep_worktrees",
    "queue_orphan_branches",
    "reap_branches",
    "af_stragglers",
    "af_assert_no_stragglers",
)

INVARIANT_HARNESS = (
    HARNESS
    + """
CONFLICTS="$PWD/conflicts.tsv"
: > "$CONFLICTS"
resolve_conflicts(){ printf 'RESOLVER %s\\n' "$1"; return 0; }
"""
)


def _stragglers(repo: Path, known: str = "", finished: str = "", assert_where: str = "") -> tuple[int, str]:
    tail = f'af_assert_no_stragglers "{assert_where}"' if assert_where else "af_stragglers"
    script = f"""{INVARIANT_HARNESS}
AF_KNOWN_IDS=" {known} "
AF_FINISHED_IDS=" {finished} "
{INVARIANT_FUNCS}
{tail}
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=180
    )
    return r.returncode, r.stdout + r.stderr


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _commit(repo: Path, fname: str, body: str, subject: str) -> str:
    (repo / fname).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "README", "base\n", "chore: base")
    return r


def _reap(repo: Path, known: str = "R8 R9 LADDER-1", finished: str = "",
          start_epoch: int = 0) -> tuple[int, str]:
    # start_epoch=0 is the "no run boundary known" default the driver itself falls back to, under
    # which NO branch can be foreign-era — so every pre-existing case below is unaffected by the
    # foreign-era guard and still exercises the regress path it was written for.
    script = f"""{HARNESS}
AF_KNOWN_IDS=" {known} "
AF_FINISHED_IDS=" {finished} "
AF_START_EPOCH={start_epoch}
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )
    return r.returncode, r.stdout


def _branches(repo: Path) -> list[str]:
    return sorted(
        _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
    )


def _worker_branch(repo: Path, name: str, fname: str, body: str, subject: str) -> None:
    """Build a worker branch the way a round does, and return to main."""
    _git(repo, "checkout", "-q", "-b", name)
    _commit(repo, fname, body, subject)
    _git(repo, "checkout", "-q", "main")


def test_a_merged_worker_branch_is_reaped_and_only_main_survives(repo: Path):
    """The headline invariant: `git branch --list | wc -l` is exactly 1 after a clean run."""
    for i, name in enumerate(["worktree-agent-aaa1", "worktree-agent-bbb2", "worktree-wf_cc3"]):
        _worker_branch(repo, name, f"f{i}.py", f"x = {i}\n", f"feat: thing {i} (R8)")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q", name)

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"], "a finished ticket must own no branches"
    assert "3 branches reaped, 0 unmerged branches remain" in out


def test_patch_equivalent_branch_is_reaped_despite_not_being_an_ancestor(repo: Path):
    """`git cherry`, not ancestry. Three of the four appeal_engine survivors looked unmerged for
    exactly this reason: the work was upstream under a different sha."""
    _worker_branch(repo, "worktree-agent-dup", "a.py", "a = 1\n", "feat: a (R8)")
    # The integrator is a DIFFERENT identity from the worker (as in a real landing), and
    # naming it matters twice over: a bare CI runner has no git identity at all, so an
    # undecorated cherry-pick dies with "Committer identity unknown"; and reusing the
    # worker's identity would make the replayed commit byte-identical to the original
    # (same tree, parent, message, author) and collapse it to the SAME sha — destroying
    # the patch-equivalent-but-different-sha condition this test exists to exercise.
    _git(repo, "-c", "user.name=integrator", "-c", "user.email=i@i",
         "cherry-pick", _git(repo, "rev-parse", "worktree-agent-dup"))
    _commit(repo, "later.py", "later = 1\n", "chore: unrelated later commit")
    assert _git(repo, "rev-list", "--count", "main..worktree-agent-dup") == "1"

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"]
    assert "already upstream by patch-id" in out
    assert "1 branches reaped, 0 unmerged branches remain" in out


def test_superseded_attempt_is_reaped_and_names_its_replacement(repo: Path):
    """The verifier rejected this attempt and a later round rebuilt it; main carries the correction."""
    _worker_branch(repo, "worktree-agent-old", "a.py", "broken = 1\n", "feat: a (R8)")
    sha = _commit(repo, "a.py", "fixed = 1\n", "feat: a, rebuilt after regression (R8)")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert _branches(repo) == ["main"]
    assert "a superseded attempt" in out and f"R8 superseded by {sha}" in out


def test_finished_ticket_whose_work_never_landed_fails_the_round(repo: Path):
    """The LADDER-1 case: caught by the verifier only by luck, invisible to branch bookkeeping."""
    _worker_branch(repo, "worktree-agent-ladder", "l.py", "l = 1\n", "feat: ladder (LADDER-1)")

    rc, out = _reap(repo, finished="LADDER-1")

    assert rc == 1, "a lying ticket must FAIL the round, not be reported and skimmed"
    assert "worktree-agent-ladder" in _branches(repo), "unique work is never deleted"
    assert "ROUND FAILED" in out
    assert (repo / "regress.log").read_text().strip() == "REGRESS LADDER-1 worktree-agent-ladder"
    assert "regressed LADDER-1 to incomplete" in out
    assert "feat: ladder (LADDER-1)" in out, "the missing commits must be named"
    assert "1 branches reaped, 1 unmerged branches remain: worktree-agent-ladder" not in out
    assert "0 branches reaped, 1 unmerged branches remain: worktree-agent-ladder" in out


# ------------------------------------------------------- foreign-era branches (2026-08-09) --
#
# THE INCIDENT. A forgotten af-ticket-loop left worker branches behind. Days later a different loop
# started, its orphan sweep found those branches, saw that the tickets they named read `finished`
# with commits not on the integration ref, and regressed R27 — a ticket a DIFFERENT run had genuinely
# completed — then failed the round over it. The sweep was reasoning about work that was never its
# to judge, and the only evidence needed to tell the difference was already on the branch: its tip
# commit was written before this run existed.


def _aged_worker_branch(repo: Path, name: str, fname: str, subject: str, when: int) -> None:
    """A worker branch whose tip was committed at epoch ``when`` — i.e. by some earlier run."""
    _git(repo, "checkout", "-q", "-b", name)
    (repo / fname).write_text("x = 1\n")
    _git(repo, "add", "-A")
    stamp = f"{when} +0000"
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", subject],
        cwd=repo, check=True, timeout=60,
        env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp},
    )
    _git(repo, "checkout", "-q", "main")


def test_foreign_era_branch_is_archived_as_a_tag_and_its_ticket_is_not_regressed(repo: Path):
    """The headline fix: an earlier run's leftovers neither regress a ticket nor fail the round."""
    _aged_worker_branch(repo, "af-build/R27", "r27.py", "feat: r27 (R27)", when=1_000_000_000)

    rc, out = _reap(repo, known="R27", finished="R27", start_epoch=2_000_000_000)

    assert rc == 0, f"an earlier run's residue must not fail this run's round:\n{out}"
    assert not (repo / "regress.log").exists(), (
        "R27 was finished by another run days ago — regressing it destroys a real completion"
    )
    assert "ROUND FAILED" not in out
    assert "foreign-era branch af-build/R27" in out and "not regressed" in out
    # Deleted, so the next sweep does not rediscover it and ask the same question forever...
    assert _branches(repo) == ["main"]
    # ...but nothing is lost: the commits are reachable from the archive tag.
    tags = _git(repo, "tag", "--list").splitlines()
    assert tags == ["archive/foreign-af-build-R27"]
    assert "feat: r27 (R27)" in _git(repo, "log", "-1", "--format=%s", "archive/foreign-af-build-R27")


def test_a_branch_written_during_this_run_is_still_regressed(repo: Path):
    """The negative case, and the one that matters most: the foreign-era guard must not become a
    blanket amnesty. A branch whose tip lands AFTER the run started is this run's own output, and a
    ticket that reads finished while that work sits unmerged is still lying."""
    _aged_worker_branch(repo, "af-build/R28", "r28.py", "feat: r28 (R28)", when=2_000_000_500)

    rc, out = _reap(repo, known="R28", finished="R28", start_epoch=2_000_000_000)

    assert rc == 1
    assert "ROUND FAILED" in out
    assert (repo / "regress.log").read_text().strip() == "REGRESS R28 af-build/R28"
    assert "af-build/R28" in _branches(repo), "unique work is never deleted on the failure path"


def test_unfinished_ticket_with_unique_work_survives_and_is_named(repo: Path):
    """Work still in flight is not residue."""
    _worker_branch(repo, "worktree-agent-live", "n.py", "n = 1\n", "feat: partial (R9)")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-live" in _branches(repo)
    assert "R9 is not finished" in out
    assert "0 branches reaped, 1 unmerged branches remain: worktree-agent-live" in out


def test_branch_with_no_owned_ticket_id_survives(repo: Path):
    """Provenance that cannot be established is a reason to keep, never to delete."""
    _worker_branch(repo, "worktree-agent-foreign", "b.py", "b = 1\n", "fix: upstream thing (BES-115)")

    rc, out = _reap(repo, known="R8", finished="R8")

    assert rc == 0, out
    assert "worktree-agent-foreign" in _branches(repo)
    assert "provenance cannot be established" in out


def test_human_branches_are_never_touched(repo: Path):
    """Only refs the factory itself mints are in scope — and `build/<X>` only when X is our ticket."""
    for name, subject in [
        ("fix/mypy-strict-unblock", "fix: mypy (R8)"),
        ("wip/type1-annotations", "wip: annotations (R8)"),
        ("build/login-redesign", "feat: login (R8)"),
        ("build/R8", "feat: old layout (R8)"),
    ]:
        _worker_branch(repo, name, f"{name.replace('/', '_')}.py", "x = 1\n", subject)
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q", name)

    rc, out = _reap(repo, known="R8", finished="R8")

    assert rc == 0, out
    assert _branches(repo) == [
        "build/login-redesign",
        "fix/mypy-strict-unblock",
        "main",
        "wip/type1-annotations",
    ], "merged-looking human branches must survive; only build/<known-ticket> is ours"
    assert "1 branches reaped" in out


def test_a_branch_checked_out_in_a_live_worktree_is_left_alone(repo: Path, tmp_path: Path):
    """A worker's tree is still executing against it. git refuses anyway; skipping keeps the log
    honest, and is why the reap must run AFTER the worktree sweep."""
    _worker_branch(repo, "worktree-agent-busy", "c.py", "c = 1\n", "feat: c (R8)")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-busy")
    _git(repo, "worktree", "add", str(tmp_path / "live"), "worktree-agent-busy")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-busy" in _branches(repo)
    assert "0 branches reaped, 0 unmerged branches remain" in out, (
        "an in-flight branch is neither reaped nor reported as an orphan"
    )


def test_the_integration_branch_itself_is_never_a_candidate(repo: Path):
    """Even if the checked-out branch were named like a worker's."""
    _git(repo, "checkout", "-q", "-b", "worktree-agent-integration")

    rc, out = _reap(repo, finished="R8")

    assert rc == 0, out
    assert "worktree-agent-integration" in _branches(repo)


def test_detached_head_compares_against_head_not_the_startup_sha(repo: Path):
    """INTEGRATION_REF is a fixed sha on a detached checkout — both build worktrees on the box are —
    and it does NOT move as integrate_round merges. Comparing against it would read every branch the
    round just landed as unique work, and a finished ticket's branch as a hard failure."""
    startup_sha = _git(repo, "rev-parse", "HEAD")
    _worker_branch(repo, "worktree-agent-det", "d.py", "d = 1\n", "feat: d (R8)")
    _git(repo, "checkout", "-q", "--detach", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-det")
    assert _git(repo, "rev-parse", "HEAD") != startup_sha

    script = f"""{HARNESS.replace('INTEGRATION_REF=main', f'INTEGRATION_REF={startup_sha}')}
AF_KNOWN_IDS=" R8 "
AF_FINISHED_IDS=" R8 "
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "worktree-agent-det" not in _branches(repo), "the merged branch must still be reaped"
    assert "ROUND FAILED" not in r.stdout


def test_keep_branches_knob_reports_without_deleting(repo: Path):
    _worker_branch(repo, "worktree-agent-keep", "k.py", "k = 1\n", "feat: k (R8)")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-edit", "-q",
         "worktree-agent-keep")

    script = f"""{HARNESS}
AF_KEEP_BRANCHES=1
AF_KNOWN_IDS=" R8 "
AF_FINISHED_IDS=" R8 "
{FUNCS}
reap_branches
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=120
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "worktree-agent-keep" in _branches(repo)
    assert "0 branches reaped, 1 unmerged branches remain" in r.stdout


def test_reaping_is_an_invariant_wired_to_the_exit_trap():
    """Same lesson as the worktree fix in 4b5d743: every abnormal exit — the DEPENDENCY STALL break,
    exits 3/4/5/6, an operator `tmux kill-session` — must still reap and still print the report."""
    src = SCRIPT.read_text()
    trap = src[src.index("af_cleanup_on_exit(){") : src.index("trap af_cleanup_on_exit")]
    assert "reap_branches" in trap, "reaping is not attached to the exit trap"
    assert trap.index("sweep_worktrees") < trap.index("reap_branches"), (
        "git refuses to delete a branch checked out in a worktree, so the sweep must come first"
    )
    assert "trap af_cleanup_on_exit EXIT INT TERM" in src


def test_the_round_reaps_after_it_purges():
    src = SCRIPT.read_text()
    body = src[src.index("  integrate_round\n") :]
    assert body.index("sweep_worktrees") < body.index("reap_branches")


# --------------------------------------------------------------------------------------------
# The 2026-08-03 sotos regression: ownership was an ALLOWLIST OF NAMES, so a name nobody had
# anticipated (`af-build/HIP-23`) was skipped by queue_orphan_branches AND by reap_branches, and the
# run logged `drained — nothing claimable` over two tickets whose work was on no integration branch.
# These are the cases that would have gone red on that run.
# --------------------------------------------------------------------------------------------


def test_a_branch_with_a_never_before_seen_name_is_still_owed_a_merge(repo: Path):
    """THE regression test for the whole class. `af-build/*` matched no pattern this script knew, and
    nothing in this script mints it — a worker chose it. Ownership must not depend on having guessed
    the name, so the branch is detected by the SAME hard-failure path a `worktree-agent-*` branch
    would have taken: ticket finished, work nowhere, round fails, ticket regressed, branch KEPT."""
    _worker_branch(repo, "af-build/HIP-23", "h23.py", "h = 23\n", "feat: h23 (HIP-23)")

    rc, out = _reap(repo, known="HIP-23", finished="HIP-23")

    assert rc == 1, "a novel-named branch holding a finished ticket's only copy must FAIL the round"
    assert "af-build/HIP-23" in _branches(repo), "never delete unmerged work to make a check pass"
    assert "ROUND FAILED" in out and "HIP-23" in out
    assert (repo / "regress.log").read_text().strip() == "REGRESS HIP-23 af-build/HIP-23"


def test_an_unrecognised_branch_with_an_unrecognised_ticket_id_is_reported_not_skipped(repo: Path):
    """Second silent-miss path: the old `build/*` case consulted AF_KNOWN_IDS and, when it was empty,
    fell through to `return 1` — skip. An empty id set is 'we could not ask', never 'not ours'."""
    _worker_branch(repo, "totally-invented-name", "z.py", "z = 1\n", "feat: z (ZZZ-9)")

    rc, out = _reap(repo, known="", finished="")

    assert rc == 0, out
    assert "totally-invented-name" in _branches(repo)
    assert "0 branches reaped, 1 unmerged branches remain: totally-invented-name" in out, (
        "an unknown branch must be NAMED as unmerged, not silently passed over"
    )


def test_a_branch_checked_out_in_a_worktree_is_owned_whatever_it_is_called(repo: Path, tmp_path: Path):
    """FACT 1: `git worktree list --porcelain` is authoritative and name-independent. Even a name the
    human-exemption list would otherwise cover is factory work while a worktree holds it."""
    _git(repo, "branch", "hotfix/looks-human")
    _git(repo, "worktree", "add", str(tmp_path / "wt"), "hotfix/looks-human")

    script = f"""{HARNESS}
AF_KNOWN_IDS=" "
{_extract(*OWNERSHIP)}
af_is_owed_merge hotfix/looks-human && echo OWED || echo NOT_OWED
af_is_owed_merge hotfix/never-checked-out && echo OWED2 || echo NOT_OWED2
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=60
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "OWED\n" in r.stdout, "a branch in a worktree is factory work by definition"
    assert "NOT_OWED2" in r.stdout, "the explicit human exemption still applies off-worktree"


def test_the_integration_branch_and_registered_human_branches_stay_exempt(repo: Path):
    """The exemption is the ONLY way out, so it has to keep working — and AF_HUMAN_BRANCHES must ADD
    to the defaults rather than replace them, or setting it once would un-exempt main."""
    script = f"""{HARNESS}
AF_KNOWN_IDS=" "
AF_HUMAN_BRANCHES="spike/*"
{_extract(*OWNERSHIP)}
for b in main release/1.2 spike/idea af-build/HIP-23; do
  af_is_owed_merge "$b" && echo "OWED $b" || echo "EXEMPT $b"
done
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=60
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "EXEMPT main" in r.stdout
    assert "EXEMPT release/1.2" in r.stdout
    assert "EXEMPT spike/idea" in r.stdout
    assert "OWED af-build/HIP-23" in r.stdout


def test_deletion_stays_narrow_even_though_detection_is_broad(repo: Path):
    """The two questions must not collapse back into one. Detection is default-inclusive; deletion is
    gated on the narrow, id-derived test, so a human's already-merged branch is left alone."""
    script = f"""{HARNESS}
AF_KNOWN_IDS=" HIP-23 "
{_extract(*OWNERSHIP)}
for b in af-build/HIP-23 build/HIP-23 worktree-agent-x fix/mypy build/login-redesign; do
  af_is_factory_named "$b" && echo "DELETABLE $b" || echo "KEEP $b"
done
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=60
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "DELETABLE af-build/HIP-23" in r.stdout, "any prefix, when the id is one Praxis owns"
    assert "DELETABLE build/HIP-23" in r.stdout
    assert "DELETABLE worktree-agent-x" in r.stdout
    assert "KEEP fix/mypy" in r.stdout
    assert "KEEP build/login-redesign" in r.stdout


# ------------------------------------------------------------------- locked worktrees --


def _add_worktree(repo: Path, path: Path, branch: str) -> None:
    _git(repo, "worktree", "add", "-b", branch, str(path))


def test_a_locked_worktree_is_unlocked_and_swept(repo: Path):
    """`git worktree remove` and `git worktree prune` both REFUSE a locked tree, so a sweep that does
    not unlock first silently no-ops — the same failure shape as the branch miss. Two of the four
    sotos leftovers were locked and survived every sweep."""
    wt = repo / ".claude" / "worktrees" / "agent-deadbeef"
    wt.parent.mkdir(parents=True)
    _add_worktree(repo, wt, "worktree-agent-deadbeef")
    _git(repo, "worktree", "lock", str(wt))
    assert wt.exists()

    script = f"""{INVARIANT_HARNESS}
AF_KNOWN_IDS=" "
AF_FINISHED_IDS=" "
{INVARIANT_FUNCS}
sweep_worktrees
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=180
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert not wt.exists(), f"a locked worktree must be unlocked and removed, not skipped:\n{r.stdout}"
    assert "worktree-agent-deadbeef" in _branches(repo), "removing a tree keeps its branch"


def test_the_sibling_wt_layout_is_swept_too(repo: Path, tmp_path: Path):
    """The sotos leftovers were `/workspace/sotos-wt-HIP23` — a SIBLING of the checkout, under
    neither known scratch root, so every sweep walked past them."""
    wt = repo.parent / f"{repo.name}-wt-HIP23"
    _add_worktree(repo, wt, "af-build/HIP-23")
    assert wt.exists()

    script = f"""{INVARIANT_HARNESS}
AF_KNOWN_IDS=" HIP-23 "
AF_FINISHED_IDS=" "
{INVARIANT_FUNCS}
sweep_worktrees
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=180
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert not wt.exists(), r.stdout


# ------------------------------------------------------- the terminal invariant, both directions --


def test_terminal_invariant_holds_on_a_genuinely_clean_run(repo: Path):
    rc, out = _stragglers(repo, known="R8", finished="R8", assert_where="drain")

    assert rc == 0, out
    assert "straggler invariant HOLDS at drain" in out


def test_terminal_invariant_FAILS_when_an_unmerged_worker_branch_exists(repo: Path):
    """THE RED PATH. Two wrong conclusions were reached on 2026-08-03 by only ever watching the
    passing case. A guard nobody has seen go red is worth nothing, so this asserts the failure: a
    novel-named unmerged branch must make the run exit 7 instead of announcing a drain — and must
    still be sitting there afterwards, because nothing is ever deleted to turn the check green."""
    _worker_branch(repo, "af-build/HIP-27", "h27.py", "h = 27\n", "feat: h27 (HIP-27)")

    rc, out = _stragglers(repo, known="HIP-27", finished="", assert_where="drain")

    assert rc == 7, f"a straggler must exit 7, not report a clean drain:\n{out}"
    assert "STRAGGLER INVARIANT VIOLATED at drain" in out
    assert "unmerged branch af-build/HIP-27" in out
    assert "af-build/HIP-27" in _branches(repo), "the invariant must never delete its way to green"


def test_terminal_invariant_FAILS_on_a_leftover_worktree(repo: Path):
    """The other half: two locked `.claude/worktrees/agent-<hex>` trees outlived the sotos run."""
    wt = repo / ".claude" / "worktrees" / "agent-cafe"
    wt.parent.mkdir(parents=True)
    _add_worktree(repo, wt, "worktree-agent-cafe")
    _git(repo, "worktree", "lock", str(wt))
    # A tree git cannot remove at all is the pessimistic case: make removal impossible by
    # stubbing it out, and the invariant must still refuse to call the run clean.
    script = f"""{INVARIANT_HARNESS}
AF_KNOWN_IDS=" "
AF_FINISHED_IDS=" "
{INVARIANT_FUNCS}
af_force_remove_worktree(){{ return 1; }}
af_assert_no_stragglers "drain"
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=180
    )

    assert r.returncode == 7, f"an unsweepable worktree must fail loudly:\n{r.stdout}{r.stderr}"
    assert "leftover worktree" in r.stdout
    assert "could not purge" in r.stdout, "the sweep's own failure must be loud, not swallowed"


def test_terminal_invariant_is_not_fatal_at_a_round_boundary(repo: Path):
    """Work still in flight is legitimate mid-run: log loudly, force a resolution pass, keep going."""
    _worker_branch(repo, "af-build/HIP-30", "h30.py", "h = 30\n", "feat: h30 (HIP-30)")

    script = f"""{INVARIANT_HARNESS}
AF_KNOWN_IDS=" HIP-30 "
AF_FINISHED_IDS=" "
{INVARIANT_FUNCS}
af_assert_no_stragglers "round #1" 0 || echo NONFATAL_RC=$?
echo REACHED_THE_END
"""
    r = subprocess.run(
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, timeout=180
    )

    assert r.returncode == 0, r.stdout + r.stderr
    assert "NONFATAL_RC=1" in r.stdout
    assert "REACHED_THE_END" in r.stdout
    assert "STRAGGLER INVARIANT VIOLATED at round #1" in r.stdout


def test_a_straggler_forces_a_resolution_pass_before_it_is_reported(repo: Path):
    """Reporting is not the remedy. The invariant queues every straggler and runs the resolver before
    it will even consider failing — the sotos run's branches were never queued by anything."""
    _worker_branch(repo, "af-build/HIP-27", "h27.py", "h = 27\n", "feat: h27 (HIP-27)")

    rc, out = _stragglers(repo, known="HIP-27", finished="", assert_where="drain")

    assert rc == 7
    assert "STRAGGLERS PRESENT at drain" in out
    assert "orphan branch from an earlier round queued for landing: af-build/HIP-27" in out
    assert "RESOLVER straggler-sweep@drain" in out, "the resolver must actually be invoked"


# ------------------------------------------------------------------------------- wiring --


def test_the_invariant_is_wired_to_every_terminal_path():
    src = SCRIPT.read_text()
    assert 'af_assert_no_stragglers "drain"' in src, "the drain gate is the whole point"
    assert 'af_assert_no_stragglers "exit"' in src, "every break must pass through it"
    assert 'af_assert_no_stragglers "--resolve-orphans"' in src
    assert 'af_assert_no_stragglers "round #$round" 0' in src
    # It must sit BEFORE the loop announces it is finished, not after.
    assert src.index('af_assert_no_stragglers "exit"') < src.index('say "af-ticket-loop finished')
    assert "AF_EXIT_STRAGGLERS=7" in src, "a straggler needs its own exit code, not a reused one"


def test_ownership_is_no_longer_an_allowlist_of_names():
    """The shape of the fix, asserted structurally: the default answer to 'is this owed a merge' is
    YES. If someone re-inverts it to an allowlist, this goes red."""
    body = _extract("af_is_owed_merge")
    assert "af_is_worktree_branch" in body, "worktree membership must be consulted first"
    assert body.rstrip().endswith("return 0\n}"), (
        "the last word of the ownership test must be 'yes, it is ours' — a default of `return 1` is "
        "the allowlist bug this whole change exists to remove"
    )
