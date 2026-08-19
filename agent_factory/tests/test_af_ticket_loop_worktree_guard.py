"""The startup guard in `af-ticket-loop.sh`: argv must agree with the worktree's own
`FACTORY_PROJECT`, and only ONE loop may run per worktree.

Same idiom as `test_af_ticket_loop_seam.py`: there is no module to import — the logic lives in the
shell driver — so these tests READ THE SHIPPED SCRIPT, extract the exact bytes between the guard's
markers, and EXECUTE them. Deleting or weakening the guard fails these tests; a unit test of
anything else would not notice.

The failure this locks down (observed 2026-08-19 on the devbox): a loop was started for
`mvpvu-data-collection` on a worktree a *live* `mvpvu-foundation` loop was already using, and
repointed that worktree's settings. The older loop kept passing `mvpvu-foundation` in argv while its
hooks resolved `prd-mvpvu-data-collection` — so its completeness gate went inert rather than loud.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

_GUARD_RE = re.compile(
    r"# --- BEGIN worktree guard ---\n(.*?)\n# --- END worktree guard ---\n", re.S
)


def guard_body() -> str:
    m = _GUARD_RE.search(SCRIPT.read_text())
    assert m, "the worktree guard's markers are gone from af-ticket-loop.sh"
    return m.group(1)


def make_worktree(tmp_path: Path, factory_project: str | None) -> Path:
    wt = tmp_path / "wt"
    (wt / ".claude").mkdir(parents=True)
    env = {"PRAXIS_ORG": "o", "PRAXIS_API_KEY": "k", "PRAXIS_API_BASE_URL": "u"}
    if factory_project is not None:
        env["FACTORY_PROJECT"] = factory_project
    (wt / ".claude" / "settings.local.json").write_text(json.dumps({"env": env}))
    return wt


def runner(tmp_path: Path, wt: Path, project: str, tail: str = "") -> Path:
    path = tmp_path / f"run-{abs(hash(tail)) % 10**6}.sh"
    path.write_text(
        "set -euo pipefail\n"
        'PY="${PY:-python3}"\n'
        f'PROJECT={project!r}\nWT={str(wt)!r}\n'.replace("'", '"')
        + guard_body()
        + "\necho PAST_VALIDATION\n"
        + tail
        + "\n"
    )
    return path


def run(script: Path, **kw):
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=60, **kw
    )


def test_matching_project_and_settings_proceeds_past_validation(tmp_path):
    wt = make_worktree(tmp_path, "mvpvu-foundation")
    p = run(runner(tmp_path, wt, "mvpvu-foundation"))
    assert p.returncode == 0, p.stderr
    assert "PAST_VALIDATION" in p.stdout
    assert "held by pid" in p.stderr
    # The lock is released on exit, so the next loop is not locked out by a finished one.
    assert not (wt / ".af-loop.lock").exists()


def test_mismatch_exits_nonzero_and_names_both_projects(tmp_path):
    wt = make_worktree(tmp_path, "mvpvu-data-collection")
    p = run(runner(tmp_path, wt, "mvpvu-foundation"))
    assert p.returncode != 0
    assert "PAST_VALIDATION" not in p.stdout
    assert "mvpvu-foundation" in p.stderr and "mvpvu-data-collection" in p.stderr
    assert str(wt / ".claude" / "settings.local.json") in p.stderr
    # It must NOT auto-patch the shared config: that is how the live loop got poisoned.
    settings = json.loads((wt / ".claude" / "settings.local.json").read_text())
    assert settings["env"]["FACTORY_PROJECT"] == "mvpvu-data-collection"


def test_missing_key_is_a_hard_stop_naming_what_is_absent(tmp_path):
    wt = make_worktree(tmp_path, None)
    p = run(runner(tmp_path, wt, "mvpvu-foundation"))
    assert p.returncode != 0
    assert "FACTORY_PROJECT" in p.stderr


def test_missing_settings_file_is_a_hard_stop(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    p = run(runner(tmp_path, wt, "mvpvu-foundation"))
    assert p.returncode != 0
    assert "settings.local.json" in p.stderr


def test_a_second_concurrent_loop_on_the_same_worktree_is_refused(tmp_path):
    wt = make_worktree(tmp_path, "mvpvu-foundation")
    holder_script = runner(tmp_path, wt, "mvpvu-foundation", tail="for _ in $(seq 150); do sleep 0.2; done")
    holder = subprocess.Popen(
        ["bash", str(holder_script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        lock = wt / ".af-loop.lock"
        for _ in range(200):
            if lock.exists():
                break
            time.sleep(0.05)
        assert lock.exists(), "the holder never took the lock"
        # Same project, same worktree: refused on the lock. (A DIFFERENT project on this worktree
        # is refused even earlier, by the argv/settings check above — see the mismatch test.)
        p = run(runner(tmp_path, wt, "mvpvu-foundation", tail="# second"))
        assert p.returncode != 0
        assert "PAST_VALIDATION" not in p.stdout
        assert str(holder.pid) in p.stderr
        assert "mvpvu-foundation" in p.stderr
        assert "ALREADY RUNNING" in p.stderr
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)
    # The holder's trap released the lock on SIGTERM — a killed loop must not brick the worktree.
    assert not (wt / ".af-loop.lock").exists()


def test_a_stale_lock_whose_pid_is_dead_is_reclaimed_automatically(tmp_path):
    wt = make_worktree(tmp_path, "mvpvu-foundation")
    dead = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True).stdout.strip()
    with pytest.raises(ProcessLookupError):
        os.kill(int(dead), 0)
    (wt / ".af-loop.lock").write_text(f"{dead} mvpvu-foundation\n")
    p = run(runner(tmp_path, wt, "mvpvu-foundation"))
    assert p.returncode == 0, p.stderr
    assert "PAST_VALIDATION" in p.stdout
    assert "reclaiming stale lock" in p.stderr
