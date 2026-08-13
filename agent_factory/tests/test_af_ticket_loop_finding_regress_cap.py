"""BUG E — the finding_guard zero-commit regress streak must be BOUNDED.

Live incident: a post-merge verification finding whose defect an EARLIER round's commit had already
fixed produced a rebuild with zero commits (correctly — nothing left to change). finding_guard
regressed the ticket for "closing a finding by changing nothing", the pair ping-ponged, and because
the finding carried ``check_id=None`` the check_id-keyed auto-suspend could never fire — so the same
ticket was re-dispatched every ~9 minutes FOREVER.

Like the seam tests, this does NOT import the loop's logic (there is no such module — it lives in a
``python - <<'PYEOF'`` heredoc inside the shell script). It READS THE SHIPPED SCRIPT, extracts the
exact finding_guard block, and EXECUTES it against instrumented modules across successive rounds,
asserting the streak caps at AF_FINDING_REGRESS_MAX regressions and then escalates instead of looping.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# IMPORT ORDER IS LOAD-BEARING — see the long note in test_af_ticket_loop_seam.py. Canonicalize the
# seam via agent_factory FIRST, so the bare names below resolve to the same module objects the
# exec'd block imports (and that these monkeypatches therefore actually patch).
import agent_factory  # noqa: F401,E402

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)

# A marker unique to the finding_guard block (its zero-commit-streak reset helper).
_MARKER = "def _forget(rid):"


def _block() -> str:
    hits = [b for b in _HEREDOC_RE.findall(SCRIPT.read_text()) if _MARKER in b]
    assert len(hits) == 1, f"expected exactly one embedded block containing {_MARKER!r}, got {len(hits)}"
    return hits[0]


def _run(argv: list[str]) -> str:
    """Exec the finding_guard block as the driver would; return its stdout (the regressed count)."""
    code = compile(_block(), "<af-ticket-loop:finding_guard>", "exec")
    old_argv = sys.argv
    sys.argv = ["-", *argv]
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(code, {"__name__": "__main__"})  # noqa: S102 - executing the driver's own bytes is the point
    except SystemExit:
        pass
    finally:
        sys.argv = old_argv
    return buf.getvalue().strip()


@pytest.fixture
def finished_ticket_with_checkless_finding(monkeypatch):
    """One finished ticket carrying an OPEN finding with check_id=None (the case nothing else can
    break), and instrumented regress + a git-log stub that reports ZERO answering commits."""
    fact = {
        "id": "cid-1", "cid": "cid-1",
        "meta": {
            "requirement_id": "T1", "build_state": "finished",
            ts.M_REGRESSION_DETAIL: [
                {"reason": "the tip overlay reads a stale sidecar", "check_id": None, "resolved": False},
            ],
        },
    }
    regressed: list = []
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: [fact])
    monkeypatch.setattr(_praxis, "regress_requirements",
                        lambda project, ids, detail=None, **kw: regressed.append(list(ids)))

    # No answering commit exists — `git log base..HEAD --grep=T1` returns empty. Patch subprocess.run
    # so the block's `import subprocess` sees an empty stdout (a real non-repo tmp dir would raise,
    # which the block treats as "unknown" == answered — the opposite of this scenario).
    class _R:
        stdout = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    return regressed


def _argv(streak: Path, esc: Path, kmax: int = 2) -> list[str]:
    # PROJECT rnd base streak esc kmax ids...
    return ["alpha", "1", "HEAD", str(streak), str(esc), str(kmax), "T1"]


def test_the_streak_caps_at_kmax_then_escalates_instead_of_regressing(
        finished_ticket_with_checkless_finding, tmp_path):
    regressed = finished_ticket_with_checkless_finding
    streak, esc = tmp_path / "streak.json", tmp_path / "esc.tsv"

    # Rounds 1 and 2 (K=2): the ticket is regressed each time, nothing is escalated.
    assert _run(_argv(streak, esc)) == "1"
    assert _run(_argv(streak, esc)) == "1"
    assert regressed == [["cid-1"], ["cid-1"]], "the first K rounds must still regress"
    assert not esc.read_text().strip(), "nothing should escalate before the cap is reached"

    # Round 3: the cap is hit. The ticket is NOT regressed again — it is escalated for a human.
    assert _run(_argv(streak, esc)) == "0", "past the cap the guard must regress nothing"
    assert regressed == [["cid-1"], ["cid-1"]], "no further regress once capped (this is the infinite loop)"
    lines = [l for l in esc.read_text().splitlines() if l.strip()]
    assert len(lines) == 1 and lines[0].startswith("T1\t"), "the capped ticket must be escalated"
    assert "the tip overlay reads a stale sidecar" in lines[0], "the escalation must name the finding"


def test_an_answering_commit_resets_the_streak(
        finished_ticket_with_checkless_finding, monkeypatch, tmp_path):
    regressed = finished_ticket_with_checkless_finding
    streak, esc = tmp_path / "streak.json", tmp_path / "esc.tsv"

    assert _run(_argv(streak, esc)) == "1"  # streak -> 1

    # Now a commit answers the finding: `git log` returns a non-empty line. The guard must NOT count
    # this as a no-change close, and must FORGET the streak so a future genuine no-change close starts
    # from zero rather than tripping the cap early.
    class _R:
        stdout = "abc123 fix: answer the finding (T1)"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert _run(_argv(streak, esc)) == "0", "a ticket with an answering commit is not regressed"

    import json
    assert not [k for k in json.loads(streak.read_text()) if k.startswith("T1\x00")], \
        "the answering commit must reset T1's streak"
