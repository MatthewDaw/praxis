"""Unit tests for the LLM-backed coverage evaluator (evals/plan_repro/llm_evaluator.py).

No network: a fake ``Complete`` (prompt -> canned text) is injected, so these exercise the
glue (prompt -> model text -> parsed verdict) and the engine integration deterministically.
"""

from evals.plan_repro.coverage import (
    COVERED,
    MISSING,
    Feature,
    PartResult,
    all_related_query,
    run_coverage,
)
from evals.plan_repro.llm_evaluator import (
    _extract_json,
    make_llm_evaluator,
    make_llm_refuter,
    make_tiered_evaluator,
)


def _fixed(resp: str):
    """A Complete that always returns ``resp``."""
    return lambda prompt: resp


def _router(judge: str, refuter: str):
    """A Complete that returns the refuter response for refuter prompts, else the judge one."""
    return lambda prompt: refuter if "refuted" in prompt else judge


# --- tolerant JSON extraction --------------------------------------------------


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert _extract_json('```json\n{"status": "covered"}\n```') == {"status": "covered"}


def test_extract_json_embedded_in_prose():
    out = _extract_json('Sure — here is my call: {"status": "missing", "notes": "n"} done.')
    assert out == {"status": "missing", "notes": "n"}


def test_extract_json_garbage_is_empty():
    assert _extract_json("no json at all") == {}
    assert _extract_json("") == {}


# --- evaluator -----------------------------------------------------------------


def test_evaluator_covered_with_evidence():
    ev = make_llm_evaluator(
        _fixed('{"status":"covered","evidence":"the plan does X","matched_ids":["C1"],"confidence":0.9}')
    )
    r = ev(Feature("A", "feature a", derived=True), [])
    assert r.status == COVERED
    assert r.evidence == "the plan does X"
    assert r.matched_ids == ["C1"]
    assert r.derived is True  # engine/parser carry the flag


def test_evaluator_covered_without_evidence_is_downgraded():
    r = make_llm_evaluator(_fixed('{"status":"covered","evidence":""}'))(Feature("A", "a"), [])
    assert r.status == MISSING  # evidence-required


def test_evaluator_unparseable_is_missing():
    r = make_llm_evaluator(_fixed("the model rambled with no json"))(Feature("A", "a"), [])
    assert r.status == MISSING
    assert "unparseable" in r.notes


def test_evaluator_missing_passthrough():
    assert make_llm_evaluator(_fixed('{"status":"missing"}'))(Feature("A", "a"), []).status == MISSING


# --- refuter -------------------------------------------------------------------


def test_refuter_true_false_and_default():
    part = Feature("A", "a")
    res = PartResult("A", COVERED, evidence="some candidate text")
    assert make_llm_refuter(_fixed('{"refuted":true}'))(part, res, []) is True
    assert make_llm_refuter(_fixed('{"refuted":false}'))(part, res, []) is False
    assert make_llm_refuter(_fixed("garbage"))(part, res, []) is True  # default-refuted on trouble


# --- tiered (lexical fast-path) ------------------------------------------------


def test_tiered_fast_path_skips_model_on_near_identical_match():
    calls: list[str] = []

    def complete(prompt):
        calls.append(prompt)
        return '{"status":"missing"}'

    ev = make_tiered_evaluator(complete, lexical_cover_threshold=0.85)
    part = Feature("A", "alpha beta gamma delta")
    res = ev(part, [Feature("C", "alpha beta gamma delta")])  # identical -> overlap 1.0
    assert res.status == COVERED
    assert calls == []  # model NOT called on the obvious match


def test_tiered_escalates_ambiguous_to_model():
    calls: list[str] = []

    def complete(prompt):
        calls.append(prompt)
        return '{"status":"missing"}'

    ev = make_tiered_evaluator(complete, lexical_cover_threshold=0.85)
    res = ev(Feature("B", "obscure quartz widget"), [Feature("D", "entirely different wording")])
    assert res.status == MISSING
    assert calls  # model WAS called on the ambiguous case


# --- engine integration (judge + targeted adversarial) -------------------------


def test_run_coverage_with_llm_evaluator_and_refuter_downgrades_derived():
    # A derived part is judged covered, but the adversarial refuter rejects it -> MISSING.
    complete = _router(
        judge='{"status":"covered","evidence":"the plan does X","confidence":0.95}',
        refuter='{"refuted":true,"reason":"only partially covers it"}',
    )
    parts = [Feature("A", "a derived feature", derived=True)]
    rep = run_coverage(
        parts, [Feature("C", "the plan does X")],
        all_related_query, make_llm_evaluator(complete), refuter=make_llm_refuter(complete),
    )
    r = rep.results[0]
    assert r.status == MISSING
    assert r.adversarial == {"ran": True, "refuted": True}
    assert not rep.passed


def test_run_coverage_keeps_covered_when_refuter_clears_it():
    complete = _router(
        judge='{"status":"covered","evidence":"the plan does X","confidence":0.95}',
        refuter='{"refuted":false}',
    )
    parts = [Feature("A", "a derived feature", derived=True)]
    rep = run_coverage(
        parts, [Feature("C", "the plan does X")],
        all_related_query, make_llm_evaluator(complete), refuter=make_llm_refuter(complete),
    )
    assert rep.results[0].status == COVERED
    assert rep.passed
