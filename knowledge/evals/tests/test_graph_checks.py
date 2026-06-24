"""Tests for the FR-005 ``at_most_one_active`` graph check."""

from __future__ import annotations

from knowledge.evals.deterministic_checks.graph import at_most_one_active
from knowledge.evals.eval_def import EvalContext

_A = "ALWAYS use tabs for indentation in this project. Spaces are forbidden."
_B = "ALWAYS use spaces for indentation in this project. Tabs are forbidden."


def _ctx(injected: str | None) -> EvalContext:
    return EvalContext(case_id="c", output="", injected_knowledge=injected)


def test_passes_when_only_the_winner_is_active():
    r = at_most_one_active(_ctx(_A), texts=[_A, _B], winner=_A)
    assert r.passed


def test_fails_when_both_active():
    r = at_most_one_active(_ctx(f"{_A}\n\n{_B}"), texts=[_A, _B], winner=_A)
    assert not r.passed


def test_fails_when_the_wrong_side_is_active():
    r = at_most_one_active(_ctx(_B), texts=[_A, _B], winner=_A)
    assert not r.passed


def test_not_applicable_offline_without_injected_knowledge():
    # FakeRunner injects nothing -> the live-only invariant check passes (n/a).
    r = at_most_one_active(_ctx(None), texts=[_A, _B], winner=_A)
    assert r.passed
