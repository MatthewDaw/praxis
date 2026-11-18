"""The round-end tail must integrate finished tickets, and a failure there must not read as a handoff.

mvpvue-league-core, 2026-11-18 (rolling build): ~12 rounds in ~6 hours, every one logged "round #1
complete", then "live scheduler handoff — preserving worker checkouts", and integrate_round NEVER ran.
Branches af/R<n> of finished tickets stayed unmerged and later tickets built from the old base. Two
things combined: some command between the round wait loop and integrate_round exited non-zero under
`set -euo pipefail`, and the EXIT trap prepared a "handoff" on every rc != 0, which skipped sweep, reap
and integrate and printed nothing about the real failure.

The integration checkout there is SPARSE, with a tracked-and-modified `.af-loop.lock` (an earlier
"wip:" commit swept the loop's own lock file into history). This drives the real round-end tail
against exactly that shape.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(os.environ.get("AF_LOOP_SCRIPT", Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"))
TEXT = SCRIPT.read_text()
RECOVERY = (SCRIPT.parent / "af-recovery.sh").read_text()


def _fn(name: str, text: str | None = None) -> str:
    """A top-level shell function, lifted verbatim: from `name(){` to the first line that is `}`."""
    text = TEXT if text is None else text
    start = text.index(f"\n{name}(){{") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _tail() -> str:
    start = TEXT.index('  if [ "$waited" -ge "$deadline" ]; then say "round #$round timed out')
    end = TEXT.index("  integrate_round\n", start) + len("  integrate_round\n")
    return TEXT[start:end]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "integration"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "mvpvu").mkdir()
    (r / "mvpvu" / "base.txt").write_text("base\n")
    (r / "other").mkdir()
    (r / "other" / "x.txt").write_text("x\n")
    (r / ".af-loop.lock").write_text("111 mvpvue-league-core\n")  # committed by an earlier wip: commit
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "wip: preserve in-flight output")
    for n in (1, 2):
        wt = tmp_path / f"wt{n}"
        _git(r, "worktree", "add", "-q", "-b", f"af/R{n}", str(wt), "main")
        (wt / "mvpvu" / f"r{n}.txt").write_text(f"r{n}\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", f"feat(league): ticket {n} (R{n})")
    # Sparse checkout: only mvpvu/. The loop then writes its lock file at the root, OUTSIDE the
    # patterns: `git status` lists it as modified but `git add -A` will not stage it.
    _git(r, "sparse-checkout", "set", "--no-cone", "/mvpvu/")
    (r / ".af-loop.lock").write_text("222 mvpvue-league-core\n")  # tracked AND modified
    return r


def _harness(repo: Path, tail: str, untrack: bool = True) -> str:
    funcs = "\n".join(_fn(n) for n in (
        "commit_wip", "scrub_test_results", "af_owned_ids", "af_base_ref", "integrate_round",
        "salvage_external_grok_clones", "af_kill_worker_sessions", "af_untrack_loop_lock",
        "af_warn_unmerged_finished")) + "\n" + _fn("af_wait_verify", RECOVERY)
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        WT={repo}; LOG={repo.parent}/loop.log; CONFLICTS={repo.parent}/conflicts; PROJECT=mvpvue-league-core
        AF_ROLLING=1; AF_WORKER_SESSIONS=""; AF_CARRY_IDS="R9"; AF_VERIFY_PID=""
        factory_tree_violation=0; round=1; waited=0; deadline=3600; ids_csv="R1,R2"
        HOME={repo.parent}
        say(){{ echo "$*" | tee -a "$LOG"; }}
        praxis_q(){{ case "$1" in finished_ids) echo "R1 R2";; known_ids) echo "R1 R2 R9";; *) return 1;; esac; }}
        """) + funcs + textwrap.dedent(f"""
        {"af_untrack_loop_lock" if untrack else ""}
        cd "$WT"
        {tail}
        af_warn_unmerged_finished "round #1"
        echo ROUND-END-REACHED-THE-END
        """)


def _run(repo: Path, tail: str, untrack: bool = True):
    script = repo.parent / "harness.sh"
    script.write_text(_harness(repo, tail, untrack))
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)


def test_round_end_tail_merges_both_finished_branches_and_exits_zero(repo: Path):
    res = _run(repo, _tail())
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ROUND-END-REACHED-THE-END" in res.stdout
    for n in (1, 2):
        _git(repo, "merge-base", "--is-ancestor", f"af/R{n}", "HEAD")  # raises if not merged
        assert (repo / "mvpvu" / f"r{n}.txt").exists()
    assert "NOT merged" not in res.stdout


def test_round_end_survives_the_lock_file_even_when_it_is_still_tracked(repo: Path):
    """THE ROOT CAUSE, with the startup untrack step switched off.

    commit_wip saw ` M .af-loop.lock` in `git status`, `git add -A` declined to stage a file outside
    the sparse patterns, and `git commit` exited 1 ("no changes added to commit") under `set -e`.
    """
    res = _run(repo, _tail(), untrack=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ROUND-END-REACHED-THE-END" in res.stdout
    for n in (1, 2):
        _git(repo, "merge-base", "--is-ancestor", f"af/R{n}", "HEAD")


def test_commit_wip_alone_does_not_die_on_a_sparse_excluded_modified_file(repo: Path):
    """commit_wip called BARE under `set -e` (no `||` shield) must return 0 on the same shape."""
    res = _run(repo, "commit_wip", untrack=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ROUND-END-REACHED-THE-END" in res.stdout


def test_loop_lock_file_is_excluded_and_untracked_at_startup(repo: Path):
    res = _run(repo, "true")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _git(repo, "ls-files", "--", ".af-loop.lock").strip() == ""
    assert (repo / ".af-loop.lock").exists()
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".af-loop.lock" in exclude.splitlines()
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_unmerged_finished_branch_is_called_out_loudly(repo: Path):
    res = _run(repo, "AF_KNOWN_IDS=' R1 R2 R9 '; AF_FINISHED_IDS=' R1 R2 '")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "branch af/R1 holds finished ticket(s) [R1] but is NOT merged" in res.stdout
    assert "2 finished-ticket branch(es) remain unintegrated" in res.stdout


def test_failure_is_not_dressed_up_as_a_handoff():
    """rc != 0 must not prepare a handoff unless the operator (or AF_HANDOFF_ON_FAILURE) asks for one."""
    body = _fn("af_cleanup_on_exit")
    assert 'AF_HANDOFF_ON_FAILURE:-0}" = "1"' in body
    assert "--prepare-handoff" in body
