"""FL14 (R14, D6, D8) — automatic, evidence-gated widening + universal promotion.

R14: recurrence of the same failure class in a NEW scope (surface/tag/project) widens a check's
binding into that scope only after a FRESH, CLASS-SPECIFIC proof there — never on generic breakage.
D6 settles the proof mechanism: the widening proof must FAIL on the new scope's own pinned bad
artifact AND PASS on that scope's healthy reference — the sibling project's integration HEAD,
resolved through the box worktree registry (:func:`resolve_sibling_worktree`). Reusing
:func:`agent_factory.ingestion_api.run_fail_then_pass_proof` for that proof is what gives the
inversion guard (E7's "generic breakage never widens" requirement) for free: a check whose failure
is NOT class-specific fails on both the bad artifact and the healthy sibling ("fails-both") or
passes on both ("vacuous-pass"), never "proven" — the only verdict this module ever acts on.

R20b: this is exactly the taxonomy-DEPENDENT automation the staged-rollout calibration gate
(:mod:`agent_factory.failure_taxonomy`) exists for — :func:`attempt_widen` is a no-op, observe-only
call until calibration is armed.

E8/R24: when the sibling worktree is not resolvable (the box worktree registry has no entry, or the
path no longer exists), the widen PARKS rather than failing or guessing: it emits a ``"parking"``
flag (visible, push-not-pull per FL18) and returns ``retry=True`` — the caller is expected to call
:func:`attempt_widen` again on the NEXT recurrence rather than treating this as a terminal verdict.

This module never calls ``hooks._praxis`` directly — it only orchestrates
:mod:`agent_factory.ingestion_api` (the sole writer) and :mod:`agent_factory.failure_taxonomy` (the
calibration gate), exactly like ``failure_taxonomy`` does for its own logic.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_factory import failure_taxonomy, ingestion_api

# The box worktree registry (D6): a JSON object ``{"<project>": "<worktree path>"}`` naming, for
# every project this box knows about, the checkout whose HEAD is that project's current integration
# state. Sourced from an env var rather than a hardcoded path so a per-box registry never needs a
# code change; absent/malformed is "no registry" (every lookup then defers/parks), never a crash.
BOX_WORKTREE_REGISTRY_ENV = "BOX_WORKTREE_REGISTRY"


def _load_registry() -> dict[str, str]:
    raw = os.environ.get(BOX_WORKTREE_REGISTRY_ENV)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def resolve_sibling_worktree(project: str, *, registry: dict[str, str] | None = None) -> Path | None:
    """The box worktree registry lookup (D6): the checkout path holding ``project``'s current
    integration HEAD, or ``None`` when the box has no registry entry for it or the path no longer
    exists — either case is "sibling unavailable" to the caller, never an exception (a registry miss
    is an ordinary, expected outcome the widen-parking path exists to handle)."""
    reg = registry if registry is not None else _load_registry()
    path = reg.get(str(project))
    if not path:
        return None
    p = Path(path)
    return p if p.is_dir() else None


def attempt_widen(check_id: str, project: str, new_scope: str, *, class_id: str,
                  bad_artifact_meta: dict[str, Any], run: str, sibling_project: str | None = None,
                  repeat_count: int = ingestion_api.DEFAULT_REPEAT_COUNT,
                  registry: dict[str, str] | None = None,
                  executor: Callable[[str, Path], bool] | None = None,
                  identity: str | None = None) -> dict[str, Any]:
    """R14 — the automatic widening decision for ONE recurrence.

    Gated by R20b's calibration: while unarmed this is observe-only (``status="observe-only"``, no
    proof attempted, no write). Once armed:

    1. Resolve the healthy reference via the box worktree registry (:func:`resolve_sibling_worktree`
       against ``sibling_project`` — defaults to ``project`` itself, i.e. the new scope's own healthy
       sibling). Unavailable -> PARKS visibly (``status="parked"``, a ``"parking"`` flag emitted,
       ``retry=True`` — call again on the next recurrence, never a silent drop, E8).
    2. Run the class-specific fail-then-pass proof against the new scope's pinned bad artifact and
       that healthy reference. Only a ``"proven"`` verdict widens (:func:`ingestion_api.widen`) —
       every other verdict (generic breakage's "fails-both", a vacuous pass, a flaky/irreproducible
       proof) leaves the check's scope untouched (``status="not-widened"``), which is exactly R14's
       inversion guard: a check whose proof is satisfiable by generic breakage never widens on an
       unrelated failure.
    """
    if not failure_taxonomy.guard_automation("widen"):
        return {"status": "observe-only", "reason": "calibration-not-armed"}

    sibling_path = resolve_sibling_worktree(sibling_project or project, registry=registry)
    if sibling_path is None:
        ingestion_api.emit_flag(
            ingestion_api.FLAG_KIND_PARKING, project,
            {"check_id": check_id, "class_id": class_id, "new_scope": new_scope,
             "reason": "sibling-unavailable", "sibling_project": sibling_project or project},
            identity=identity,
        )
        return {"status": "parked", "reason": "sibling-unavailable", "retry": True}

    proof = ingestion_api.run_fail_then_pass_proof(
        run, bad_artifact_meta=bad_artifact_meta, healthy_repo_path=sibling_path,
        repeat_count=repeat_count, executor=executor,
    )
    if proof["status"] != "proven":
        return {"status": "not-widened", "reason": proof.get("reason"), "proof": proof}

    widened = ingestion_api.widen(
        check_id, project, [new_scope],
        reason=f"class {class_id} recurrence: fresh class-specific proof in scope {new_scope!r}",
        identity=identity,
    )
    return {"status": "widened", "check": widened, "proof": proof}
