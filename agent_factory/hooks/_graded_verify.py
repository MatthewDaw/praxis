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
  (af-build) routes that to ``_ticket_state.block`` (HITL), never ``incomplete`` forever.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable, Optional

import _ticket_state as ts

from agent_factory.graded_verdict import grade
from agent_factory.rubric import Rubric, Verdict, rubric_from_dict

Complete = Callable[[str], str]

GRADED_SOURCE = "graded-judge"
DEFAULT_GRADED_ITER_CAP = 3
M_GRADED_LOOP = "graded_loop"  # {validation_id: {iters, last_defects, last_hash}}


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
             "remedy": d.remedy, "confidence": d.confidence}
            for d in v.defects
        ],
    }


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
    rubric = rubric or frozen_rubric_for(meta, validation_id)
    if rubric is None:
        raise ValueError(f"graded validation {validation_id!r} has no frozen rubric to grade against")

    h = code_state_hash(code_diff)
    entry = _pinned_entry(meta, validation_id)
    cached = (entry or {}).get("verdict") or {}
    loop_all = dict(meta.get(M_GRADED_LOOP) or {})
    loop = dict(loop_all.get(str(validation_id)) or {})

    # CACHE HIT — same code-state: reuse the verdict, no judge call, no iteration consumed.
    if cached.get("code_hash") == h:
        v = Verdict(passed=bool(cached.get("passed")),
                    axis_scores=dict(cached.get("axis_scores") or {}),
                    min_axis=cached.get("min_axis"),
                    reason=str(cached.get("reason") or ""))
        return GradedResult(v, cached=True, iterations=int(loop.get("iters", 0)),
                            should_block=False)

    # Code changed — grade fresh against the frozen rubric.
    v = grade(complete, rubric, code_diff)
    n_defects = len(v.defects)
    iters = int(loop.get("iters", 0)) + 1
    should_block, reason = False, ""
    if not v.passed:
        last_defects = loop.get("last_defects")
        if iters >= cap:
            should_block = True
            reason = f"graded check {validation_id} failed {iters} iteration(s) (cap {cap})"
        elif last_defects is not None and last_defects > 0 and n_defects >= last_defects:
            should_block = True
            reason = (f"graded check {validation_id} not converging "
                      f"({last_defects} -> {n_defects} defects)")

    loop.update(iters=iters, last_defects=n_defects, last_hash=h)
    loop_all[str(validation_id)] = loop
    ts._praxis.patch_meta(cid, {M_GRADED_LOOP: loop_all}, **ts._ref_kw(ref))
    ts.record_validation_pass(cid, validation_id, v.passed, ran_at=now,
                              source=GRADED_SOURCE, verdict=_verdict_payload(v, h), ref=ref)
    return GradedResult(v, cached=False, iterations=iters,
                        should_block=should_block, block_reason=reason)
