"""Offline checks for Monica's dashboard and human-gate support suite."""

from pathlib import Path

from knowledge.evals.run import FakeRunner, load_cases, run_case

MONICA_SUITE_DIR = Path(__file__).parents[1] / "cases" / "monica"

EXPECTED_CASE_IDS = {
    "monica_demo_candidate_contract",
    "monica_human_gate_staged_not_injected",
    "monica_promotion_context_cand1",
    "monica_contradiction_pair_metadata",
    "monica_api_mutation_audit_trail",
    "monica_low_confidence_confirmation",
    "monica_data_source_fallback_readiness",
    "monica_eval_metrics_narrative",
}


def test_monica_suite_cases_load_from_requested_folder():
    cases = load_cases(MONICA_SUITE_DIR)
    assert {case.id for case in cases} == EXPECTED_CASE_IDS


def test_monica_suite_component_cases_pass_offline():
    cases = load_cases(MONICA_SUITE_DIR)
    assert cases
    results = [run_case(case, FakeRunner()) for case in cases]
    assert {result.case_id for result in results} == EXPECTED_CASE_IDS
    assert all(result.passed for result in results)


def test_monica_human_gate_keeps_proposed_distillation_out_of_context():
    case = next(
        case for case in load_cases(MONICA_SUITE_DIR) if case.id == "monica_human_gate_staged_not_injected"
    )
    result = run_case(case, FakeRunner())
    assert result.passed is True
    checks = {check.name: check for check in result.checks}
    assert checks["active_context_visible"].passed is True
    assert checks["proposed_context_hidden"].passed is True
