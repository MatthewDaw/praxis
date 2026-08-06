"""R33 acceptance: the universal-exemption predicate gains a ``paths`` argument evaluated in two
phases -- declared paths at pin time inside :func:`_ticket_state.start_ticket`, then actual touched
paths re-checked at grade time (:mod:`_graded_verify`, which already holds the code diff) -- and the
grade-time result WINS when the two disagree. Bundled with it (absorbed R34/R35/R36):

  * coverage authoring: a non-exempt ticket reaches ``all_validations_passed`` with no hand-authored
    covering validation for the universal lane.
  * pass-by-exemption: a ticket touching only ``migrations/`` auto-discharges the universal check at
    grade time, with no human-sourced pass.
  * deadlock escape: a grade-time-exempted universal requirement is discharged, not a stall.
  * graded loop reset: a re-picked ticket starts a fresh iteration budget.
  * loop termination: a ticket whose diff never changes still escalates within the cap.
  * unremediable verdict: a ticket failing only on advise-tier defects escalates immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _graded_verify as gv  # noqa: E402
import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402
from agent_factory.rubric import rubric_from_dict  # noqa: E402
from agent_factory.seeded_checks import SeededCheck  # noqa: E402

PLAN = ("r33-app", "prd-r33-app")

RUBRIC = {"confidence_floor": 5, "criterion": "c",
          "axes": [{"name": "a", "threshold": 0.7}]}


def _universal_check(report_only: bool) -> SeededCheck:
    """The gating flag FORCED locally for this test harness: ``report_only=False`` makes the
    universal lane a real gate rather than calibration-only, so the acceptance's "reaches
    all_validations_passed" claim is actually load-bearing here."""
    rubric = rubric_from_dict({
        "axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "no dead code"}],
    })
    return SeededCheck(check_id="minimalism-dry", kind="graded", applies_to=("*",),
                       criterion="strict minimization", promote_universal=True,
                       rubric=rubric, report_only=report_only)


class FakePraxis(SanctionedWrites):
    """Persists ONE ticket's meta across calls; ``patch_meta`` MERGES like the real server."""

    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def _install(monkeypatch, meta, *, report_only=False):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts._praxis, "facts_by", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts._praxis, "surface_checks", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts, "_universal_checks", lambda: [_universal_check(report_only)])
    return fake


def _stub(passed_axis: float, defects=None, axis: str = "a"):
    import json
    payload = {"axis_scores": {axis: passed_axis}, "defects": defects or []}
    calls = {"n": 0}

    def complete(_prompt):
        calls["n"] += 1
        return json.dumps(payload)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


MIGRATION_DIFF = (
    "diff --git a/migrations/0001_init.sql b/migrations/0001_init.sql\n"
    "--- a/migrations/0001_init.sql\n"
    "+++ b/migrations/0001_init.sql\n"
    "@@ -0,0 +1 @@\n"
    "+CREATE TABLE widgets (id int);\n"
)

APP_DIFF = (
    "diff --git a/app/widgets.py b/app/widgets.py\n"
    "--- a/app/widgets.py\n"
    "+++ b/app/widgets.py\n"
    "@@ -0,0 +1 @@\n"
    "+def widgets(): return []\n"
)


# ------------------------------------------------------------ 1) coverage authoring, forced gating

def test_non_exempt_ticket_finishes_with_no_hand_authored_universal_validation(monkeypatch):
    fake = _install(monkeypatch, {
        "requirement_id": "R1", "tags": [], "acceptance": "thing works",
    }, report_only=False)

    ts.start_ticket("R1", "owner", project="r33-app")

    # The worker authors + pins a covering validation ONLY for its own acceptance floor -- never
    # one covering the universal "minimalism-dry" requirement.
    ts.pin_validations("R1", [{"validation_id": "v-acc", "covers": ["R1::acceptance"],
                               "run": "true"}], ref=PLAN)

    pinned_ids = {e["validation_id"] for e in fake._meta[ts.M_PINNED_CHECKS]}
    assert "minimalism-dry" in pinned_ids, "the universal lane must auto-author its own coverage"

    ts.record_validation_pass("R1", "v-acc", True, ref=PLAN)
    # Grade the auto-authored universal entry -- a real judge call, not a hand-authored validation.
    gv.verify_graded_check("R1", "minimalism-dry", APP_DIFF, _stub(0.9, axis="minimalism"),
                           ref=PLAN, now=1.0)

    assert ts.all_validations_passed("R1", ref=PLAN) is True


# ------------------------------------------------------------ 2) + 5) pass-by-exemption / discharge

def test_migrations_only_ticket_passes_by_exemption_no_human_flag(monkeypatch):
    fake = _install(monkeypatch, {
        "requirement_id": "R2", "tags": [], "acceptance": "add the widgets table",
    }, report_only=False)

    ts.start_ticket("R2", "owner", project="r33-app")
    ts.pin_validations("R2", [{"validation_id": "v-acc", "covers": ["R2::acceptance"],
                               "run": "true"}], ref=PLAN)
    ts.record_validation_pass("R2", "v-acc", True, ref=PLAN)

    def _never_call(_prompt):
        raise AssertionError("the judge must never be called for a path-exempt diff")

    result = gv.verify_graded_check("R2", "minimalism-dry", MIGRATION_DIFF, _never_call,
                                    ref=PLAN, now=1.0)
    assert result.verdict.passed and not result.should_block

    entry = next(e for e in fake._meta[ts.M_PINNED_CHECKS] if e["validation_id"] == "minimalism-dry")
    assert entry["passed"] is True
    assert entry["source"] == "path-exemption"
    assert entry["source"] not in ts.HUMAN_PASS_SOURCES  # no human flag

    # Discharged, not stalling completion -- the whole ticket can finish.
    assert ts.coverage_gap("R2", ref=PLAN) == []
    assert ts.all_validations_passed("R2", ref=PLAN) is True


def test_grade_time_exemption_wins_over_pin_time_non_exemption(monkeypatch):
    """Pin time had no declared paths (conservative default: not exempt, so the check was included
    in the contract) -- grade time, which HAS the real diff, discovers it is migrations-only and
    the exemption WINS, discharging a requirement pin time could not have known was exempt."""
    fake = _install(monkeypatch, {
        "requirement_id": "R3", "tags": [], "acceptance": "add another table",
    }, report_only=False)
    ts.start_ticket("R3", "owner", project="r33-app")
    assert "minimalism-dry" in fake._meta[ts.M_REQUIRED_VALIDATIONS]  # pin time: not exempt

    ts.pin_validations("R3", [{"validation_id": "v-acc", "covers": ["R3::acceptance"],
                               "run": "true"}], ref=PLAN)
    result = gv.verify_graded_check("R3", "minimalism-dry", MIGRATION_DIFF,
                                    lambda p: (_ for _ in ()).throw(AssertionError("no judge call")),
                                    ref=PLAN, now=1.0)
    assert result.verdict.passed


# ------------------------------------------------------------ 3) escalate within cap, no looping

def test_unchanged_diff_escalates_within_cap_instead_of_looping(monkeypatch):
    _install(monkeypatch, {})
    ts.pin_validations("R4", [{"validation_id": "v1", "covers": ["R4"],
                               "kind": "graded", "rubric": RUBRIC}], ref=PLAN)
    stub = _stub(0.3, [{"file": "x", "line": 1, "problem": "p", "remedy": "r", "confidence": 8}])

    r1 = gv.verify_graded_check("R4", "v1", "frozen-diff", stub, ref=PLAN, now=1.0)
    assert not r1.should_block and not r1.cached
    for n in (2.0, 3.0):
        r = gv.verify_graded_check("R4", "v1", "frozen-diff", stub, ref=PLAN, now=n)
        assert r.cached
        if n < 3.0:
            assert not r.should_block
    r_final = gv.verify_graded_check("R4", "v1", "frozen-diff", stub, ref=PLAN, now=4.0)
    assert r_final.cached and r_final.should_block
    assert "unchanged" in r_final.block_reason.casefold()
    # Only ONE judge call across every repeat of the identical diff.
    assert stub.calls["n"] == 1


# ------------------------------------------------------------ 4) unremediable advise-tier verdict

def test_advise_tier_only_failure_escalates_as_unremediable(monkeypatch):
    _install(monkeypatch, {})
    ts.pin_validations("R5", [{"validation_id": "v1", "covers": ["R5"],
                               "kind": "graded", "rubric": RUBRIC}], ref=PLAN)
    advise_defect = {"file": "x", "line": 1, "problem": "p", "remedy": "r",
                     "confidence": 8, "tier": "advise"}
    r = gv.verify_graded_check("R5", "v1", "diffA", _stub(0.3, [advise_defect]), ref=PLAN, now=1.0)
    assert not r.verdict.passed
    assert r.should_block  # escalates on the FIRST failure, well under the default cap
    assert "unremediable" in r.block_reason.casefold()


def test_enforce_tier_defect_does_not_escalate_early(monkeypatch):
    """Contrast case: a single ENFORCE-tier defect behaves exactly as before -- no early escalation."""
    _install(monkeypatch, {})
    ts.pin_validations("R6", [{"validation_id": "v1", "covers": ["R6"],
                               "kind": "graded", "rubric": RUBRIC}], ref=PLAN)
    enforce_defect = {"file": "x", "line": 1, "problem": "p", "remedy": "r", "confidence": 8}
    r = gv.verify_graded_check("R6", "v1", "diffA", _stub(0.3, [enforce_defect]), ref=PLAN, now=1.0)
    assert not r.verdict.passed and not r.should_block


# ------------------------------------------------------------ 6) re-pick resets the loop budget

def test_repicked_ticket_starts_a_fresh_iteration_budget(monkeypatch):
    fake = _install(monkeypatch, {
        ts.M_BUILD_STATE: "incomplete",  # not a live lease -- free to (re-)pick
        ts.M_GRADED_LOOP: {"v1": {"iters": 99, "cache_repeats": 99, "last_defects": 5}},
    })
    assert ts.claim("R7", "new-owner", ref=PLAN) is True
    assert fake._meta[ts.M_GRADED_LOOP] == {}


def test_idempotent_renew_by_same_live_owner_keeps_the_loop_budget(monkeypatch):
    now = __import__("time").time()
    fake = _install(monkeypatch, {
        ts.M_BUILD_STATE: "in_progress",
        ts.M_CLAIM_OWNER: "owner-a",
        ts.M_CLAIM_HEARTBEAT_AT: now,
        ts.M_CLAIM_LEASE_TTL: 900,
        ts.M_GRADED_LOOP: {"v1": {"iters": 2}},
    })
    assert ts.claim("R8", "owner-a", ref=PLAN) is True
    assert fake._meta[ts.M_GRADED_LOOP] == {"v1": {"iters": 2}}
