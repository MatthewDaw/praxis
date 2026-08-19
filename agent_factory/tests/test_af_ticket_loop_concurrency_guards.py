"""Concurrency guards in `af-ticket-loop.sh` for MANY loops on one box: the shared-Postgres-port
refusal, the project-keyed watch-stop sentinel, and the round-level stall heartbeat.

Same idiom as `test_af_ticket_loop_worktree_guard.py` — there is no module to import, so these
tests READ THE SHIPPED SCRIPT, extract the exact bytes between each block's markers, and EXECUTE
them. Weakening the driver fails the test; a unit test of a reimplementation would not notice.

All three defects were measured on the devbox 2026-08-19:
  1. /workspace/hudl-cv-download (hudl-cv-download) and /workspace/sports_analysis
     (mvpvu-data-collection) were BOTH live and BOTH declared Postgres on port 5438.
  2. The stop sentinel was keyed on the worktree basename alone, so it stopped every project in
     that tree — and three stale ones from Aug 4/Aug 13 sat waiting to kill the next watch run.
  3. A round sat with `make check-fast` asleep at 0.0% CPU for 10+ minutes with no log line.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def block(name: str) -> str:
    m = re.search(
        rf"# --- BEGIN {name} ---\n(.*?)\n# --- END {name} ---\n", SCRIPT.read_text(), re.S
    )
    assert m, f"the '{name}' markers are gone from af-ticket-loop.sh"
    return m.group(1)


def run(script: Path, env: dict | None = None, timeout: int = 60):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=timeout, env=e
    )


# --------------------------------------------------------------- defect 1: shared Postgres port --


def worktree(root: Path, name: str, project: str, db_url: tuple[str, str] | None) -> Path:
    wt = root / name
    (wt / ".claude").mkdir(parents=True)
    env = {"FACTORY_PROJECT": project}
    if db_url is not None:
        env[db_url[0]] = db_url[1]
    (wt / ".claude" / "settings.local.json").write_text(json.dumps({"env": env}))
    return wt


def db_runner(tmp_path: Path, wt: Path, project: str, tag: str = "", tail: str = "") -> Path:
    """The worktree guard (which defines af_guard_die, AF_LOCK, the trap) then the db port guard."""
    path = tmp_path / f"run-{tag or project}.sh"
    path.write_text(
        "set -euo pipefail\n"
        'PY="${PY:-python3}"\n'
        f'PROJECT="{project}"\nWT="{wt}"\n'
        + block("worktree guard")
        + "\n"
        + block("db port guard")
        + "\necho PAST_DB_GUARD\n"
        + tail
        + "\n"
    )
    return path


def test_same_port_with_a_live_holder_is_refused_naming_both_projects(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    a = worktree(root, "hudl-cv-download", "hudl-cv-download", ("PRAXIS_DB_URL", "postgresql://u@localhost:5438/x"))
    b = worktree(root, "sports_analysis", "mvpvu-data-collection", ("SPORTS_ANALYSIS_DB_URL", "postgres://u@localhost:5438/y"))
    holder = subprocess.Popen(
        ["bash", str(db_runner(tmp_path, a, "hudl-cv-download", tail="for _ in $(seq 200); do sleep 0.2; done"))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(300):
            if (a / ".af-loop.lock").exists():
                break
            time.sleep(0.05)
        assert (a / ".af-loop.lock").exists(), "the holder never took its lock"
        p = run(db_runner(tmp_path, b, "mvpvu-data-collection", tag="second"), {"AF_LOCK_SCAN_ROOT": str(root)})
        assert p.returncode != 0
        assert "PAST_DB_GUARD" not in p.stdout
        for expected in ("5438", "hudl-cv-download", "mvpvu-data-collection", str(holder.pid), str(a), str(b)):
            assert expected in p.stderr, expected
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)


def test_same_port_with_a_dead_holder_pid_proceeds(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    a = worktree(root, "other", "other-project", ("DATABASE_URL", "postgresql://u@localhost:5438/x"))
    b = worktree(root, "mine", "mine-project", ("POSTGRES_URL", "postgresql://u@localhost:5438/y"))
    dead = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
    (a / ".af-loop.lock").write_text(f"{dead} other-project 5438\n")
    p = run(db_runner(tmp_path, b, "mine-project"), {"AF_LOCK_SCAN_ROOT": str(root)})
    assert p.returncode == 0, p.stderr
    assert "PAST_DB_GUARD" in p.stdout


def test_different_ports_proceed(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    a = worktree(root, "other", "other-project", ("DATABASE_URL", "postgresql://u@localhost:5434/x"))
    b = worktree(root, "mine", "mine-project", ("PRAXIS_DB_URL", "postgresql://u@localhost:5438/y"))
    holder = subprocess.Popen(
        ["bash", str(db_runner(tmp_path, a, "other-project", tail="for _ in $(seq 200); do sleep 0.2; done"))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(300):
            if (a / ".af-loop.lock").exists():
                break
            time.sleep(0.05)
        p = run(db_runner(tmp_path, b, "mine-project", tag="second"), {"AF_LOCK_SCAN_ROOT": str(root)})
        assert p.returncode == 0, p.stderr
        assert "PAST_DB_GUARD" in p.stdout
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)


def test_af_allow_shared_db_proceeds_and_logs_the_override(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    a = worktree(root, "other", "other-project", ("DATABASE_URL", "postgresql://u@localhost:5438/x"))
    b = worktree(root, "mine", "mine-project", ("PRAXIS_DB_URL", "postgresql://u@localhost:5438/y"))
    holder = subprocess.Popen(
        ["bash", str(db_runner(tmp_path, a, "other-project", tail="for _ in $(seq 200); do sleep 0.2; done"))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(300):
            if (a / ".af-loop.lock").exists():
                break
            time.sleep(0.05)
        p = run(
            db_runner(tmp_path, b, "mine-project", tag="second"),
            {"AF_LOCK_SCAN_ROOT": str(root), "AF_ALLOW_SHARED_DB": "1"},
        )
        assert p.returncode == 0, p.stderr
        assert "PAST_DB_GUARD" in p.stdout
        assert "AF_ALLOW_SHARED_DB=1" in p.stderr and "5438" in p.stderr
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)


def test_a_refusal_still_releases_the_worktree_lock(tmp_path):
    """Every new failure path rides the trap from 5a735d5 — a refused loop must not brick its tree."""
    root = tmp_path / "workspace"
    root.mkdir()
    a = worktree(root, "other", "other-project", ("DATABASE_URL", "postgresql://u@localhost:5438/x"))
    b = worktree(root, "mine", "mine-project", ("PRAXIS_DB_URL", "postgresql://u@localhost:5438/y"))
    holder = subprocess.Popen(
        ["bash", str(db_runner(tmp_path, a, "other-project", tail="for _ in $(seq 200); do sleep 0.2; done"))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        for _ in range(300):
            if (a / ".af-loop.lock").exists():
                break
            time.sleep(0.05)
        p = run(db_runner(tmp_path, b, "mine-project", tag="second"), {"AF_LOCK_SCAN_ROOT": str(root)})
        assert p.returncode != 0
        assert not (b / ".af-loop.lock").exists()
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)


# ------------------------------------------------------------------- defect 2: the stop sentinel --


def stop_runner(tmp_path: Path, wt: Path, project: str, tag: str) -> Path:
    path = tmp_path / f"stop-{tag}.sh"
    path.write_text(
        "set -euo pipefail\n"
        f'PROJECT="{project}"\nWT="{wt}"\n'
        + block("watch stop sentinel")
        + "\n"
        'if af_watch_stopped; then echo "STOPPED $WATCH_STOP_HIT"; else echo NOT_STOPPED; fi\n'
    )
    return path


def test_the_sentinel_stops_only_its_own_project(tmp_path):
    root = tmp_path / "workspace"
    wt = root / "sports_analysis"
    wt.mkdir(parents=True)
    (root / "af-watch-stop-mvpvu-data-collection@sports_analysis").write_text("")
    stopped = run(stop_runner(tmp_path, wt, "mvpvu-data-collection", "a"))
    assert "STOPPED" in stopped.stdout, stopped.stderr
    # The OTHER project sharing that exact worktree keeps running — the whole point.
    other = run(stop_runner(tmp_path, wt, "mvpvu-foundation", "b"))
    assert "NOT_STOPPED" in other.stdout, other.stderr


def test_a_sentinel_older_than_the_ttl_is_ignored_and_the_ignore_is_logged(tmp_path):
    root = tmp_path / "workspace"
    wt = root / "sotos"
    wt.mkdir(parents=True)
    sentinel = root / "af-watch-stop-sotos-project@sotos"
    sentinel.write_text("")
    two_weeks_ago = time.time() - 14 * 86400
    os.utime(sentinel, (two_weeks_ago, two_weeks_ago))
    p = run(stop_runner(tmp_path, wt, "sotos-project", "ttl"))
    assert "NOT_STOPPED" in p.stdout, p.stderr
    assert "IGNORING STALE" in p.stdout and str(sentinel) in p.stdout
    # ...and it IS honoured while it is fresh, so the TTL is a staleness rule, not a disablement.
    os.utime(sentinel, None)
    fresh = run(stop_runner(tmp_path, wt, "sotos-project", "ttl2"))
    assert "STOPPED" in fresh.stdout, fresh.stderr


def test_the_legacy_basename_only_sentinel_still_stops_and_logs_a_deprecation(tmp_path):
    root = tmp_path / "workspace"
    wt = root / "sports_analysis"
    wt.mkdir(parents=True)
    legacy = root / "af-watch-stop-sports_analysis"
    legacy.write_text("")
    p = run(stop_runner(tmp_path, wt, "mvpvu-data-collection", "legacy"))
    assert "STOPPED" in p.stdout, p.stderr
    assert str(legacy) in p.stdout
    assert "DEPRECATED" in p.stdout
    assert "af-watch-stop-mvpvu-data-collection@sports_analysis" in p.stdout


# ------------------------------------------------------------ defect 3: the round stall heartbeat --


def heartbeat_runner(tmp_path: Path, tag: str, body: str) -> Path:
    path = tmp_path / f"hb-{tag}.sh"
    path.write_text(
        "set -euo pipefail\n"
        # the heartbeat logs through af_watch_stop_say, which lives in the sentinel block
        f'PROJECT="p"\nWT="{tmp_path}/wt"\n'
        + block("watch stop sentinel")
        + "\n"
        + block("round stall heartbeat")
        + "\n"
        + body
        + "\n"
    )
    return path


def test_stall_warning_fires_after_the_quiet_interval(tmp_path):
    body = (
        'af_round_heartbeat 7 "3/2" "TIC-1,TIC-2"\n'
        "sleep 2\n"
        'af_round_heartbeat 7 "3/2" "TIC-1,TIC-2"\n'
    )
    p = run(heartbeat_runner(tmp_path, "fires", body), {"AF_ROUND_QUIET_WARN_S": "1"})
    assert p.returncode == 0, p.stderr
    assert "STALL WARNING round #7" in p.stdout
    assert "TIC-1,TIC-2" in p.stdout


def test_stall_warning_does_not_fire_when_progress_occurs(tmp_path):
    body = (
        'af_round_heartbeat 7 "3/2" "TIC-1,TIC-2"\n'
        "sleep 2\n"
        'af_round_heartbeat 7 "4/1" "TIC-2"\n'
        "sleep 2\n"
        'af_round_heartbeat 7 "5/0" ""\n'
    )
    p = run(heartbeat_runner(tmp_path, "quiet", body), {"AF_ROUND_QUIET_WARN_S": "1"})
    assert p.returncode == 0, p.stderr
    assert "STALL WARNING" not in p.stdout


def test_the_wait_loop_actually_calls_the_heartbeat(tmp_path):
    """The block existing is not enough — it has to be wired into the round wait."""
    text = SCRIPT.read_text()
    assert 'af_round_heartbeat "$round" "$now/$open" "$ids_csv"' in text
    # and the sentinel checks go through the TTL/legacy-aware helper, not a bare -f test
    assert '[ -f "$WATCH_STOP" ]' not in text
    assert text.count("af_watch_stopped &&") == 3
