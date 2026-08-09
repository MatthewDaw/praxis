"""Failure-class taxonomy: ingestion-time dedup + staged calibration rollout (FL3 / R3, R20b, KD4).

R3 (dedup): an incoming failure matching an existing lesson's class attaches evidence to that
class and counts as a recurrence instead of duplicating the lesson; a genuinely novel failure
mints a new class.

R20b / KD4 (staged rollout): taxonomy-dependent automation (R14 widening, R20/FL15 resurrection)
stays observe-only — class assignments are recorded and surfaced (af-retro) but drive NO automatic
action — until the calibration EXIT CONDITION is met: a configured number of class assignments
recorded back-to-back with no operator correction. Crossing that threshold is a ONE-WAY, observable
state flip (``armed`` False -> True on the shared calibration fact) that later automation reads via
:func:`guard_automation` before acting; a later correction does not disarm it — a correction after
graduation is ordinary reclassification, not a rollback of trust already earned.

All writes into the shared learnings space route through :mod:`agent_factory.ingestion_api` — the
sole writer (FL1). This module contains only matching/calibration LOGIC and never calls a
``_praxis`` write primitive itself.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any

from agent_factory import ingestion_api

DEFAULT_MATCH_THRESHOLD = 0.55
DEFAULT_CALIBRATION_EXIT_COUNT = 20
_CALIBRATION_ENV = "FAILURE_TAXONOMY_CALIBRATION_COUNT"
_CALIBRATION_DEFAULTS = {
    "streak": 0, "total_assignments": 0, "corrections": 0, "armed": False, "armed_at": None,
}


def calibration_exit_count() -> int:
    """The configured uncorrected-assignment streak needed to arm automation. Overridable via
    ``FAILURE_TAXONOMY_CALIBRATION_COUNT``; falls back to the default on absent/invalid values."""
    raw = os.environ.get(_CALIBRATION_ENV)
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return DEFAULT_CALIBRATION_EXIT_COUNT


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _similarity(a: str, b: str) -> float:
    """Cheap deterministic token-Jaccard match — no embedding round trip needed for MVP dedup."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _class_label(cls: dict[str, Any]) -> str:
    return str(cls.get("text") or cls.get("content") or (cls.get("meta") or {}).get("label") or "")


def find_matching_class(text: str, classes: list[dict[str, Any]] | None = None,
                        *, threshold: float = DEFAULT_MATCH_THRESHOLD) -> dict[str, Any] | None:
    """The existing class ``text`` dedups against (best token-overlap match at/above
    ``threshold``), or ``None`` when nothing qualifies — signalling a genuinely novel failure."""
    pool = ingestion_api.read_classes() if classes is None else classes
    best: dict[str, Any] | None = None
    best_score = 0.0
    for cls in pool:
        score = _similarity(text, _class_label(cls))
        if score > best_score:
            best, best_score = cls, score
    return best if best is not None and best_score >= threshold else None


def assign_class(text: str, *, evidence: str | None = None, source: str | None = None,
                 corrected: bool = False, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """The R3 ingestion-time dedup entrypoint.

    A match attaches ``evidence`` to that class and increments its recurrence count — the lesson
    is NEVER duplicated. No match mints a brand-new class fact plus its lesson. Either way the
    assignment is recorded against the calibration streak (R20b); pass ``corrected=True`` when this
    call is an operator CORRECTING a prior misclassification (resets the streak, never extends it).

    Returns ``{"class_id", "action": "matched"|"minted", "recurrence_count", "calibration": {...}}``.
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("text is required")

    match = find_matching_class(body)
    if match is not None:
        class_id = match["id"]
        class_meta = dict(match.get("meta") or {})
        recurrence_count = int(class_meta.get("recurrence_count") or 1) + 1
        evidence_log = list(class_meta.get("evidence") or [])
        if evidence:
            evidence_log.append({"text": str(evidence), "source": source, "recorded_at": time.time()})
        class_meta["recurrence_count"] = recurrence_count
        class_meta["evidence"] = evidence_log
        ingestion_api.update_class_meta(class_id, class_meta)
        action = "matched"
    else:
        class_meta = dict(meta or {})
        class_meta["recurrence_count"] = 1
        class_meta["evidence"] = (
            [{"text": str(evidence), "source": source, "recorded_at": time.time()}] if evidence else []
        )
        written = ingestion_api.write_class(body, source=source, meta=class_meta)
        class_id = written["id"]
        recurrence_count = 1
        ingestion_api.write_lesson(body, source=source,
                                   meta={**(meta or {}), "failure_class_id": class_id})
        action = "minted"

    calibration = record_assignment(corrected=corrected)
    return {
        "class_id": class_id,
        "action": action,
        "recurrence_count": recurrence_count,
        "calibration": calibration,
    }


# --------------------------------------------------------------------------- calibration (R20b)

def calibration_state() -> dict[str, Any]:
    """The current staged-rollout state — read-only, safe to call from any project (af-retro)."""
    fact = ingestion_api.read_calibration_state()
    meta = dict((fact or {}).get("meta") or {})
    state = dict(_CALIBRATION_DEFAULTS)
    state.update({k: meta[k] for k in state if k in meta})
    state["required"] = calibration_exit_count()
    return state


def is_armed() -> bool:
    """Whether taxonomy-dependent automation (R14 widening, R20/FL15 resurrection) may act."""
    return bool(calibration_state().get("armed"))


def record_assignment(*, corrected: bool = False) -> dict[str, Any]:
    """Record one class-assignment event against the calibration streak and re-check the exit
    condition. A CORRECTED assignment resets the streak to 0 without arming; an uncorrected one
    extends it. Crossing :func:`calibration_exit_count` is a ONE-WAY flip — see the module
    docstring for why a later correction does not disarm it."""
    state = calibration_state()
    required = state.pop("required")
    state["total_assignments"] = int(state["total_assignments"]) + 1
    if corrected:
        state["corrections"] = int(state["corrections"]) + 1
        state["streak"] = 0
    else:
        state["streak"] = int(state["streak"]) + 1

    if not state["armed"] and state["streak"] >= required:
        state["armed"] = True
        state["armed_at"] = time.time()

    ingestion_api.write_calibration_state(state)
    state["required"] = required
    return state


def guard_automation(action: str) -> bool:
    """The gate every taxonomy-dependent automatic action (R14 widening, R20/FL15 resurrection)
    MUST consult before acting. ``True`` iff armed; while unarmed it always returns ``False`` — the
    caller stays observe-only, recording the would-be ``action`` instead of performing it (R20b)."""
    return is_armed()


# --------------------------------------------------------------------------- R20/FL15: resurrection

def attempt_resurrect(class_id: str, project: str, *, evidence: str | None = None,
                      identity: str | None = None) -> dict[str, Any]:
    """R20/FL15 — the automatic resurrection decision for a recurrence of failure class
    ``class_id``: consult ``project``'s archived/suspended checks for that class BEFORE any caller
    drafts anew, and resurrect the match (carrying its prior proof history forward) rather than
    minting a duplicate. Calibration-gated (R20b) exactly like :func:`agent_factory.widening.attempt_widen`
    — observe-only (never mutates) until armed.

    Returns ``{"resurrected": bool, "check": {...}|None, "class_id": class_id, "reason": str}``:
    ``reason`` is one of ``"no-resurrectable-check"`` (nothing archived/suspended for this class),
    ``"calibration-not-armed"`` (a candidate exists but automation stays observe-only), or
    ``"resurrected"``.
    """
    candidate = ingestion_api.find_resurrectable_check(class_id, project)
    if candidate is None:
        return {"resurrected": False, "check": None, "class_id": class_id,
                "reason": "no-resurrectable-check"}
    if not guard_automation("resurrect"):
        return {"resurrected": False, "check": candidate, "class_id": class_id,
                "reason": "calibration-not-armed"}
    candidate_check_id = (candidate.get("meta") or {}).get("check_id") or candidate["id"]
    resurrected_check = ingestion_api.resurrect_check(candidate_check_id, project, evidence=evidence,
                                                       identity=identity)
    return {"resurrected": True, "check": resurrected_check, "class_id": class_id,
            "reason": "resurrected"}


# --------------------------------------------------------------------------- R20/FL15: near-duplicate sweep

DEFAULT_NEAR_DUP_THRESHOLD = 0.75  # stricter than DEFAULT_MATCH_THRESHOLD — a merge is irreversible


def find_near_duplicate_pairs(classes: list[dict[str, Any]] | None = None, *,
                              threshold: float = DEFAULT_NEAR_DUP_THRESHOLD
                              ) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    """Every pair of failure classes whose label token-overlap is at/above ``threshold`` — the
    candidate set :func:`sweep_near_duplicate_classes` merges. Already-merged classes
    (``meta.merged_into`` set) are excluded, and each class appears as a loser in at most one pair
    per call so a chain of near-dups merges one hop at a time rather than double-counting."""
    pool = [c for c in (ingestion_api.read_classes() if classes is None else classes)
            if not (c.get("meta") or {}).get("merged_into")]
    pairs: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    claimed: set[str] = set()
    for i, a in enumerate(pool):
        if str(a.get("id")) in claimed:
            continue
        for b in pool[i + 1:]:
            bid = str(b.get("id"))
            if bid in claimed:
                continue
            score = _similarity(_class_label(a), _class_label(b))
            if score >= threshold:
                pairs.append((a, b, score))
                claimed.add(bid)
    return pairs


def _merge_survivor_and_loser(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic (survivor, loser) ordering for one near-dup pair: the higher recurrence count
    survives (it has earned more trust); a tie breaks on the lower id, so the choice is stable
    across repeated sweeps rather than depending on iteration order."""
    ac = int((a.get("meta") or {}).get("recurrence_count") or 1)
    bc = int((b.get("meta") or {}).get("recurrence_count") or 1)
    if bc > ac:
        return b, a
    if ac > bc:
        return a, b
    return (a, b) if str(a.get("id")) <= str(b.get("id")) else (b, a)


def sweep_near_duplicate_classes(*, classes: list[dict[str, Any]] | None = None,
                                 threshold: float = DEFAULT_NEAR_DUP_THRESHOLD
                                 ) -> list[dict[str, Any]]:
    """R20/FL15 — the off-critical-path near-duplicate sweep: an af-build loop-end hook calls this
    to propose (and, in the same motion, apply) class merges over the failure-class corpus. Each
    merged pair RETROACTIVELY CREDITS the survivor's recurrence count with the loser's, combines
    their evidence logs, and marks the loser ``merged_into`` the survivor (never deleted — the
    audit trail stays intact). Never gated by calibration (:func:`guard_automation`): this
    housekeeping is what the calibration streak itself watches, so gating it on calibration would
    be circular.

    Returns one merge record per pair — the set :mod:`agent_factory.af_retro` surfaces for operator
    spot-audit."""
    merges: list[dict[str, Any]] = []
    for a, b, score in find_near_duplicate_pairs(classes, threshold=threshold):
        survivor, loser = _merge_survivor_and_loser(a, b)
        survivor_meta = dict(survivor.get("meta") or {})
        loser_meta = dict(loser.get("meta") or {})
        credited = int(survivor_meta.get("recurrence_count") or 1) + int(loser_meta.get("recurrence_count") or 1)
        survivor_meta["recurrence_count"] = credited
        survivor_meta["evidence"] = list(survivor_meta.get("evidence") or []) + list(loser_meta.get("evidence") or [])
        ingestion_api.update_class_meta(survivor["id"], survivor_meta)
        ingestion_api.update_class_meta(loser["id"], {
            "merged_into": survivor["id"], "merged_at": time.time(), "merge_score": score,
        })
        merges.append({"survivor_id": survivor["id"], "loser_id": loser["id"], "score": score,
                       "credited_recurrence": credited})
    return merges


# --------------------------------------------------------------------------- the loop-end seam (D5)

def main(argv: list[str] | None = None) -> int:
    """``python -m agent_factory.failure_taxonomy sweep`` — the RUNTIME entry point for the
    loop-end near-duplicate sweep (R20/FL15), shaped like the one call
    ``scripts/af-ticket-loop.sh`` already makes into this package
    (``python -m agent_factory.af_retro --flags <project>``) so wiring it is one line.

    ``--dry-run`` reports the pairs the sweep WOULD merge and writes nothing. Exit codes: ``0``
    swept (merges are printed, one per line, and ``swept 0`` is a legitimate result), ``2`` the
    sweep could not run (unreachable/unauthenticated backend) — printed to stderr and non-zero so a
    caller that swallows it with ``|| true`` still leaves the failure in the log, never a silent
    no-op that looks like "no near-duplicates"."""
    ap = argparse.ArgumentParser(
        prog="failure-taxonomy",
        description="Failure-class taxonomy maintenance. `sweep` merges near-duplicate failure "
                    "classes (R20/FL15): the survivor is retroactively credited with the loser's "
                    "recurrence count and evidence, the loser is marked merged_into (never "
                    "deleted). Intended to run once at loop end, off the critical path.")
    sub = ap.add_subparsers(dest="command", required=True)
    sweep = sub.add_parser("sweep", help="merge near-duplicate failure classes")
    sweep.add_argument("--threshold", type=float, default=DEFAULT_NEAR_DUP_THRESHOLD,
                       help=f"token-overlap merge threshold (default {DEFAULT_NEAR_DUP_THRESHOLD})")
    sweep.add_argument("--dry-run", action="store_true",
                       help="report the pairs that would merge; write nothing")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        if args.dry_run:
            pairs = find_near_duplicate_pairs(threshold=args.threshold)
            for a, b, score in pairs:
                print(f"failure-taxonomy: would merge {a.get('id')} <- {b.get('id')} "
                      f"(score={score:.2f})")
            print(f"failure-taxonomy: dry-run — {len(pairs)} near-duplicate pair(s)")
            return 0
        merges = sweep_near_duplicate_classes(threshold=args.threshold)
    except Exception as exc:  # noqa: BLE001 - an unreachable backend must be LOUD, not a silent 0
        print(f"failure-taxonomy: sweep FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for merge in merges:
        print(f"failure-taxonomy: merged {merge['loser_id']} -> {merge['survivor_id']} "
              f"(score={merge['score']:.2f}, recurrence={merge['credited_recurrence']})")
    print(f"failure-taxonomy: swept {len(merges)} near-duplicate merge(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    sys.exit(main())
