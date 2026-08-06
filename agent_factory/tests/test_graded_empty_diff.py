"""An empty diff must not be handed to the judge and called a verdict.

A code-quality rubric scored against zero lines gets a confident pass. Observed
on appeal_engine DATA-1, recorded verbatim in the ticket:

    "the rubric is trivially satisfied on an empty diff: no dead code, no
     duplication, no fragmentation, because nothing was added."

In the ticket that is indistinguishable from a judge having read real code and
approved it, and it is how a ticket came to carry two green graded checks having
evaluated nothing.

The discharge still PASSES: a ticket whose remaining work is operational (run a
harvest, sync a bucket) legitimately has no diff, and failing it would block
honest work. What must not happen is the pass masquerading as a judge verdict,
so it carries its own source and never spends a judge call.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _graded_verify as gv  # noqa: E402
import _ticket_state as ts  # noqa: E402
from agent_factory.rubric import Anchors, Axis, Rubric  # noqa: E402

RUBRIC = Rubric(
    criterion="minimal code",
    axes=[Axis(name="proportionality", threshold=0.8, guidance="no dead code")],
    anchors=Anchors(good=["x = 1"], slop=["x = 1  # unused"], negative=[]),
    judge_prompt="grade it",
    confidence_floor=5,
)


class _Recorder:
    """Captures what the module records, so the source is assertable."""

    def __init__(self):
        self.calls = []

    def __call__(self, cid, validation_id, passed, **kw):
        self.calls.append({"cid": cid, "validation_id": validation_id,
                           "passed": passed, **kw})


def _install(monkeypatch, judge_calls):
    """Point the module at an in-memory ticket and a judge that records calls."""
    entry = {"validation_id": "minimalism-dry", "covers": ["r1"], "kind": "graded"}
    meta = {"pinned_checks": [entry], "graded_loop": {}}
    monkeypatch.setattr(ts, "_meta", lambda *a, **k: meta, raising=False)
    monkeypatch.setattr(gv, "frozen_rubric_for", lambda *a, **k: RUBRIC, raising=False)
    monkeypatch.setattr(gv, "_pinned_entry", lambda *a, **k: entry, raising=False)
    rec = _Recorder()
    monkeypatch.setattr(ts, "record_validation_pass", rec, raising=False)
    # The fresh-grade path persists graded-loop state; keep the test offline.
    monkeypatch.setattr(ts._praxis, "write_build_state", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ts._praxis, "patch_meta", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ts, "_ref_kw", lambda *a, **k: {}, raising=False)

    def judge(prompt):
        judge_calls.append(prompt)
        return '{"axis_scores": {"proportionality": 1.0}, "defects": []}'

    return judge, rec


def test_empty_diff_never_reaches_the_judge(monkeypatch):
    calls = []
    judge, rec = _install(monkeypatch, calls)
    result = gv.verify_graded_check("cid1", "minimalism-dry", "", judge)
    assert calls == [], "the judge was asked to grade an empty diff"
    assert result.verdict.passed is True
    assert result.iterations == 0, "an empty diff must not consume a judge iteration"


def test_empty_diff_is_recorded_under_its_own_source(monkeypatch):
    """The whole point: greppable as 'not evaluated', not as a judge verdict."""
    calls = []
    judge, rec = _install(monkeypatch, calls)
    gv.verify_graded_check("cid1", "minimalism-dry", "", judge)
    assert rec.calls, "nothing was recorded"
    assert rec.calls[-1]["source"] == gv.EMPTY_DIFF_SOURCE
    assert rec.calls[-1]["source"] != gv.GRADED_SOURCE


def test_whitespace_only_diff_counts_as_empty(monkeypatch):
    calls = []
    judge, rec = _install(monkeypatch, calls)
    gv.verify_graded_check("cid1", "minimalism-dry", "   \n\t\n  ", judge)
    assert calls == []
    assert rec.calls[-1]["source"] == gv.EMPTY_DIFF_SOURCE


def test_the_reason_says_it_was_not_evaluated(monkeypatch):
    calls = []
    judge, rec = _install(monkeypatch, calls)
    result = gv.verify_graded_check("cid1", "minimalism-dry", "", judge)
    reason = result.verdict.reason.lower()
    assert "not evaluated" in reason
    assert "no code diff" in reason


def test_a_real_diff_still_goes_to_the_judge(monkeypatch):
    """Guard the guard: the shortcut must not swallow genuine gradings."""
    calls = []
    judge, rec = _install(monkeypatch, calls)
    gv.verify_graded_check("cid1", "minimalism-dry", "--- a/x.py\n+++ b/x.py\n+x = 1\n", judge)
    assert len(calls) == 1, "a real diff must still be graded"
    assert rec.calls[-1]["source"] == gv.GRADED_SOURCE
