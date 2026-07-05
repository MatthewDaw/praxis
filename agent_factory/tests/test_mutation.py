"""Unit tests for the mutation worklist parser (U5; R8, SC1, AE3).

These exercise ONLY the parsing / worklist / kill-rate logic against a synthetic
``mutmut results``-style fixture. They deliberately do NOT invoke ``mutmut`` itself:
mutation runs are out-of-band (``python -m mutmut run`` then ``python -m mutmut results``)
and flaky on Windows, so the wrapper is the part that must be deterministically tested.

Target kill-rate is a *report* metric (>=80%, :data:`evals.mutation.TARGET_KILL_RATE`),
not a CI gate (KTD7).
"""

from __future__ import annotations

import pytest

from evals.mutation import (
    TARGET_KILL_RATE,
    MutationReport,
    parse_results,
    render_worklist,
)

# A synthetic capture of the mutmut 2.x `mutmut results` layout: the apply/show preamble,
# a Survived bucket split across two files (one of them a plan_gate rule mutant — the AE3
# shape), and a non-survivor (Timed out) bucket that must NOT enter the worklist.
RESULTS_FIXTURE = """\
To apply a mutant on disk:
    mutmut apply <id>

To show a mutant:
    mutmut show <id>


Survived 🙁 (3)

---- src/agent_factory/plan_gate.py (2) ----

12-13

---- evals/checks.py (1) ----

7

Timed out ⏰ (1)

---- src/agent_factory/gate.py (1) ----

20
"""

NO_SURVIVORS_FIXTURE = """\
To apply a mutant on disk:
    mutmut apply <id>
"""


def test_parses_survivors_into_worklist():
    report = parse_results(RESULTS_FIXTURE)
    assert report.survivor_count == 3
    assert [m.mutant_id for m in report.worklist] == ["12", "13", "7"]


def test_survivors_carry_their_source_location():
    report = parse_results(RESULTS_FIXTURE)
    by_id = {m.mutant_id: m.location for m in report.worklist}
    assert by_id["12"] == "src/agent_factory/plan_gate.py"
    assert by_id["13"] == "src/agent_factory/plan_gate.py"
    assert by_id["7"] == "evals/checks.py"


def test_non_survivor_buckets_are_excluded_from_worklist():
    # The timed-out mutant (#20 in gate.py) was caught, just noisily — never a wanted case.
    report = parse_results(RESULTS_FIXTURE)
    assert "20" not in {m.mutant_id for m in report.worklist}
    assert all(m.location != "src/agent_factory/gate.py" for m in report.worklist)
    assert report.counts == {"survived": 3, "timeout": 1}


def test_ae3_dangling_rule_mutant_is_a_located_worklist_entry():
    # AE3: a surviving mutant on the plan_gate rule logic surfaces in the worklist with its
    # location and id, so a human knows which case is still wanted to kill it.
    report = parse_results(RESULTS_FIXTURE)
    plan_gate_survivors = [
        m for m in report.worklist if m.location == "src/agent_factory/plan_gate.py"
    ]
    assert plan_gate_survivors
    entry = plan_gate_survivors[0]
    assert entry.location in entry.description
    assert entry.mutant_id in entry.description


def test_id_ranges_are_expanded():
    report = parse_results(
        "Survived 🙁 (5)\n\n---- evals/checks.py (5) ----\n\n1-3, 7, 9\n"
    )
    assert [m.mutant_id for m in report.worklist] == ["1", "2", "3", "7", "9"]


def test_kill_rate_counts_only_survivors_as_escapes():
    report = parse_results(RESULTS_FIXTURE)  # 3 survivors
    # 10 mutants generated, 3 survived -> 7 killed -> 70%.
    assert report.kill_rate(10) == pytest.approx(0.70)
    assert report.meets_target(10) is False  # below the 80% report target


def test_target_kill_rate_is_eighty_percent():
    assert TARGET_KILL_RATE == 0.80


def test_clean_run_yields_empty_worklist_and_full_kill_rate():
    report = parse_results(NO_SURVIVORS_FIXTURE)
    assert report.worklist == []
    assert report.survivor_count == 0
    assert report.kill_rate(8) == 1.0
    assert report.meets_target(8) is True


def test_kill_rate_rejects_nonpositive_total():
    report = parse_results(NO_SURVIVORS_FIXTURE)
    with pytest.raises(ValueError):
        report.kill_rate(0)


def test_empty_input_is_an_empty_report():
    report = parse_results("")
    assert isinstance(report, MutationReport)
    assert report.worklist == []
    assert report.counts == {}


def test_render_worklist_reports_rate_and_lists_survivors():
    report = parse_results(RESULTS_FIXTURE)
    rendered = render_worklist(report, total_mutants=10)
    assert "70%" in rendered
    assert "BELOW TARGET" in rendered
    assert "src/agent_factory/plan_gate.py: mutant #12" in rendered
    # The caught timed-out mutant must not be advertised as a wanted case.
    assert "#20" not in rendered


def test_render_worklist_clean_run_states_no_survivors():
    report = parse_results(NO_SURVIVORS_FIXTURE)
    rendered = render_worklist(report, total_mutants=8)
    assert "no surviving mutants" in rendered
    assert "100%" in rendered
