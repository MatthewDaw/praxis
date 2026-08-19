"""Concurrent writers must not lose each other's updates.

Measured on a live campaign: a supervising loop registered trials while an operator command
acknowledged a diagnosis against the same space file. Afterwards the acknowledgement was gone and
four adjudicated trials -- including the campaign's only ADOPTION -- had no record at all, despite
the loop having printed their verdicts. The ledger rows existed; the trials did not.

Nothing errored. A lost update is silent by construction, which is why it survived so long.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _write_space(path: Path) -> None:
    path.write_text(json.dumps({"facts": {}}))


def _add_fact(space_file: Path, n: int) -> int:
    """One process doing a full load-mutate-save cycle, via the real CLI entry point."""
    # The sleep INSIDE the mutation is what makes this a real test. Without it, subprocess startup
    # (~100ms) serialises the writers so their load-mutate-save windows never overlap and the test
    # passes even with the lock removed -- verified, and it is exactly the kind of test that looks
    # like coverage while proving nothing. Holding the section open for 300ms guarantees all ten
    # are inside it simultaneously.
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from knowledge.ml_registry.cli import _load_mutate_save\n"
        "def fn(space):\n"
        "    time.sleep(0.3)\n"
        "    space.insert('idea', {'model_id': 'm', 'origin': 'seeded', 'axis': 'a',\n"
        "                          'description': 'd%d', 'id': 'I%d'})\n"
        "    return 1\n"
        "_load_mutate_save(%r, fn)\n" % (str(REPO), n, n, str(space_file))
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path) -> None:
    """Ten concurrent load-mutate-save cycles must yield ten facts, not fewer.

    Without the lock each process loads the same starting state and the last save wins, so the
    count lands far below ten -- and nothing anywhere reports that writes were discarded.
    """
    space_file = tmp_path / "space.json"
    _write_space(space_file)

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(lambda n: _add_fact(space_file, n), range(10)))
    assert all(c == 0 for c in codes), codes

    facts = json.loads(space_file.read_text())["facts"]
    assert len(facts) == 10, f"lost {10 - len(facts)} update(s) to a write race"


def test_the_lock_file_is_separate_from_the_space_file(tmp_path: Path) -> None:
    """The save replaces the space file, and a lock held on a replaced inode protects nothing."""
    space_file = tmp_path / "space.json"
    _write_space(space_file)
    _add_fact(space_file, 0)
    assert (tmp_path / "space.json.lock").exists()
