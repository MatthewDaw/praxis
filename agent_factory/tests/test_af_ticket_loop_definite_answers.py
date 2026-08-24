"""A definite answer from Praxis is not an outage.

``praxis_q`` retried EVERY failure five times with linear backoff (~2.5 minutes), and every
exhausted retry fed ``outage()``, which waits 60s and halts the run after ten consecutive passes
as "Praxis unreachable".

That is correct for a blip. It is wrong for an ANSWER. A 403 ("API key is not scoped to org X"),
a 404 ("unknown space Y"), or a partial snapshot reference are the backend telling us the config is
wrong — and it will still be wrong on the fifth attempt, and on the fiftieth. Measured cost of
conflating them: a wrong ``PRAXIS_ORG`` spent ~2.5 minutes per pass, then ten more minutes of
outage waiting, and finally halted with exit 6 blaming a backend that had answered correctly and
instantly every single time. The operator was pointed at the service instead of at the typo.

These tests extract the two SHIPPED shell functions and run them. The behaviour lives in bash, so
asserting on the bash is the only way to assert on the behaviour; a test that read the Python
client alone would not have noticed the driver retrying its answers.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    """The shipped definition of one shell function, from `name(){` to its closing brace."""
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    body = text[start + 1 : end + 3]
    assert body.startswith(f"{name}(){{"), body[:80]
    return body


def _harness(script_body: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped functions under the same `set -euo pipefail` the driver uses.

    ``bump``/``tries`` count through a FILE: ``praxis_q`` invokes its query inside a command
    substitution, so a shell variable incremented by the query lives in a subshell and never
    reaches the assertion. ``sleep`` is stubbed so the retry and outage waits are exercised as
    control flow without spending their wall clock.
    """
    counter = tmp_path / "tries"
    counter.write_text("0")
    preamble = textwrap.dedent(
        f"""
        set -euo pipefail
        say(){{ echo "$*"; }}
        sleep(){{ :; }}
        COUNTER={counter}
        bump(){{ echo $(( $(cat "$COUNTER") + 1 )) > "$COUNTER"; }}
        tries(){{ cat "$COUNTER"; }}
        outages=0
        """
    )
    program = preamble + _function("praxis_q") + "\n" + _function("outage") + "\n" + script_body
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=120)


DEFINITE = [
    "_praxis.PraxisUnreachable: Praxis GET /facts/by -> HTTP 403: API key is not scoped to org 'x'",
    "_praxis.PraxisUnreachable: Praxis GET /facts/by -> HTTP 404: unknown space 'praxis'",
    "_praxis.PraxisUnreachable: Praxis GET /facts/by: partial snapshot reference (space=None)",
]


# ------------------------------------------------------------------------ the definite answers --

@pytest.mark.parametrize("stderr_line", DEFINITE)
def test_a_definite_answer_stops_after_one_attempt(stderr_line: str, tmp_path: Path):
    res = _harness(
        f"""
        q(){{ bump; echo "{stderr_line}" >&2; return 1; }}
        praxis_q q >/dev/null || true
        echo "TRIES=$(tries)"
        echo "FATAL=${{AF_PRAXIS_FATAL:-<empty>}}"
        """,
        tmp_path,
    )
    assert "TRIES=1" in res.stdout, f"a definite answer must not be retried\n{res.stdout}{res.stderr}"
    assert "FATAL=<empty>" not in res.stdout, "the diagnosis must be captured for the operator"


def test_a_definite_answer_halts_loudly_instead_of_being_counted_as_an_outage(tmp_path: Path):
    res = _harness(
        """
        q(){ bump; echo "Praxis GET /facts/by -> HTTP 403: API key is not scoped to org 'bestie'" >&2; return 1; }
        praxis_q q >/dev/null || true
        outage "claimable"
        echo "REACHED-AFTER-OUTAGE"
        """,
        tmp_path,
    )
    assert res.returncode == 6, res.stdout + res.stderr
    assert "MISCONFIGURATION, not an outage" in res.stdout
    assert "not scoped to org" in res.stdout, "the halt must name the actual fault"
    assert "REACHED-AFTER-OUTAGE" not in res.stdout, "outage() must not return on a definite answer"
    assert "outage 1/" not in res.stdout, "a definite answer must never increment the outage streak"


# --------------------------------------------------------------- the transient path is preserved --

def test_a_genuine_transient_still_retries_five_times(tmp_path: Path):
    res = _harness(
        """
        q(){ bump; echo "urlopen error [Errno 111] Connection refused" >&2; return 1; }
        praxis_q q >/dev/null || true
        echo "TRIES=$(tries)"
        echo "FATAL=${AF_PRAXIS_FATAL:-<empty>}"
        """,
        tmp_path,
    )
    assert "TRIES=5" in res.stdout, res.stdout + res.stderr
    assert "FATAL=<empty>" in res.stdout, "a blip must not be mistaken for a misconfiguration"


def test_a_transient_still_waits_and_counts_rather_than_halting(tmp_path: Path):
    """The behaviour being PRESERVED. Riding out a blip is right; the fix must not turn every
    failure into an immediate halt, which would be the same conflation with the sign flipped."""
    res = _harness(
        """
        q(){ bump; echo "Connection reset by peer" >&2; return 1; }
        praxis_q q >/dev/null || true
        outage "claimable"
        outage "claimable"
        echo "STILL-RUNNING outages=$outages"
        """,
        tmp_path,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "STILL-RUNNING outages=2" in res.stdout
    assert "Praxis unreachable (claimable)" in res.stdout


def test_a_long_transient_streak_still_halts_as_an_outage(tmp_path: Path):
    res = _harness(
        """
        AF_MAX_OUTAGES=3
        q(){ bump; echo "timed out" >&2; return 1; }
        praxis_q q >/dev/null || true
        outage "claimable"; outage "claimable"; outage "claimable"
        echo "REACHED"
        """,
        tmp_path,
    )
    assert res.returncode == 6
    assert "Praxis unreachable for 3 consecutive passes" in res.stdout
    assert "REACHED" not in res.stdout


def test_a_recovering_query_returns_its_output_and_leaves_no_fatal(tmp_path: Path):
    res = _harness(
        """
        q(){ bump; if [ "$(tries)" -lt 3 ]; then echo "timed out" >&2; return 1; fi; echo "17"; }
        out=$(praxis_q q)
        echo "OUT=$out TRIES=$(tries) FATAL=${AF_PRAXIS_FATAL:-<empty>}"
        """,
        tmp_path,
    )
    assert "OUT=17" in res.stdout, res.stdout + res.stderr
    assert "TRIES=3" in res.stdout
    assert "FATAL=<empty>" in res.stdout


def test_the_fatal_flag_is_cleared_between_queries(tmp_path: Path):
    """A stale flag would make the NEXT healthy pass halt the run for a fault already fixed."""
    res = _harness(
        """
        bad(){ echo "HTTP 403: nope" >&2; return 1; }
        good(){ echo "5"; }
        praxis_q bad >/dev/null || true
        praxis_q good >/dev/null
        echo "FATAL=${AF_PRAXIS_FATAL:-<empty>}"
        """,
        tmp_path,
    )
    assert "FATAL=<empty>" in res.stdout, res.stdout + res.stderr
