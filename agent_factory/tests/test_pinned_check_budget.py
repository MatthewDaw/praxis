"""FL16 (R15/S2/DF2): cost-tiered checks + a per-ticket pinned-check budget.

Checks carry a cost tier from static/cheap up through browser/LLM/expensive. Each ticket's
resolved (non-identity-lane) check set is ordered cheapest-first and capped at a budget; any
check beyond the budget demotes to report-only (``meta.report_only`` + a recorded
``meta.demoted_reason``) via the SAME report-only lane :func:`_ticket_state.pin_requirements` /
:func:`_ticket_state.all_validations_passed` already honor for the universal lane — so a demoted
check stays PINNED and still counts as covering its requirement (never opens a coverage gap or
blocks FINISH). Ticket-identity-lane checks (R11/FL6) are UNCONDITIONALLY exempt: S1 (a proven
check can never recur silently) outranks the latency budget (DF2), which outranks widening
ambition. A ticket with no relevant failure history (no more than the budget's worth of ordinary
checks) pins the same count regardless of how large the corpus of OTHER checks grows (S2).
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402

PLAN = ("fl16-app", "prd-fl16-app")


class _DBSpy:
    """In-memory checks DB: array-membership match on every key of a ``meta`` query filter."""

    def __init__(self, checks=None):
        self._checks = checks or []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        meta = meta or {}
        out = []
        for c in self._checks:
            cmeta = c.get("meta") or {}
            if all(v in (cmeta.get(k) or []) for k, v in meta.items()):
                out.append(c)
        return out

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []

    def context(self, query, top_k=10, as_of=None, space=None, snapshot=None):
        return []

    def get_fact(self, cid, space=None, snapshot=None, not_found_ok=False):
        return {"id": cid, "text": cid, "meta": {}}


def _check(cid, applies_to, run="grep -q TODO file.py", scope="validation", **extra_meta):
    meta = {"applies_to": applies_to, "scope": scope, "run": run, **extra_meta}
    return {"id": cid, "category": "check", "scope": scope, "meta": meta}


def _install(monkeypatch, checks):
    monkeypatch.setattr(ts, "_praxis", _DBSpy(checks=checks))


def _ticket(tid="T1", tags=("backend",)):
    return {"id": tid, "meta": {"tags": list(tags)}}


# --------------------------------------------------------------------------- cost tier classifier

def test_cost_tier_ranks_static_below_testrunner_below_browser_below_llm():
    static_chk = _check("c-static", ["backend"], run="grep -q TODO file.py")
    runner_chk = _check("c-runner", ["backend"], run="pytest tests/test_x.py -q")
    browser_chk = _check("c-browser", ["backend"], run="npx playwright test e2e/login.spec.ts")
    llm_chk = _check("c-llm", ["backend"], run="", kind="graded", rubric={"axes": []})
    assert ts.cost_tier(static_chk) < ts.cost_tier(runner_chk)
    assert ts.cost_tier(runner_chk) < ts.cost_tier(browser_chk)
    assert ts.cost_tier(browser_chk) < ts.cost_tier(llm_chk)


# --------------------------------------------------------------------------- cheapest-first + budget

def test_resolved_checks_are_ordered_cheapest_first(monkeypatch):
    _install(monkeypatch, [
        _check("c-llm", ["backend"], run="", kind="graded", rubric={"axes": []}),
        _check("c-static", ["backend"], run="grep -q TODO file.py"),
        _check("c-browser", ["backend"], run="npx playwright test e2e/login.spec.ts"),
        _check("c-runner", ["backend"], run="pytest tests/test_x.py -q"),
    ])
    resolved = ts.resolve_validation_requirements(_ticket(), project="p")
    assert [c["id"] for c in resolved] == ["c-static", "c-runner", "c-browser", "c-llm"]


def test_budget_overflow_demotes_most_expensive_non_identity_checks_to_report_only(monkeypatch):
    checks = [
        _check("c-static", ["backend"], run="grep -q TODO file.py"),
        _check("c-runner1", ["backend"], run="pytest tests/test_a.py -q"),
        _check("c-runner2", ["backend"], run="pytest tests/test_b.py -q"),
        _check("c-browser", ["backend"], run="npx playwright test e2e/login.spec.ts"),
        _check("c-llm", ["backend"], run="", kind="graded", rubric={"axes": []}),
    ]
    _install(monkeypatch, checks)
    resolved = ts.resolve_validation_requirements(_ticket(), project="p", budget=3)
    by_id = {c["id"]: c for c in resolved}
    # cheapest 3 stay gating
    for cid in ("c-static", "c-runner1", "c-runner2"):
        assert not by_id[cid]["meta"].get("report_only")
    # the two most expensive overflow into report-only, with a recorded reason
    for cid in ("c-browser", "c-llm"):
        assert by_id[cid]["meta"].get("report_only") is True
        assert by_id[cid]["meta"].get("demoted_reason") == "budget-overflow"


def test_identity_lane_checks_are_exempt_from_budget_demotion(monkeypatch):
    # The ticket's OWN identity-bound check (R11/FL6) must stay gating even though it is by far the
    # most expensive entry and the budget is exhausted by cheaper ordinary checks.
    _install(monkeypatch, [
        _check("id-check", ["T1"], run="", kind="graded", rubric={"axes": []}),
        _check("c-static1", ["backend"], run="grep -q A file.py"),
        _check("c-static2", ["backend"], run="grep -q B file.py"),
    ])
    resolved = ts.resolve_validation_requirements(_ticket(), project="p", budget=2)
    by_id = {c["id"]: c for c in resolved}
    assert by_id["id-check"]["meta"]["identity_lane"] is True
    assert not by_id["id-check"]["meta"].get("report_only")
    assert not by_id["c-static1"]["meta"].get("report_only")
    assert not by_id["c-static2"]["meta"].get("report_only")


def test_ticket_with_no_relevant_failure_history_pins_same_count_as_corpus_grows(monkeypatch):
    # Baseline: only the checks that actually apply to THIS ticket's tags matter, regardless of how
    # many OTHER checks exist in the corpus (checks bound to unrelated tags never resolve here).
    baseline_checks = [_check(f"c-base{i}", ["backend"], run=f"grep -q X{i} file.py")
                       for i in range(3)]
    _install(monkeypatch, baseline_checks)
    baseline = ts.resolve_validation_requirements(_ticket(), project="p", budget=8)
    assert len(baseline) == 3
    assert not any(c["meta"].get("report_only") for c in baseline)

    # Corpus grows hugely with checks bound to UNRELATED tags/tickets — this ticket's own resolved
    # set (and gating count) must not change.
    grown_checks = baseline_checks + [
        _check(f"c-other{i}", ["frontend"], run=f"grep -q Y{i} file.py") for i in range(50)
    ]
    _install(monkeypatch, grown_checks)
    grown = ts.resolve_validation_requirements(_ticket(), project="p", budget=8)
    assert len(grown) == 3
    assert not any(c["meta"].get("report_only") for c in grown)


def test_widening_a_check_into_a_full_scope_lands_report_only_there(monkeypatch):
    # Simulates R17 widen(): a check that used to apply only elsewhere now ALSO applies to this
    # ticket's tag (post-widen `applies_to`); if the target scope's budget is already exhausted by
    # cheaper checks, the widened check must land report-only there, never silently gating.
    _install(monkeypatch, [
        _check("c-cheap1", ["backend"], run="grep -q A file.py"),
        _check("c-cheap2", ["backend"], run="grep -q B file.py"),
        _check("c-widened", ["backend"], run="npx playwright test e2e/full.spec.ts"),
    ])
    resolved = ts.resolve_validation_requirements(_ticket(), project="p", budget=2)
    by_id = {c["id"]: c for c in resolved}
    assert by_id["c-widened"]["meta"].get("report_only") is True
    assert by_id["c-widened"]["meta"].get("demoted_reason") == "budget-overflow"


# --------------------------------------------------------------------------- pin + finish interplay

class FakePraxis(SanctionedWrites):
    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def test_demoted_check_is_recorded_and_finish_is_still_reachable(monkeypatch):
    fake = FakePraxis({})
    monkeypatch.setattr(ts, "_praxis", fake)

    checks = [
        _check("c-cheap", ["backend"], run="grep -q A file.py"),
        _check("c-expensive", ["backend"], run="npx playwright test e2e/full.spec.ts"),
    ]
    monkeypatch.setattr(ts._praxis, "facts_by",
                        lambda category=None, meta=None, state="active", space=None, snapshot=None: (
                            [c for c in checks
                             if all(v in (c.get("meta") or {}).get(k, []) for k, v in (meta or {}).items())]
                        ), raising=False)
    monkeypatch.setattr(ts._praxis, "surface_checks", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts._praxis, "context", lambda *a, **k: [], raising=False)

    resolved = ts.resolve_validation_requirements(_ticket(), project="p", budget=1)
    pinned = ts.pin_requirements("T1", resolved, ref=PLAN)["meta"]

    # the demoted requirement's reason is recorded, queryable off the ticket's own build state
    assert pinned["report_only_requirements"] == ["c-expensive"]
    assert pinned["budget_demotions"] == {"c-expensive": "budget-overflow"}
    assert set(pinned["required_validations"]) == {"c-cheap", "c-expensive"}

    # the worker authors ONE covering validation per requirement, including the demoted one —
    # a demoted check stays pinned and counts as covering its requirement.
    ts.pin_validations("T1", [
        {"validation_id": "v-cheap", "covers": ["c-cheap"], "run": "grep -q A file.py"},
        {"validation_id": "v-expensive", "covers": ["c-expensive"],
         "run": "npx playwright test e2e/full.spec.ts"},
    ], ref=PLAN)
    ts.record_validation_pass("T1", "v-cheap", True, ref=PLAN)
    # The demoted check's OWN validation FAILS — because it is report-only this must never block
    # FINISH: it is recorded (calibration) but non-gating.
    ts.record_validation_pass("T1", "v-expensive", False, ref=PLAN)

    assert ts.coverage_gap("T1", ref=PLAN) == []
    assert ts.all_validations_passed("T1", ref=PLAN) is True
