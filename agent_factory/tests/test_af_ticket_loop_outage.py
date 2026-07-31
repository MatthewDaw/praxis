"""A transient Praxis failure must not silently kill the ticket loop.

The driver runs under `set -euo pipefail` and reads Praxis through helpers that swallow their own
stderr. Before `praxis_q`, every read was a bare `var=$(query)`, so one non-zero exit from the
python — an API 5xx, a DNS blip, a reset connection — aborted the whole script and wrote NOTHING to
the log, because stderr was already redirected to /dev/null. That is how the appeal_engine run died
on 2026-07-31: last line "round #3 progress: 3/4 finished", then no error, no signal, no OOM, and an
orphaned tmux session left burning for hours.

The `${var:-default}` fallbacks written at each call site were meant to survive exactly this, and
could never run: `set -e` fires on the assignment before the parameter expansion is reached.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# The reads that go over the network. Each is defined as `name(){` in the driver.
QUERY_FNS = ("claimable", "ready_batch", "finished_count", "batch_open")


def _extract(fn: str) -> str:
    """Return the shell source of one top-level function, so it can be exercised in isolation."""
    src = SCRIPT.read_text()
    start = src.index(f"{fn}(){{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _run(body: str, backoff: str = "0") -> subprocess.CompletedProcess[str]:
    script = f"set -euo pipefail\nexport AF_QUERY_BACKOFF_S={backoff}\n{_extract('praxis_q')}\n{body}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )


def test_bare_assignment_is_what_killed_the_loop():
    """Characterize the original bug, so the fix is never 'simplified' back into it."""
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail\nq(){ return 1; }\nx=$(q); x=${x:-FALLBACK}\necho reached"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "reached" not in r.stdout  # the ${x:-FALLBACK} guard never got a chance to run
    assert r.stderr == ""  # and it died completely silently


def test_failing_query_does_not_abort_the_caller():
    r = _run("q(){ return 1; }\nif ! out=$(praxis_q q); then echo 'handled'; fi\necho reached")
    assert r.returncode == 0, r.stderr
    assert "handled" in r.stdout
    assert "reached" in r.stdout


def test_failing_query_reports_failure_rather_than_a_wrong_answer():
    """The caller must be able to tell 'Praxis is down' from 'Praxis says zero'.

    Read the status the way every real call site does — inside an `if` — because a bare failing
    command still aborts under `set -e`, and that is correct: the guard belongs at the call site.
    """
    r = _run("q(){ return 1; }\nif praxis_q q; then s=0; else s=$?; fi\necho \"status=$s\"")
    assert "status=1" in r.stdout, r.stdout


def test_empty_output_with_success_is_a_real_answer():
    """`ready_batch` prints nothing for a genuine dependency stall; that must not read as an outage,
    or a true stall would retry forever instead of halting loudly."""
    r = _run("q(){ printf ''; }\nif praxis_q q; then echo 'success'; else echo 'wrongly-failed'; fi")
    assert "success" in r.stdout, r.stdout


def test_transient_failure_is_retried_and_then_succeeds():
    body = (
        'tick="$(mktemp)"\n'
        'q(){ n=$(cat "$tick"); n=$((n+1)); echo "$n" > "$tick"; [ "$n" -ge 3 ] || return 1; echo 7; }\n'
        'echo 0 > "$tick"\n'
        'out=$(praxis_q q)\n'
        'echo "out=$out attempts=$(cat "$tick")"\n'
    )
    r = _run(body)
    assert "out=7 attempts=3" in r.stdout, r.stdout + r.stderr


def test_stdout_is_passed_through_unchanged():
    r = _run("q(){ echo 'A B C'; }\nout=$(praxis_q q)\necho \"[$out]\"")
    assert "[A B C]" in r.stdout, r.stdout


@pytest.mark.parametrize("fn", QUERY_FNS)
def test_every_praxis_read_goes_through_praxis_q(fn):
    """A bare `var=$(query)` anywhere is the bug returning. Assignments are the lethal shape: `set -e`
    aborts on them, whereas the same call inside an `if` or followed by `||` is safe."""
    offenders = [
        line.strip()
        for line in SCRIPT.read_text().splitlines()
        if re.search(rf"^\s*[a-z_]+=\$\(\s*{fn}\b", line)
    ]
    assert offenders == [], f"unguarded Praxis read(s): {offenders}"
