"""A busy worker must never be reported as frozen.

`worktree_recently_written` is the driver's answer to "is this worker actually making progress",
and it exists because the CPU test lies: a worker that spends 48 minutes polling a log file emits
hundreds of thousands of tokens while its own process tree looks idle. The comment above the
function records exactly that — a COV-1B worker reported not-busy and its round reaped as frozen.

The replacement measured file mtimes instead, which is the right signal. It then threw the answer
away:

    find "$wt" ... -mmin "-$mins" -print 2>/dev/null | head -1 | grep -q . && return 0

`head -1` exits after the first line and SIGPIPEs `find`. The driver runs under `set -o pipefail`,
so the pipeline reports find's 141 — and the function answers "NO recent writes" in precisely the
case where a file WAS found. It could only ever return true when find produced nothing to
truncate, which is the empty case. The detector written to stop healthy workers being reaped was
itself reaping healthy workers.

That is the general shape of defect class "it fails only on the box": treat it as a capacity or
plumbing hypothesis before blaming the worker. Here the plumbing was the whole of it.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _ask(wt: Path, mins: int = 10) -> bool:
    """Run the SHIPPED function under the same options the driver uses."""
    program = (
        textwrap.dedent(
            """
            set -euo pipefail
            say(){ echo "$*"; }
            """
        )
        + _function("worktree_recently_written")
        + f'\nif worktree_recently_written "{wt}" {mins}; then echo YES; else echo NO; fi\n'
    )
    res = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip() == "YES"


def test_a_worktree_that_was_just_written_reads_as_busy(tmp_path: Path):
    """THE REGRESSION. A freshly written file is exactly the case the old pipeline got wrong."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "mod.py").write_text("the worker is working\n")

    assert _ask(wt) is True


def test_a_realistically_sized_worktree_still_reads_as_busy(tmp_path: Path):
    """THE DISCRIMINATING CASE, and the reason the bug was invisible in small fixtures.

    The inversion needs `find` to still be writing when `head -1` exits, so it depends on how much
    find has to say. Measured on this box: 1 file and 50 files answer "busy" under both the old
    pipeline and the new capture; 2000 and 20000 answer FROZEN under the old one. A checkout has
    far more than 2000 files, so in production the old detector answered frozen essentially always
    — while every test small enough to write by hand said it worked.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    for i in range(3000):
        (wt / f"f{i}.txt").write_text("x")

    assert _ask(wt) is True


def test_an_idle_worktree_reads_as_idle(tmp_path: Path):
    """The detector must stay capable of saying NO, or the stall break never fires at all."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "old.txt").write_text("stale")
    old = 60 * 60 * 24
    import os
    os.utime(wt / "old.txt", (0, 0))
    os.utime(wt, (0, 0))

    assert _ask(wt, mins=1) is False


def test_git_plumbing_does_not_count_as_progress(tmp_path: Path):
    """.git mtimes move for reasons unrelated to a worker writing code."""
    wt = tmp_path / "wt"
    (wt / ".git" / "refs").mkdir(parents=True)
    (wt / ".git" / "refs" / "HEAD").write_text("ref: refs/heads/x")

    assert _ask(wt) is False


def test_a_missing_worktree_is_not_busy(tmp_path: Path):
    assert _ask(tmp_path / "nope") is False


def test_the_answer_is_captured_before_it_is_tested():
    """Pinning the shape: the moment this goes back through a pipeline whose last stage can exit
    early, pipefail silently inverts it again."""
    body = _function("worktree_recently_written")
    assert "| head -1 | grep -q ." not in body
    assert "recent=$(" in body, "capture, then test"
