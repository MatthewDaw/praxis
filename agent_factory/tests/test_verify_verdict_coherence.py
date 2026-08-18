"""B-2: a verification round may not read GREEN while the verifier's own notes name failing tickets.

The verifier/loop split already stops a verifier writing its own Praxis resolution: it emits a
verdict JSON and the loop performs the regression from the `regressed` field. And `finding_guard`
already regresses a ticket that closes an OPEN finding with ZERO commits. What neither covers is the
UNDER-REPORT shape observed live: a verdict whose authoritative `regressed` field is EMPTY -- so the
loop regresses nothing and the round reads as a pass -- while the verdict's own `notes` read
"Should-regress REM-29,REM-28,REM-27". The judgement named the failures; the field that carries them
to the write path silently dropped them. A finding is not answered by a note about it.

These tests SLICE the shipped coherence-gate python block out of `af-ticket-loop.sh` and run it over
crafted verdict files, asserting the under-report verdict is downgraded to UNVERIFIED (never a pass)
while a genuinely clean pass and a properly-reported failure are left alone.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)


def _coherence_block() -> str:
    hits = [b for b in _HEREDOC_RE.findall(SCRIPT.read_text()) if "is neither pass nor fail" in b]
    assert len(hits) == 1, f"expected exactly one coherence block, found {len(hits)}"
    return hits[0]


def _summary(verdict: dict, tmp_path: Path) -> str:
    """Run the shipped coherence block over `verdict` exactly as the driver does (verdict path as
    argv[1], code on stdin) and return the one-line summary it prints."""
    vpath = tmp_path / "verdict.json"
    vpath.write_text(json.dumps(verdict))
    out = subprocess.run([sys.executable, "-", str(vpath)], input=_coherence_block(),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_notes_naming_a_regression_while_regressed_empty_is_incoherent(tmp_path):
    """The observed failure verbatim: regressed=[] with notes 'Should-regress REM-29,REM-28,REM-27'."""
    summary = _summary({"verdict": "pass", "gates_green": True, "regressed": [],
                        "notes": "All lenses clear. Should-regress REM-29,REM-28,REM-27."}, tmp_path)
    assert summary.startswith("INCOHERENT"), summary
    assert "a note about it" in summary


def test_a_genuinely_clean_pass_is_not_flagged(tmp_path):
    summary = _summary({"verdict": "pass", "gates_green": True, "regressed": [],
                        "notes": "all repo-wide gates green; every ticket survived integration"},
                       tmp_path)
    assert not summary.startswith("INCOHERENT"), summary


def test_generic_regression_prose_without_a_named_ticket_does_not_false_positive(tmp_path):
    """A pass whose notes merely use the word 'regress' generically (no 'should regress', no named
    ticket) must not be downgraded -- the consequence of a false positive is only non-certification,
    but a normal green round must still read green."""
    summary = _summary({"verdict": "pass", "gates_green": True, "regressed": [],
                        "notes": "no regressions found; nothing to regress this round"}, tmp_path)
    assert not summary.startswith("INCOHERENT"), summary


def test_a_properly_reported_failure_is_not_flagged_by_the_underreport_rule(tmp_path):
    """When the tickets ARE in `regressed`, the loop's regression pass carries them -- notes naming
    them is expected, not incoherent. The under-report rule fires only on an EMPTY `regressed`."""
    summary = _summary({"verdict": "fail", "gates_green": False,
                        "regressed": [{"id": "REM-29", "reason": "migration overwritten"}],
                        "notes": "REM-29 should be regressed: its migration was overwritten"}, tmp_path)
    assert not summary.startswith("INCOHERENT"), summary


def test_existing_gates_red_but_pass_incoherence_still_holds(tmp_path):
    """Regression pin for the pre-existing coherence rule, so the B-2 addition did not disturb it."""
    summary = _summary({"verdict": "pass", "gates_green": False, "regressed": [],
                        "notes": "gates red but everything looks fine"}, tmp_path)
    assert summary.startswith("INCOHERENT"), summary
