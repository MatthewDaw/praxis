"""A verify="manual" ticket may not be regressed for producing no commit.

THE BUG THIS LOCKS. Post-merge round verification regresses every ticket the verifier names, and the
verifier's lenses carry an unstated commits-must-exist invariant. A manual-verify ticket produces no
commit BY DESIGN — its completion is a human sign-off, not code — so that invariant fires on it every
single round. Observed on mvpvu-foundation round #1:

    round #1 verification: verdict=fail gates_green=True regressed=1
    [{'id': 'R21', 'reason': "R21's worktree branch never produced a merged commit in this round...
      whatever R21 was supposed to build is not present anywhere in src/, tests/, docs/",
      'fix': "Re-claim and rebuild R21 from scratch..."}]

R21 carried ``meta.verify == "manual"`` and its acceptance was a human re-labelling pass over
rendered images. Three independent review lenses each "confirmed" the absence. The ticket cannot ever
answer that finding, so it cycles forever: dispatched -> cannot produce commits -> regressed ->
re-dispatched.

Like the other loop tests there is no module to import — the logic lives in a ``python - <<'PYEOF'``
heredoc inside the shell driver — so these tests READ THE SHIPPED SCRIPT, extract the exact
regression-application block, and EXECUTE it against instrumented modules.

What they pin, in both directions:
  * a PARKED manual ticket with zero commits is NOT regressed, and is REPORTED as parked awaiting
    sign-off (never silently passed — nothing here finishes it, and completion still runs through
    ``all_validations_passed``, whose manual clause no worker-sourced pass satisfies);
  * an AUTOMATED ticket with zero commits IS still regressed, in the same verdict;
  * a manual ticket that DID land a commit IS still regressed — the suppression is scoped to
    "no code of its exists to fault", not to the ticket class.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

# IMPORT ORDER IS LOAD-BEARING — see the long note in test_af_ticket_loop_seam.py. Canonicalize the
# seam via agent_factory FIRST, so the bare names below resolve to the same module objects the
# exec'd block imports (and that these monkeypatches therefore actually patch).
import agent_factory  # noqa: F401,E402
from agent_factory import failure_taxonomy, ingestion_api  # noqa: E402

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)

# A marker unique to the verify_round regression-application block (its commit-provenance probe).
_MARKER = "def _named_by_a_commit(rid):"

PROJ = "mvpvu-foundation"

# The verbatim shape of the observed false positive.
R21_REASON = ("R21's worktree branch never produced a merged commit in this round, and whatever R21 "
              "was supposed to build is not present anywhere in src/, tests/, docs/")


def _block() -> str:
    hits = [b for b in _HEREDOC_RE.findall(SCRIPT.read_text()) if _MARKER in b]
    assert len(hits) == 1, f"expected exactly one embedded block containing {_MARKER!r}, got {len(hits)}"
    return hits[0]


def _run(argv: list[str]) -> str:
    """Exec the regression block as the driver does; return its stdout (the regressed count)."""
    code = compile(_block(), "<af-ticket-loop:verify_round-regress>", "exec")
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


def _ticket(rid: str, *, manual: bool) -> dict[str, Any]:
    """A finished ticket whose acceptance floor is covered by ONE passing worker-run validation.

    ``manual=True`` puts the floor in ``manual_requirements`` — exactly what a ``meta.verify:
    manual`` ticket gets from ``start_ticket`` — so the strict gate withholds completion pending a
    human/external-sourced pass while every other obligation is met: i.e. PARKED, per
    ``ts.parked_on_manual``. ``manual=False`` is the same ticket with an automated floor.
    """
    floor = f"{rid}::acceptance"
    return {
        "id": f"cid-{rid}", "meta": {
            "requirement_id": rid, "build_state": "finished",
            ts.M_REQUIRED_VALIDATIONS: [floor],
            ts.M_MANUAL_REQUIREMENTS: [floor] if manual else [],
            ts.M_PINNED_CHECKS: [{"validation_id": "v1", "covers": [floor], "run": "echo ok",
                                  "passed": True, "source": ts.WORKER_PASS_SOURCE}],
        },
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Instrumented Praxis + ingestion + git, and a factory for driving the block over a verdict."""
    state: dict[str, Any] = {"tickets": [], "commits": set(), "ingested": [], "regressed": []}

    def facts_by(**kw):
        return list(state["tickets"]) if kw.get("category") == "requirement" else []

    monkeypatch.setattr(_praxis, "facts_by", facts_by)
    monkeypatch.setattr(_praxis, "get_fact",
                        lambda cid, **kw: next((t for t in state["tickets"] if t["id"] == cid), None))
    monkeypatch.setattr(_praxis, "regress_requirements",
                        lambda project, ids, detail=None, **kw: state["regressed"].extend(ids))

    def regress_with_ingestion(project, ids, lesson, **kw):
        state["ingested"].extend(ids)
        return {"lesson_id": "lesson-1", "check_id": None}

    monkeypatch.setattr(ingestion_api, "regress_with_ingestion", regress_with_ingestion)
    # The widening pass only runs for tickets that WERE regressed; stub the taxonomy so it stops at
    # "first sighting" instead of reaching a live backend.
    monkeypatch.setattr(failure_taxonomy, "assign_class",
                        lambda *a, **kw: {"class_id": "c1", "action": "created", "recurrence_count": 1})

    class _Result:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.returncode = 0

    def fake_git(cmd, *a, **kw):
        """`git rev-parse HEAD` -> a sha; `git log --grep=(RID)` -> a subject iff RID committed."""
        if "rev-parse" in cmd:
            return _Result("deadbeef")
        grep = next((c for c in cmd if str(c).startswith("--grep=")), "")
        rid = str(grep)[len("--grep=("):-1]
        return _Result(f"feat: did the thing ({rid})" if rid in state["commits"] else "")

    monkeypatch.setattr(subprocess, "run", fake_git)

    def drive(tickets, verdict, *, commits=()):
        state["tickets"] = list(tickets)
        state["commits"] = set(commits)
        vpath = tmp_path / "verdict.json"
        vpath.write_text(json.dumps(verdict))
        parked = tmp_path / "parked.txt"
        out = _run([PROJ, "1", str(vpath), str(tmp_path), str(parked)])
        return out, (parked.read_text() if parked.exists() else "")

    state["drive"] = drive
    return state


def _verdict(*ids: str) -> dict[str, Any]:
    return {"verdict": "fail", "gates_green": True, "notes": "one ticket produced nothing",
            "regressed": [{"id": rid, "reason": R21_REASON, "evidence": "no commit on its branch",
                           "fix": f"Re-claim and rebuild {rid} from scratch"} for rid in ids]}


def test_a_manual_ticket_with_zero_commits_is_not_regressed(harness):
    """The R21 case verbatim: parked on a human sign-off, zero commits, faulted for exactly that."""
    r21 = _ticket("R21", manual=True)
    assert ts.parked_on_manual(r21, (PROJ, f"prd-{PROJ}")) is True, "fixture must model a PARKED ticket"

    count, parked = harness["drive"]([r21], _verdict("R21"))

    assert count == "0", "a manual-verify ticket must not be regressed for producing no commit"
    assert harness["ingested"] == [] and harness["regressed"] == [], \
        "nothing may be written for a suppressed regression — not the ingestion, not the regress"
    # ...and it is not silently passed either: the pending sign-off is REPORTED for a human.
    assert "R21" in parked and "PARKED awaiting manual sign-off" in parked
    assert "cannot self-certify" in parked


def test_an_automated_ticket_with_zero_commits_is_still_regressed(harness):
    """The control. Same verdict, same absence of commits, no manual requirement — still a defect."""
    count, parked = harness["drive"]([_ticket("R22", manual=False)], _verdict("R22"))

    assert count == "1", "an automated ticket that produced nothing must still be regressed"
    assert harness["ingested"] == ["cid-R22"] and harness["regressed"] == ["cid-R22"]
    assert parked == "", "an automated ticket is not parked on anything"


def test_both_in_one_verdict_are_judged_separately(harness):
    """The mixed round: the suppression is per-ticket, and must not spare the ticket beside it."""
    count, parked = harness["drive"]([_ticket("R21", manual=True), _ticket("R22", manual=False)],
                                     _verdict("R21", "R22"))

    assert count == "1"
    assert harness["regressed"] == ["cid-R22"], "only the automated ticket may be regressed"
    assert "R21" in parked and "R22" not in parked


def test_a_manual_ticket_that_did_land_a_commit_is_regressed_on_its_merits(harness):
    """The suppression is scoped to "no code of its exists to fault", NOT to the ticket class: a
    manual ticket whose code IS in the merged history stays answerable for it."""
    count, parked = harness["drive"]([_ticket("R21", manual=True)], _verdict("R21"),
                                     commits={"R21"})

    assert count == "1", "a manual ticket with landed code is regressed like any other"
    assert harness["regressed"] == ["cid-R21"]
    assert parked == ""


def test_an_unanswerable_parked_check_regresses_rather_than_swallowing_the_finding(harness, monkeypatch):
    """Fail LOUD, not open. If "is it parked?" cannot be answered, the verifier's finding stands —
    a suppressed regression on a broken read would be the false-green this whole pass exists to
    prevent."""
    def boom(*a, **kw):
        raise RuntimeError("praxis unreachable")

    monkeypatch.setattr(ts, "parked_on_manual", boom)
    count, parked = harness["drive"]([_ticket("R21", manual=True)], _verdict("R21"))

    assert count == "1", "an unanswerable parked-check must not suppress the regression"
    assert parked == ""
