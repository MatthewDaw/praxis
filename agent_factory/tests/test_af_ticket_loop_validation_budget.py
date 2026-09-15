"""The hour-per-ticket target is met by removing REDUNDANT validation, never by crashing the run.

Owner, 2026-09-15: the limit is a goal, not something to enforce; exceeding it must not stop the build.
So AF_TICKET_DEADLINE_ACTION=warn turns the ticket wall clock into a warning, and a one-ticket
fast-forward round skips the post-merge re-run of gates its worker already ran on a clean checkout of
the identical tip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
SRC = SCRIPT.read_text()


def _function(name: str) -> str:
    start = SRC.index(f"\n{name}(){{")
    end = SRC.index("\n}\n", start)
    return SRC[start + 1 : end + 3]


def _sh(cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True)


def _repo(tmp: Path) -> Path:
    r = tmp / "r"
    r.mkdir(parents=True)
    assert _sh("git init -q -b build && git config user.email t@t && git config user.name t "
               "&& echo a > a && git add a && git commit -q -m base", r).returncode == 0
    return r


def _ff(repo: Path, premerge: str, *ids: str) -> bool:
    prog = f"WT={repo}\n" + _function("af_round_is_fast_forward") + f"\naf_round_is_fast_forward {premerge} " + " ".join(ids)
    return _sh(prog, repo).returncode == 0


def test_a_linear_one_ticket_round_is_a_fast_forward(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _sh("git rev-parse HEAD", repo).stdout.strip()
    _sh("echo b > b && git add b && git commit -q -m 'feat: b (T09)'", repo)
    assert _ff(repo, base, "T09")


def test_a_merge_commit_or_two_tickets_is_not(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _sh("git rev-parse HEAD", repo).stdout.strip()
    _sh("git checkout -q -b side && echo s > s && git add s && git commit -q -m 's (T09)' "
        "&& git checkout -q build && echo m > m && git add m && git commit -q -m m "
        "&& git merge -q --no-edit side", repo)
    assert not _ff(repo, base, "T09")
    linear = _repo(tmp_path / "x")
    lb = _sh("git rev-parse HEAD", linear).stdout.strip()
    _sh("echo c > c && git add c && git commit -q -m 'c (T09)'", linear)
    assert not _ff(linear, lb, "T09", "T10")


def test_the_verifier_is_told_to_skip_only_on_a_fast_forward() -> None:
    assert "Tickets just merged: $ids_csv.${scope_note:-}${ff_note:-} Each was built" in SRC
    assert "if af_round_is_fast_forward \"$premerge\" \"$@\"; then" in SRC
    assert "ran_at LATER than this tip's commit time" in SRC


def test_workers_run_final_checks_on_a_clean_checkout() -> None:
    assert 'SWEEP_AMENDMENT="$SWEEP_AMENDMENT$CLEAN_CHECKOUT_RULE"' in SRC
    assert "git worktree add --detach <a scratch dir> HEAD" in SRC


def test_the_wall_clock_only_halts_when_not_set_to_warn() -> None:
    assert 'if [ "$ticket_wall_clock_hit" = "1" ] && [ "${AF_TICKET_DEADLINE_ACTION:-halt}" != "warn" ]; then' in SRC
    assert "(AF_TICKET_DEADLINE_ACTION=warn) — ending this round and CONTINUING the run" in SRC
