"""Unit tests for the plan-repro coverage engine (evals/plan_repro/coverage.py).

The engine's orchestration (per-part sweep, evidence-required downgrade, targeted
adversarial control flow, aggregation, pass/fail) is deterministic and tested here with
injected stand-ins; the lexical baselines double as cheap, model-free judges.
"""

from pathlib import Path

from evals.plan_repro.coverage import (
    COVERED,
    MISSING,
    CoverageReport,
    Feature,
    PartResult,
    all_related_query,
    build_judge_prompt,
    default_adversarial_select,
    judge_result_from_response,
    lexical_evaluator,
    lexical_related_query,
    load_golden,
    refuted_from_response,
    run_coverage,
)

GOLDEN = (
    Path(__file__).resolve().parent.parent
    / "evals" / "plan_repro" / "team-app" / "golden-features.yaml"
)


def _f(fid: str, text: str, **kw) -> Feature:
    return Feature(id=fid, text=text, **kw)


def _evaluator_covering(ids: set[str]):
    """A deterministic fake judge: COVERED for ids in the set, MISSING otherwise."""

    def ev(part: Feature, related: list[Feature]) -> PartResult:
        if part.id in ids:
            return PartResult(part_id=part.id, status=COVERED, evidence="x", confidence=1.0)
        return PartResult(part_id=part.id, status=MISSING)

    return ev


# --- the sweep is systematic + self-cover is full coverage ---------------------


def test_every_part_is_visited():
    parts = [_f("A", "alpha"), _f("B", "beta"), _f("C", "gamma")]
    rep = run_coverage(parts, [], all_related_query, _evaluator_covering(set()))
    assert [r.part_id for r in rep.results] == ["A", "B", "C"]  # none skipped


def test_self_cover_is_full_coverage():
    parts = [_f("A", "alpha feature one"), _f("B", "beta feature two")]
    rep = run_coverage(parts, list(parts), all_related_query, lexical_evaluator)
    assert rep.passed
    assert rep.counts()[MISSING] == 0


def test_missing_feature_is_a_hole_and_derived_is_flagged():
    parts = [_f("A", "alpha unique zebra"), _f("B", "beta unique quartz", derived=True)]
    cands = [_f("C", "alpha unique zebra")]  # covers only A
    rep = run_coverage(parts, cands, all_related_query, lexical_evaluator)
    assert {r.part_id for r in rep.holes} == {"B"}
    assert [r.part_id for r in rep.derived_holes] == ["B"]
    assert not rep.passed


def test_engine_carries_derived_flag_onto_result():
    parts = [_f("A", "a", derived=True), _f("B", "b")]
    rep = run_coverage(parts, [], all_related_query, _evaluator_covering({"A"}))
    by = {r.part_id: r for r in rep.results}
    assert by["A"].status == COVERED and by["A"].derived is True
    assert by["B"].status == MISSING


# --- targeted adversarial control flow -----------------------------------------


def test_refuter_downgrades_a_selected_covered_claim():
    parts = [_f("A", "a", derived=True)]  # derived -> selected for adversarial
    rep = run_coverage(
        parts, [], all_related_query, _evaluator_covering({"A"}),
        refuter=lambda part, result, related: True,
    )
    assert rep.results[0].status == MISSING
    assert rep.results[0].adversarial == {"ran": True, "refuted": True}


def test_refuter_keeps_match_when_not_refuted():
    parts = [_f("A", "a", derived=True)]
    rep = run_coverage(
        parts, [], all_related_query, _evaluator_covering({"A"}),
        refuter=lambda *a: False,
    )
    assert rep.results[0].status == COVERED
    assert rep.results[0].adversarial == {"ran": True, "refuted": False}


def test_refuter_not_run_on_unselected_part():
    parts = [_f("A", "a", severity="low")]  # not derived/critical, high conf -> not selected
    calls: list[int] = []

    def refuter(part, result, related):
        calls.append(1)
        return True

    rep = run_coverage(parts, [], all_related_query, _evaluator_covering({"A"}), refuter=refuter)
    assert rep.results[0].status == COVERED
    assert calls == []  # adversarial pass skipped


def test_default_adversarial_select():
    covered = PartResult("A", COVERED, confidence=1.0)
    assert default_adversarial_select(_f("A", "a", derived=True), covered)
    assert default_adversarial_select(_f("A", "a", severity="high"), covered)
    assert default_adversarial_select(_f("A", "a"), PartResult("A", COVERED, confidence=0.5))
    assert not default_adversarial_select(_f("A", "a"), covered)


# --- evidence-required judge parsing -------------------------------------------


def test_covered_without_quoted_evidence_is_downgraded_to_missing():
    r = judge_result_from_response(_f("A", "a"), {"status": "covered", "evidence": ""})
    assert r.status == MISSING
    assert "no evidence" in r.notes


def test_covered_with_evidence_parses():
    r = judge_result_from_response(
        _f("A", "a", derived=True),
        {"status": "covered", "evidence": "the plan does X", "matched_ids": ["C1"], "confidence": 0.9},
    )
    assert r.status == COVERED
    assert r.evidence == "the plan does X"
    assert r.matched_ids == ["C1"]
    assert r.derived is True


def test_unknown_status_is_missing():
    assert judge_result_from_response(_f("A", "a"), {"status": "nope"}).status == MISSING


def test_refuted_defaults_true_on_bad_input():
    assert refuted_from_response("{ not json") is True
    assert refuted_from_response({"refuted": False}) is False


# --- adaptive lexical retrieval ------------------------------------------------


def test_lexical_related_query_returns_only_relevant():
    part = _f("P", "password reset email link")
    cands = [
        _f("C1", "a user can request a password reset and receive an email link"),
        _f("C2", "coach posts a team message"),
        _f("C3", "completely unrelated widget"),
    ]
    ids = {c.id for c in lexical_related_query(part, cands)}
    assert ids == {"C1"}


def test_lexical_related_query_empty_when_nothing_matches():
    part = _f("P", "quantum entanglement teleportation")
    assert lexical_related_query(part, [_f("C1", "coach posts a message")]) == []


# --- report --------------------------------------------------------------------


def test_report_passed_and_format():
    rep = CoverageReport(
        results=[PartResult("A", COVERED, evidence="x"), PartResult("B", MISSING, derived=True)]
    )
    assert not rep.passed
    assert [r.part_id for r in rep.derived_holes] == ["B"]
    out = rep.format()
    assert "FAIL" in out and "DERIVED HOLES" in out


def test_judge_prompt_is_evidence_required():
    prompt = build_judge_prompt(_f("A", "alpha"), [_f("C1", "cand one")])
    assert "alpha" in prompt and "C1" in prompt
    assert "MISSING" in prompt and "quote" in prompt.lower()


# --- against the real golden ---------------------------------------------------


def test_golden_loads_with_derived_teeth():
    parts = load_golden(GOLDEN)
    assert len(parts) >= 70
    by = {p.id: p for p in parts}
    assert "AUTH-password-reset" in by  # the archetypal derived hole
    assert by["AUTH-password-reset"].derived is True
    assert any(p.derived for p in parts)


def test_golden_self_cover_passes():
    parts = load_golden(GOLDEN)
    cands = [Feature(id=p.id, text=p.text) for p in parts]
    rep = run_coverage(parts, cands, lexical_related_query, lexical_evaluator)
    assert rep.passed, rep.format()
