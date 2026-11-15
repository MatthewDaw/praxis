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
    assert "Tickets just merged: $ids_csv.${scope_note:-}${ff_note:-}" in SRC
    assert "if af_round_is_fast_forward \"$premerge\" \"$@\"; then" in SRC
    assert "ran_at LATER than this tip's commit time" in SRC
    # The skip covers ONLY gates the worker ran: a pinned set follows tags, not touched code (mvpvue T06).
    assert "ONLY IF that exact command is one of the ticket's pinned validations" in SRC
    assert "Every other whole-repo gate" in SRC
    assert "a ticket's pinned set follows its tags, not the code it touched" in SRC


def test_workers_run_final_checks_on_a_clean_checkout() -> None:
    assert 'SWEEP_AMENDMENT="$SWEEP_AMENDMENT$CLEAN_CHECKOUT_RULE"' in SRC
    assert "git worktree add --detach <a scratch dir> HEAD" in SRC


def test_the_wall_clock_only_halts_when_not_set_to_warn() -> None:
    assert 'if [ "$ticket_wall_clock_hit" = "1" ] && [ "${AF_TICKET_DEADLINE_ACTION:-halt}" != "warn" ]' in SRC
    start = SRC.index('    if [ "$waited" -ge "$ticket_deadline" ]')
    end = SRC.index('    # Neither the finished count', start)
    block = SRC[start:end]
    for action, expected in [("warn", "continued:0:1"), ("halt", "stopped:1:0")]:
        program = "\n".join([
            "set -eu", "waited=120; ticket_deadline=60; ticket_deadline_warned=0",
            "ticket_wall_clock_hit=0; ids_csv=T01", "praxis_q(){ echo T01; }",
            "say(){ :; }", "AF_TICKET_DEADLINE_ACTION=" + action,
            "result=stopped", "for poll in 1 2; do", block,
            "result=continued", "done",
            'echo "$result:$ticket_wall_clock_hit:$ticket_deadline_warned"',
        ])
        result = subprocess.run(["bash", "-c", program], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


def test_active_round_extends_but_idle_or_hard_capped_round_does_not() -> None:
    cases = [
        ("warn 3600 2700 1 0", True),   # active agent, no tool child
        ("warn 3600 2700 0 1", True),   # quiet pane, long-running tool
        ("warn 3600 2700 0 0", False),  # no activity: never extend
        ("halt 2700 2700 1 1", False),  # explicit hard limit
        ("halt 0 2700 0 1", True),
    ]
    for args, expected in cases:
        result = subprocess.run(
            ["bash", "-c", _function("af_round_can_extend") + "\naf_round_can_extend " + args],
            capture_output=True, text=True,
        )
        assert (result.returncode == 0) == expected, args


def test_solo_codex_worker_has_no_polling_coordinator() -> None:
    program = '\n'.join([
        'PROJECT=demo; AF_WORKTREE_ROOT=/tmp/workers; INTEGRATION_REF=build',
        'AF_FACTORY_CHECKOUT=/tmp/factory; PY=python; SWEEP_AMENDMENT=" gates"',
        'PREEXISTING_RULE=" baseline"; SERVICES=" services"',
        _function('af_worker_prompt'), 'af_worker_prompt R001',
    ])
    result = subprocess.run(['bash', '-c', program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'build exactly this ONE ticket yourself, inline' in result.stdout
    assert 'FROM build' in result.stdout
    assert ' gates baseline services' in result.stdout
    assert 'HOLD the turn open by polling' not in result.stdout
    assert 'spawn ONE worktree-isolated subagent' not in result.stdout
    native = _function('af_native_fanout')
    supported = subprocess.run(['bash', '-c', 'BACKEND=codex\n' + native + '\naf_native_fanout'])
    assert supported.returncode == 0
    assert 'if af_native_fanout; then\n    # Driver-side fan-out' in SRC


def test_fast_lane_preserves_dependents_required_checks_and_soft_budget() -> None:
    assert 'changed modules AND transitive callers/dependents' in SRC
    assert 'no hard cutoff and no automatic pass for an unfinished check' in SRC
    assert 'explicitly pinned checks still run here' in SRC
    assert 'Hard wall budget:' not in SRC
    assert 'phase-gate lookup unavailable; using full validation' in SRC


def test_advisory_dependencies_are_independent_of_other_never_stop_switches() -> None:
    for advisory, expected in [('1', 0), ('0', 1)]:
        program = 'AF_NEVER_STOP=0; AF_DEPENDENCIES_ADVISORY=' + advisory + '\n' + _function('af_dependencies_advisory') + '\naf_dependencies_advisory'
        result = subprocess.run(['bash', '-c', program], capture_output=True, text=True)
        assert result.returncode == expected


def test_native_worker_heartbeat_samples_real_sessions_instead_of_absent_coordinator() -> None:
    program = '\n'.join([
        'worker_cpu(){ case "$1" in one) echo 2;; two) echo 3;; esac; }',
        _function('af_hb_cpu_sample'), 'af_hb_cpu_sample "one two"',
    ])
    result = subprocess.run(['bash', '-c', program], capture_output=True, text=True, check=True)
    assert result.stdout == '5'
    assert 'sess="${AF_HB_SESSION:-${AF_WORKER_SESSIONS:-${SESSION:-}}}"' in SRC
