"""Shared test fixtures — the OFFLINE judge harness (U4 of the universal-minimalism-gate plan).

Once a universal GRADED check gates, every test that drives a ticket to ``finished`` needs an LLM
``Complete`` judge that does not exist offline. This harness supplies deterministic stub judges so CI
has a judge with no API key: a pass judge (scores every declared axis at the ceiling, no defects) and
a fail judge (a located defect above any floor). The universal check ships ``report_only=true`` — so
it does NOT gate and existing ticket-to-finished tests keep passing WITHOUT a judge; these stubs
prove the pipeline works when a universal is flipped to gating (and cover the graded VERIFY path).

The stub parses axis names out of the real judge prompt, so it grades any rubric correctly.
"""

from __future__ import annotations

import json
import re

import pytest

_AXIS_LINE = re.compile(r"^\s*-\s+(.+?)\s+\(pass threshold", re.MULTILINE)


def _axes_in_prompt(prompt: str) -> list[str]:
    """The axis names build_judge_prompt listed, so a stub grades any rubric it is handed."""
    return _AXIS_LINE.findall(prompt)


def stub_pass_judge(prompt: str) -> str:
    """A deterministic judge that PASSES: every declared axis at 1.0, no located defects."""
    return json.dumps({"axis_scores": {name: 1.0 for name in _axes_in_prompt(prompt)}, "defects": []})


def stub_fail_judge(prompt: str) -> str:
    """A deterministic judge that FAILS: a maximally-confident located defect on the first axis."""
    axes = _axes_in_prompt(prompt)
    return json.dumps({
        "axis_scores": {name: 0.1 for name in axes},
        "defects": [{"file": "m.py", "line": 1, "problem": "duplicated logic",
                     "remedy": "consolidate", "confidence": 10}],
    })


@pytest.fixture
def pass_judge():
    """The offline pass judge as a ``Complete`` callable."""
    return stub_pass_judge


@pytest.fixture
def fail_judge():
    """The offline fail judge as a ``Complete`` callable."""
    return stub_fail_judge
