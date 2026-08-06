"""U5+U6: run a GRADED validation at VERIFY, with content-hash caching and loop-termination guards.

Sits beside :mod:`_ticket_state` (the live gate) and ties it to the pure rubric math
(:mod:`agent_factory.rubric`) and the fresh-context judge (:mod:`agent_factory.graded_verdict`).

The whole point is to keep the gate untouched: a graded check still records a single ``passed``
boolean via :func:`_ticket_state.record_validation_pass`, so ``all_validations_passed`` never learns
about rubrics. What this module adds around that boolean:

* **content-hash cache** — grade a given code-state once; identical code reuses the cached verdict
  with NO judge call and consumes NO iteration. This eliminates flapping (a nondeterministic judge
  cannot change its mind on code that did not change).
* **frozen rubric** — the rubric is read from the PINNED entry (frozen at synthesis time by
  ``_ticket_state._norm_validation``), never re-read live, so an edit to the seeded library cannot
  move the target under an in-progress ticket.
* **iteration cap + defect-count monotonicity** — a graded check that keeps failing across CHANGING
  code trips a bounded escalation (``should_block``) instead of looping forever; the caller
  (af-build) routes that to ``_ticket_state.block`` (HITL), never ``incomplete`` forever. An
  UNCHANGED (cache-hit) failing code-state also counts toward the same cap (R33) — otherwise a
  ticket that never edits its diff would re-verify the identical cached failure forever without
  ever tripping the escalation.
* **advise-tier unremediable escalation** (R33) — a verdict failing on ADVISE-tier defects only
  (see :data:`agent_factory.rubric.DEFECT_TIER_ADVISE`) has no ENFORCE-tier lever the worker can
  act on, so it escalates immediately as unremediable instead of consuming the iteration cap.
* **path-predicate exemption** (R33) — a UNIVERSAL-lane pinned entry (``meta.universal``) whose
  ACTUAL touched paths (parsed from ``code_diff``, which this module already receives) sit entirely
  inside an immutable/generated directory is auto-passed here ("pass-by-exemption") with no judge
  call and no human flag — the grade-time re-check of ``_ticket_state._universal_exempt`` WINS over
  whatever the pin-time (declared-paths) phase decided, discharging the requirement rather than
  stalling completion on a hand-authored covering validation.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

import _ticket_state as ts

from agent_factory.graded_verdict import grade
from agent_factory.rubric import DEFECT_TIER_ADVISE, Rubric, Verdict, rubric_from_dict

Complete = Callable[[str], str]

GRADED_SOURCE = "graded-judge"
PATH_EXEMPTION_SOURCE = "path-exemption"  # R33: auto-pass, never a human/attested source
# An EMPTY diff is "nothing to grade", not "graded and clean". Asking a judge to
# score a code-quality rubric against zero lines gets a confident pass -- observed
# on appeal_engine DATA-1: "the rubric is trivially satisfied on an empty diff: no
# dead code, no duplication, no fragmentation, because nothing was added." That
# reads in the ticket exactly like a real verdict on real code, and it is what let
# a ticket record two green graded checks having evaluated nothing.
#
# It still PASSES, deliberately: a ticket whose remaining work is operational
# (run a harvest, sync a bucket) legitimately has no diff, and failing it would
# block honest work. The fix is that it never masquerades as a judge verdict --
# its own source makes "not evaluated" greppable and auditable, exactly as
# path-exemption does for its case.
EMPTY_DIFF_SOURCE = "no-diff"
DEFAULT_GRADED_ITER_CAP = 3
M_GRADED_LOOP = ts.M_GRADED_LOOP  # {validation_id: {iters, last_defects, last_hash}} — shared with
                                  # _ticket_state.claim(), which resets it on a fresh ticket pick.

_DIFF_PATH_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class GradedResult:
    verdict: Verdict
    cached: bool
    iterations: int
    should_block: bool
    block_reason: str = ""


def code_state_hash(code_diff: str) -> str:
    """Stable hash of the graded code-state. The caller passes the diff of the ticket's touched
    paths; identical content -> identical verdict, so the cache keys on this."""
    return hashlib.sha256((code_diff or "").encode("utf-8")).hexdigest()


def _pinned_entry(meta: dict, validation_id: str) -> Optional[dict]:
    for e in (meta.get(ts.M_PINNED_CHECKS) or []):
        if str(e.get("validation_id") or e.get("check_id")) == str(validation_id):
            return e
    return None


def frozen_rubric_for(meta: dict, validation_id: str) -> Optional[Rubric]:
    """The rubric frozen onto the pinned validation at synthesis time (U6). ``None`` if the entry
    is not graded / carries no rubric."""
    e = _pinned_entry(meta, validation_id)
    if e and isinstance(e.get("rubric"), dict):
        return rubric_from_dict(e["rubric"])
    return None


def _verdict_payload(v: Verdict, code_hash: str) -> dict:
    return {
        "code_hash": code_hash,
        "passed": v.passed,
        "min_axis": v.min_axis,
        "reason": v.reason,
        "axis_scores": dict(v.axis_scores),
        "defects": [
            {"file": d.file, "line": d.line, "problem": d.problem,
             "remedy": d.remedy, "confidence": d.confidence, "tier": d.tier}
            for d in v.defects
        ],
    }


def _paths_from_diff(code_diff: str) -> list[str]:
    """The ACTUAL touched paths for the GRADE-TIME phase of the R33 path-predicate exemption —
    parsed from the unified diff the caller already holds (no new plumbing). Reads the ``+++``
    (post-image) header of each file hunk; ``/dev/null`` (a deletion) is skipped since a deleted
    path proves nothing about what NEW code needs grading."""
    paths = []
    for m in _DIFF_PATH_RE.finditer(code_diff or ""):
        p = m.group("path").strip()
        if p and p != "/dev/null":
            paths.append(p)
    return paths


def _pass_by_exemption(cid: str, validation_id: str, entry: dict, h: str, now: float,
                       ref: Optional[tuple[str, str]]) -> GradedResult:
    """Auto-discharge a UNIVERSAL requirement whose grade-time touched paths are all exempt — no
    judge call, no human flag, no iteration consumed. See module docstring."""
    v = Verdict(passed=True, reason="pass-by-exemption: touched paths are all "
               "immutable/generated (path-predicate exemption)")
    ts.record_validation_pass(cid, validation_id, True, ran_at=now,
                              source=PATH_EXEMPTION_SOURCE, verdict=_verdict_payload(v, h), ref=ref)
    return GradedResult(v, cached=False, iterations=0, should_block=False)


def _pass_no_diff(cid: str, validation_id: str, h: str, now: float,
                  ref: Optional[tuple[str, str]]) -> GradedResult:
    """Discharge a graded requirement that has no code to grade — no judge call.

    Recorded under its own source so the ticket says "not evaluated" instead of
    wearing a judge verdict it never earned. See EMPTY_DIFF_SOURCE.
    """
    v = Verdict(passed=True, reason="no code diff to grade — rubric not evaluated "
               "(this is NOT a judge verdict on the code)")
    ts.record_validation_pass(cid, validation_id, True, ran_at=now,
                              source=EMPTY_DIFF_SOURCE, verdict=_verdict_payload(v, h), ref=ref)
    return GradedResult(v, cached=False, iterations=0, should_block=False)


def verify_graded_check(cid: str, validation_id: str, code_diff: str, complete: Complete, *,
                        rubric: Optional[Rubric] = None,
                        cap: int = DEFAULT_GRADED_ITER_CAP,
                        ref: Optional[tuple[str, str]] = None,
                        now: Optional[float] = None) -> GradedResult:
    """Verify ONE graded validation. Records its ``passed`` via the normal sink and returns a
    :class:`GradedResult`; the caller blocks the ticket when ``should_block`` is set.

    ``rubric`` defaults to the FROZEN rubric on the pinned entry — pass an explicit one only in
    tests. Raises ``ValueError`` if neither is available (a graded check with no rubric is a bug).
    """
    now = now if now is not None else time.time()
    meta = ts._meta(cid, ref)
    entry = _pinned_entry(meta, validation_id) or {}
    rubric = rubric or frozen_rubric_for(meta, validation_id)
    if rubric is None:
        raise ValueError(f"graded validation {validation_id!r} has no frozen rubric to grade against")

    h = code_state_hash(code_diff)

    # NOTHING TO GRADE. Checked before the exemption and the cache: with no diff
    # there are no paths for the predicate to judge and no code-state worth
    # caching a judge verdict against. See EMPTY_DIFF_SOURCE for why this passes
    # rather than fails, and why it must not go to the judge.
    if not (code_diff or "").strip():
        return _pass_no_diff(cid, validation_id, h, now, ref)

    # PATH-PREDICATE EXEMPTION (R33), grade-time phase — only for the UNIVERSAL lane, and only
    # AHEAD of the cache check: the grade-time result WINS whenever it disagrees with whatever the
    # pin-time phase (or a prior cached judge verdict) decided, so a ticket touching only e.g.
    # migrations/ discharges the requirement even if an earlier pass had it failing/cached.
    if entry.get("universal") and ts._universal_exempt(meta, paths=_paths_from_diff(code_diff)):
        return _pass_by_exemption(cid, validation_id, entry, h, now, ref)

    cached = entry.get("verdict") or {}
    loop_all = dict(meta.get(M_GRADED_LOOP) or {})
    loop = dict(loop_all.get(str(validation_id)) or {})

    # CACHE HIT — same code-state: reuse the verdict, no judge call, and the reported ``iterations``
    # never advances (the anti-flap guarantee — a nondeterministic judge cannot change its mind on
    # code that did not change, and re-verifying unchanged PASSING code must never cost an
    # iteration). A FAILING cache hit is different: it is tracked on its OWN counter
    # (``cache_repeats``, separate from ``iters``) so ``should_block`` still trips within ``cap``
    # (R33) — otherwise a ticket whose diff never changes could re-verify the same cached failure
    # forever without ever escalating, since the fresh-grade path below (which advances ``iters``)
    # never runs for it.
    if cached.get("code_hash") == h:
        v = Verdict(passed=bool(cached.get("passed")),
                    axis_scores=dict(cached.get("axis_scores") or {}),
                    min_axis=cached.get("min_axis"),
                    reason=str(cached.get("reason") or ""))
        if v.passed:
            return GradedResult(v, cached=True, iterations=int(loop.get("iters", 0)),
                                should_block=False)
        repeats = int(loop.get("cache_repeats", 0)) + 1
        should_block, reason = False, ""
        if repeats >= cap:
            should_block = True
            reason = (f"graded check {validation_id} failed {repeats} check(s) of an UNCHANGED "
                      f"code-state (cap {cap}) — the diff never changed, escalating instead of "
                      f"looping")
        loop["cache_repeats"] = repeats
        loop_all[str(validation_id)] = loop
        ts._praxis.write_build_state(cid, {M_GRADED_LOOP: loop_all}, **ts._ref_kw(ref))
        return GradedResult(v, cached=True, iterations=int(loop.get("iters", 0)),
                            should_block=should_block, block_reason=reason)

    # Code changed — grade fresh against the frozen rubric.
    v = grade(complete, rubric, code_diff)
    n_defects = len(v.defects)
    iters = int(loop.get("iters", 0)) + 1
    should_block, reason = False, ""
    if not v.passed:
        # UNREMEDIABLE (R33): every credible defect is ADVISE-tier — there is no ENFORCE-tier lever
        # the worker can pull, so escalate immediately rather than burn the iteration cap re-grading
        # feedback that cannot, by its own tier, be acted on to flip the verdict.
        if v.defects and all(d.tier == DEFECT_TIER_ADVISE for d in v.defects):
            should_block = True
            reason = (f"graded check {validation_id} fails only on advise-tier defect(s) — "
                      f"unremediable, no enforce-tier defect to fix")
        else:
            last_defects = loop.get("last_defects")
            if iters >= cap:
                should_block = True
                reason = f"graded check {validation_id} failed {iters} iteration(s) (cap {cap})"
            elif last_defects is not None and last_defects > 0 and n_defects >= last_defects:
                should_block = True
                reason = (f"graded check {validation_id} not converging "
                          f"({last_defects} -> {n_defects} defects)")

    loop.update(iters=iters, last_defects=n_defects, last_hash=h, cache_repeats=0)
    loop_all[str(validation_id)] = loop
    ts._praxis.write_build_state(cid, {M_GRADED_LOOP: loop_all}, **ts._ref_kw(ref))
    ts.record_validation_pass(cid, validation_id, v.passed, ran_at=now,
                              source=GRADED_SOURCE, verdict=_verdict_payload(v, h), ref=ref)
    return GradedResult(v, cached=False, iterations=iters,
                        should_block=should_block, block_reason=reason)
