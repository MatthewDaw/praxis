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

import os
import re
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
