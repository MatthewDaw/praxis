"""Contention on the space lock must be distinguishable from a hang.

``supervise-campaign`` holds the exclusive ``<space>.lock`` for the WHOLE campaign -- forty
dispatches, potentially hours. Any other command against the same space file used to block on a
plain blocking ``flock`` with no timeout, no output and no way to tell "waiting on a lock held by
that campaign" apart from "crashed". That ambiguity is the defect; a bounded wait that names the
holder is the fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _hold_the_lock(space_file: Path, seconds: float) -> subprocess.Popen:
    """A second process inside the locked section, exactly as a live campaign would be."""
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from knowledge.ml_registry.cli import _load_mutate_save\n"
        "print('held', flush=True)\n"
        "_load_mutate_save(%r, lambda space: time.sleep(%r))\n"
        % (str(REPO), str(space_file), seconds)
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    proc.stdout.readline()  # the holder is inside the section once this line arrives
    return proc


def test_a_contended_lock_times_out_loudly_instead_of_blocking_forever(tmp_path: Path) -> None:
    """A waiter must give up within the configured budget and say what it was waiting for."""
    space_file = tmp_path / "space.json"
    space_file.write_text(json.dumps({"facts": {}}))
    holder = _hold_the_lock(space_file, 30.0)
    env = {**os.environ, "PRAXIS_DB_DISABLED": "1", "ML_REGISTRY_LOCK_TIMEOUT": "1"}
    idea = json.dumps({"model_id": "m", "origin": "seeded", "axis": "a",
                       "description": "d", "id": "I0"})
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from knowledge.ml_registry.cli import main\n"
        "sys.exit(main(['register-idea', '--space-file', %r, '--meta-json', %r]))\n"
        % (str(REPO), str(space_file), idea)
    )
    try:
        started = time.monotonic()
        # A blocking flock never returns while the holder sleeps, so before the fix this raises
        # TimeoutExpired -- the test FAILS rather than hanging, which is the point.
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env=env, timeout=15)
        waited = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()

    assert done.returncode == 1, done
    assert waited < 10, f"waited {waited:.1f}s for a 1s budget"
    err = done.stderr
    assert "space.json.lock" in err, err
    assert "another process" in err.lower(), err
    assert str(holder.pid) in err, err
    assert "1" in err and "s" in err, err
    assert "separate space file" in err, err


def test_the_lock_timeout_is_generous_by_default(tmp_path: Path) -> None:
    """An unset budget must not trip on any legitimate short mutation."""
    from knowledge.ml_registry.cli import _lock_timeout_seconds

    assert _lock_timeout_seconds() >= 300
