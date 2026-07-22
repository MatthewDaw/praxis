"""U5 — deterministic per-ticket rubric assembly over the shared candidate pool.

At build-time synthesis the af-build worker calls :func:`assemble` with the ticket's
``pool_candidates`` (the non-gating ``candidate:true`` graded checks resolved for it by
``_ticket_state.pool_candidates``) and a per-ticket budget, then pins the returned validations.
Assembly is PURE and DETERMINISTIC:

  * **PROMOTE** the top ``budget`` graded candidates by severity (stable tie-break on ``check_id``)
    to individual GATING graded validations, each keeping its own rubric. Because the ordering is a
    pure function of the candidate set, the promoted (gating) set never changes across passes on the
    same candidates — closing the thrash gap the companion's frozen-rubric/content-hash cache leaves
    open (those freeze each rubric, not WHICH candidates gate).
  * **FOLD** every remaining graded candidate into ONE advisory-aggregate graded validation whose
    rubric unions the folded candidates' axes at a lowered SOFT-FLOOR threshold. The companion
    verdict is already min-of-axes (``rubric.evaluate``), so the aggregate is **min-of-candidates**
    by construction: a single egregious folded axis fails it, a merely-mediocre one does not
    (non-gating for normal shortfalls, gating only on an egregious min — Decision 4).
    ``soft_floor=0.0`` makes the aggregate purely informational (no axis can fail on score).

The output is the ``pin_validations`` list the worker pins (``_ticket_state._norm_validation`` copies
each entry's ``rubric``/``source_check_id`` onto the frozen pinned validation). Binary candidates are
NOT handled here — they are cheap exit-code checks the worker's existing synthesis path emits directly.
"""

from __future__ import annotations

from typing import Any, Sequence

# The advisory aggregate's fixed identity, so the worker (and tests) can find the single aggregate.
AGGREGATE_ID = "graded:advisory-aggregate"
AGGREGATE_SOURCE = "advisory-aggregate"

# Default egregious floor: a folded axis scoring below this fails the aggregate; at/above passes.
AGGREGATE_SOFT_FLOOR = 0.2
# The aggregate is advisory: only a maximally-confident located defect should fail it on a defect
# (the score soft-floor is the primary gating lever), so its confidence floor sits at the ceiling.
_AGGREGATE_CONFIDENCE_FLOOR = 10

# Severity may arrive as a number (higher == more severe) or a known word; unknown -> lowest.
_SEVERITY_WORDS = {
    "p0": 4, "critical": 4, "blocker": 4,
    "p1": 3, "high": 3,
    "p2": 2, "medium": 2, "med": 2,
    "p3": 1, "low": 1, "minor": 1,
}


def _meta(candidate: Any) -> dict:
    return (candidate.get("meta") or {}) if isinstance(candidate, dict) else {}


def _cid(candidate: Any) -> str:
    meta = _meta(candidate)
    return str(meta.get("check_id") or candidate.get("id") or "").strip()


def _rubric_dict(candidate: Any) -> dict | None:
    rub = _meta(candidate).get("rubric")
    if isinstance(rub, dict) and rub.get("axes"):
        return rub
    return None


def _severity_rank(candidate: Any) -> float:
    sev = _meta(candidate).get("severity")
    if isinstance(sev, bool):  # guard: bool is an int subclass
        return 0.0
    if isinstance(sev, (int, float)):
        return float(sev)
    word = str(sev or "").strip().casefold()
    if word in _SEVERITY_WORDS:
        return float(_SEVERITY_WORDS[word])
    try:
        return float(word)
    except ValueError:
        return 0.0


def _graded_validation(candidate: Any, covers: list[str]) -> dict:
    cid = _cid(candidate)
    return {
        "validation_id": f"graded:{cid}",
        "covers": list(covers),
        "run": "",                       # graded checks carry no exit-code command
        "kind": "graded",
        "rubric": _rubric_dict(candidate),   # frozen provenance: the candidate's own rubric
        "source_check_id": cid,
    }


def _aggregate_validation(folded: Sequence[Any], covers: list[str], soft_floor: float) -> dict | None:
    """Compose ONE aggregate graded validation whose axes union the folded candidates' axes at the
    soft floor. Axis names are namespaced by ``check_id`` so two candidates that reused an axis name
    (e.g. both ``correctness``) do not collide. Returns None if nothing folded resolves to any axis.
    """
    axes: list[dict] = []
    for c in folded:
        cid = _cid(c)
        rub = _rubric_dict(c)
        if not rub:
            continue
        for a in (rub.get("axes") or []):
            name = str(a.get("name") or "").strip()
            if not name:
                continue
            axes.append({
                "name": f"{cid}:{name}",
                "threshold": float(soft_floor),
                "guidance": str(a.get("guidance") or ""),
            })
    if not axes:
        return None
    rubric = {
        "axes": axes,
        "confidence_floor": _AGGREGATE_CONFIDENCE_FLOOR,
        "criterion": "advisory aggregate over un-promoted candidates "
                     "(min-of-candidates; soft-floored, non-gating for normal shortfalls)",
        "judge_prompt": "Score each namespaced axis against the un-promoted candidate it came from. "
                        "This aggregate only fails on an egregiously low axis (below the soft floor) "
                        "or a maximally-confident located defect.",
    }
    return {
        "validation_id": AGGREGATE_ID,
        "covers": list(covers),
        "run": "",
        "kind": "graded",
        "rubric": rubric,
        "source_check_id": AGGREGATE_SOURCE,
    }


def assemble(candidates: Sequence[Any], budget: int,
             covers: Sequence[str] = (), soft_floor: float | None = None) -> list[dict]:
    """Tier ``candidates`` (a ticket's graded ``pool_candidates``) into promoted gating validations
    + one advisory aggregate. Pure and deterministic. ``covers`` is the requirement id(s) the pinned
    validations attach to. ``soft_floor`` overrides :data:`AGGREGATE_SOFT_FLOOR` (0.0 = informational).
    """
    floor = AGGREGATE_SOFT_FLOOR if soft_floor is None else float(soft_floor)
    cov = [str(c) for c in covers]
    budget = max(0, int(budget))

    graded = [c for c in candidates if _rubric_dict(c) is not None]
    # Deterministic tiering: severity DESC, then check_id ASC as a stable, content-only tie-break.
    graded.sort(key=lambda c: (-_severity_rank(c), _cid(c)))

    promoted = graded[:budget]
    folded = graded[budget:]

    out = [_graded_validation(c, cov) for c in promoted]
    agg = _aggregate_validation(folded, cov, floor)
    if agg is not None:
        out.append(agg)
    return out
