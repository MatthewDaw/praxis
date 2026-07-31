#!/usr/bin/env python3
"""
plan-completeness gate — the factory's SECOND *Stop* hook (idea 2, the planning-session sibling of
``build_completeness_gate.py``).

It keeps a PLANNING session from ending until the plan mechanically BLESSES: while an intake session
is armed, the hook BLOCKS the Stop until the plan passes every mechanical predicate, then AUTO-BLESSES
(ALLOWS) — summoning the human only on a failing predicate. A bounded terminal escalation guarantees
an unresolvable predicate never re-blocks forever in an autonomous run.

ARMING (stay inert for ordinary repo conversation, and for BUILD sessions)
--------------------------------------------------------------------------
A planning session is active IFF a non-stale planning marker is present for the project
(``_ticket_state.planning_active`` — stamped by af-intake-plan at intake start, cleared at bless).
No marker => inert: the hook ALLOWS the stop and is byte-identical to no hook. A build session (which
stamps a RUN marker, not a planning marker) is never armed here — the two gates do not cross-fire.

ENFORCE (all must hold to auto-bless)
-------------------------------------
  1. ``plan_gate_check`` exits 0 — every mechanical plan rule passes, INCLUDING R-CONTRACT-SIGNED
     (a signed contract with recorded evaluator actions, threaded in by plan_gate_check).
  2. contradiction detection RAN for the snapshot (the ``contradictions_checked`` marker is set — KTD4:
     "empty" is not evidence of consistency when the raw-bulk path skips detection) AND the live
     contradiction queue is empty.
  3. planning-validation lens coverage is complete (no uncovered planning lens).

BLOCK names the SPECIFIC failing predicate — the human-escalation moment.

BOUNDED TERMINAL ESCALATION (KTD5 — no infinite loop)
-----------------------------------------------------
After K failed bless attempts on an UNCHANGED plan snapshot, the hook emits a terminal ``plan_blocked``
"human required" state and ALLOWS the stop — so an unresolvable contradiction / R-NO-VAGUE term never
re-blocks forever in a headless run. A changed snapshot resets the counter (real progress is never
penalized). K defaults to 3 (``FACTORY_PLAN_GATE_MAX_ATTEMPTS``).

FAIL-CLOSED + SCOPED ESCAPE
---------------------------
Praxis is a HARD dependency: on ``PraxisUnreachable`` the gate BLOCKS loudly (never fails open). The
emergency escape is its OWN scoped env var ``FACTORY_PLAN_GATE_DISABLED=1`` — distinct from
``FACTORY_GATE_DISABLED`` (which stands down the BUILD gate), so standing down planning never disables
build enforcement, and vice versa.
"""

import hashlib
import json
import os
import sys

# The helper modules (_praxis, _ticket_state) live next to this file; the plan-gate tool lives in
# ../tools. A bare hook subprocess may launch with an arbitrary cwd, so make both importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(os.path.dirname(_HERE), "tools")
for _p in (_HERE, _TOOLS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _gate_common import active_project as _active_project  # noqa: E402
from _gate_common import allow as _allow  # noqa: E402
from _gate_common import block as _block  # noqa: E402
from _gate_common import classify_unreachable, session_touched  # noqa: E402

# The marker meta key (on the planning marker fact) that records contradiction detection RAN for the
# snapshot — positive evidence, honestly stamped false by the raw-bulk path (KTD4 / Assumptions).
M_CONTRADICTIONS_CHECKED = "contradictions_checked"

_DEFAULT_MAX_ATTEMPTS = 3

# Substrings that mean THIS session engaged PLANNING. Broad on purpose: a false positive only costs a
# fall-through to the fail-closed Praxis read (never a fail-OPEN), while a miss must never let an armed
# planning session stand down — so we err toward "looks like planning".
_PLANNING_SIGNALS = (
    "af-intake", "plan_completeness", "planning_active", "stamp_planning", "planning-validation",
    "contract-signed", "plan_gate", "prd-", "praxis_", "mcp__praxis",
)


# --------------------------------------------------------------------------- attempt tracking (KTD5 / S8)

def _read_attempts(snapshot_hash: str) -> int:
    """The number of consecutive failed bless attempts recorded for THIS snapshot hash (0 if the
    stored hash differs — a changed plan resets the counter). Reads the DURABLE Praxis-stored
    escalation state (S8) instead of the old gate-local file, so a separate process sees the
    same counter."""
    import _ticket_state as ts
    try:
        attempts, stored_hash, _ = ts.read_escalation_state(project=_active_project(os.getcwd()))
    except ts.PlanEscalationError:
        # A corrupt counter is NOT silently zero — it surfaces via the named error so the gate
        # can fail LOUD. Return a value that will force escalation (treat as max+1).
        import sys as _sys
        _sys.stderr.write(
            "[plan-completeness gate] WARNING: escalation counter is corrupt — treating as "
            "terminal to force a human review. Clear via clear_plan_blocked().\n"
        )
        return _max_attempts()  # force terminal escalation on corrupt state
    except Exception:
        # PraxisUnreachable or any other problem — return 0 (not a hard failure here; the
        # caller's fail-closed guard handles the PraxisUnreachable separately).
        return 0
    if stored_hash != snapshot_hash:
        return 0
    return attempts


def _bump_attempts(snapshot_hash: str) -> int:
    """Record one more failed attempt for this snapshot hash (durable, Praxis-stored — S8).
    Returns the new count, or 0 if Praxis is unreachable (best-effort; the fail-closed guard
    in main() handles unreachability separately)."""
    import _ticket_state as ts
    try:
        return ts.bump_escalation_attempts(project=_active_project(os.getcwd()),
                                           snapshot_hash=snapshot_hash)
    except Exception:
        return 0


def _reset_attempts() -> None:
    """Clear the failed-attempt counter AND the terminal escalation block from Praxis (S8).
    Best-effort: a missing/unreachable Praxis is a no-op here. The gate's fail-closed guard
    handles unreachability before this is ever called."""
    import _ticket_state as ts
    ts.reset_escalation_attempts(project=_active_project(os.getcwd()))


def _max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("FACTORY_PLAN_GATE_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ATTEMPTS


# --------------------------------------------------------------------------- predicates

# The plan-content fields that constitute the identity of a requirement for the purpose of
# snapshot-fingerprint comparison. These are the fields af-intake-plan authors; lifecycle
# meta keys (build_state, pinned_checks, claimed_by, etc.) added during the build process
# are EXCLUDED so a claim / heartbeat / release cycle does not change the fingerprint and
# re-trigger the reset defect.
_PLAN_CONTENT_ALLOWLIST = (
    "acceptance",
    "defines",
    "depends_on",
    "references",
    "tags",
    "verify",
    # requirement_id is included for stable ordering; it is also plan content (authored by intake).
    "requirement_id",
)


def _snapshot_hash(project: str) -> str:
    """A stable fingerprint of the plan snapshot (its requirement facts).

    Hashes ONLY the plan-content fields (the explicit allow-list) — not the full meta blob — so
    lifecycle meta keys added during the build (build_state, pinned_checks, claimed_by,
    heartbeat_at, validation_results, etc.) cannot silently re-enter the fingerprint and re-open
    the reset defect. Two attempts on the SAME plan share a hash; an edit to a requirement's
    text or acceptance changes it; adding a lifecycle key does not.
    """
    import _praxis
    import _ticket_state as ts

    space, snap = ts.project_ref(project).plan
    facts = _praxis.facts_by(category="requirement", space=space, snapshot=snap)
    rows = sorted(
        _fingerprint_row(f) for f in (facts or [])
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _fingerprint_row(fact: dict) -> tuple:
    """Reduce a single requirement fact to its plan-content fingerprint tuple.

    Only the allow-listed plan-content fields contribute — lifecycle meta keys are stripped so
    they cannot re-enter the fingerprint. The field set matches what ``plan_gate_check.py``'s
    ``requirement_from_fact`` threads into the pure gate (Requirement: id, text, acceptance,
    defines, references, depends_on, tags, verify).
    """
    meta = fact.get("meta") or {}
    return (
        str(fact.get("id") or fact.get("factId") or ""),
        str(meta.get("requirement_id") or ""),
        str(fact.get("text") or fact.get("content") or ""),
        *(str(meta.get(k) or "") if not isinstance(meta.get(k), (list, dict))
          else json.dumps(meta.get(k) or ([], {}), sort_keys=True, default=str)
          for k in _PLAN_CONTENT_ALLOWLIST),
    )


def _plan_blesses(project: str) -> tuple[bool, str]:
    """(ok, reason). Runs the mechanical plan gate (plan_gate_check, incl. R-CONTRACT-SIGNED)."""
    import plan_gate_check as pgc

    try:
        verdict, _ = pgc.check_plan(project)
    except ValueError as exc:  # no requirement facts yet — the plan is not ready to bless
        return False, f"the plan is not ready: {exc}"
    if verdict.admitted:
        return True, ""
    lines = "\n".join(f"    [{r.rule_id}] {r.message}" for r in verdict.reasons)
    return False, f"plan_gate REJECTED ({len(verdict.reasons)} reason(s)):\n{lines}"


def _contradictions_clean(project: str) -> tuple[bool, str]:
    """(ok, reason). Requires POSITIVE evidence detection ran (the ``contradictions_checked`` marker)
    AND an empty live contradiction queue for the plan snapshot (KTD4)."""
    import _praxis
    import _ticket_state as ts

    space, snap = ts.project_ref(project).plan
    mid = ts.planning_marker_id(project)
    marker = _praxis.get_fact(mid, not_found_ok=True,
                              space=space, snapshot=snap) if mid else None
    if not bool(((marker or {}).get("meta") or {}).get(M_CONTRADICTIONS_CHECKED)):
        return False, (
            "contradiction detection has NOT run for this snapshot — an empty queue is not evidence "
            "of consistency (the raw-bulk path skips detection). Run detection so the "
            f"'{M_CONTRADICTIONS_CHECKED}' marker is set, then re-check.")
    contradictions = _praxis.get_contradictions(space=space, snapshot=snap)
    if contradictions:
        return False, (
            f"{len(contradictions)} unresolved contradiction(s) flagged in the plan snapshot — "
            "resolve each (praxis_resolve_contradiction) before the plan can bless.")
    return True, ""


def _lens_coverage_complete(project: str) -> tuple[bool, str]:
    """(ok, reason). Every planning-validation lens must be covered. A resolved lens whose meta lacks
    a truthy ``covered``/``satisfied`` flag is a GAP. An EMPTY resolve (zero lenses) is its own
    explicit zero-resolved signal, distinct from a satisfied coverage contract — an empty
    planning-validation snapshot means lens coverage was never established, so it BLOCKS rather than
    being treated as trivially complete (KTD/B1c: "zero lenses is trivially complete" was the bug)."""
    import _ticket_state as ts

    lenses = ts.resolve_validation_requirements(
        {"id": ts.planning_marker_id(project) or f"prd-{ts.planning_project(project)}::planning",
         "meta": {}}, project=project, scope="planning")
    if not lenses:
        return False, (
            "planning-validation lens coverage is INCOMPLETE — ZERO lenses resolved from the "
            "planning-validation snapshot (an empty snapshot is a zero-resolved signal, not a "
            "satisfied coverage contract). Author at least one planning-validation lens "
            "(af-intake-plan-validation) before the plan can bless.")
    gaps = []
    for lens in (lenses or []):
        meta = lens.get("meta") or {}
        if not (meta.get("covered") or meta.get("satisfied")):
            gaps.append(str(lens.get("id") or lens.get("check_id") or "?"))
    if gaps:
        return False, ("planning-validation lens coverage is INCOMPLETE — uncovered lens(es): "
                       + ", ".join(gaps))
    return True, ""


# The ordered ENFORCE predicates. Evaluated in order; the FIRST failure is the escalation reason.
_PREDICATES = (_plan_blesses, _contradictions_clean, _lens_coverage_complete)


# --------------------------------------------------------------------------- session identity

def _session_owner(data: dict) -> str:
    """This session's owner identity (matches the session id that stamped the planning marker).

    ``FACTORY_TICKET_OWNER``, when set, OVERRIDES the CLI-assigned ``session_id`` (mirrors the
    ``FACTORY_PROJECT`` override pattern in ``_gate_common.active_project``, and the build gate's
    own ``_session_owner``). Unset, falls back to the session id from the Stop-hook data.
    """
    override = str(os.environ.get("FACTORY_TICKET_OWNER") or "").strip()
    if override:
        return override
    return str(data.get("session_id") or data.get("sessionId") or "").strip()


# --------------------------------------------------------------------------- main

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        data = {}
    cwd = data.get("cwd") or os.getcwd()

    # --- Scoped escape hatch (documented + LOUD; distinct from the build gate's). ----------------
    auth_disabled = os.environ.get("PRAXIS_AUTH_DISABLED") == "1"
    plan_gate_disabled = os.environ.get("FACTORY_PLAN_GATE_DISABLED") == "1"

    if plan_gate_disabled or auth_disabled:
        # Record the disable variable on the project's build marker so a run that executed with
        # a disabled gate cannot be presented as fully gated. Best-effort: still stand down if
        # stamping fails.
        try:
            import _ticket_state as ts
            _proj = _active_project(cwd)
            if _proj:
                if plan_gate_disabled:
                    ts.stamp_gate_disable(_proj, "FACTORY_PLAN_GATE_DISABLED", "1")
                if auth_disabled:
                    ts.stamp_gate_disable(_proj, "PRAXIS_AUTH_DISABLED", "1")
        except Exception:  # noqa: BLE001 - marker write is best-effort; never block the stand-down
            pass

        parts: list[str] = []
        if plan_gate_disabled:
            parts.append("FACTORY_PLAN_GATE_DISABLED=1: planning gate is NOT verifying the plan "
                         "(build enforcement is UNAFFECTED — that is a separate gate/escape)")
        if auth_disabled:
            parts.append("PRAXIS_AUTH_DISABLED=1: Praxis auth is bypassed — gate enforcement cannot "
                         "verify identity")
        _allow("plan-completeness gate STOOD DOWN: " + " | ".join(parts)
               + ". Unset the named variable(s) to restore enforcement.")

    # Load the factory .env before resolving the project (a bare Stop-hook subprocess does not inherit
    # a shell-sourced .env). Best-effort + fail-closed-preserving, exactly like the build gate.
    try:
        import _praxis
        _praxis._load_dotenv()
    except Exception:  # noqa: BLE001 — a broken/absent _praxis re-raises in the fail-closed block below
        pass

    project = _active_project(cwd)
    owner = _session_owner(data)

    # --- NO-OP FAST-PATH: a session that never touched planning has nothing to bless. -------------
    if session_touched(data.get("transcript_path"), _PLANNING_SIGNALS) is False:
        _allow()

    # --- ARM + ENFORCE (fail-closed). All Praxis reads live under one guard so PraxisUnreachable ---
    # BLOCKS loudly rather than failing open.
    try:
        import _ticket_state as ts

        if not ts.planning_active(project, owner=owner):
            _allow()  # inert — no planning session is armed for this project (or this session)

        snapshot_hash = _snapshot_hash(project)
        reason = ""
        for predicate in _PREDICATES:
            ok, why = predicate(project)
            if not ok:
                reason = why
                break
    except Exception as exc:  # noqa: BLE001
        _, detail = classify_unreachable(exc)
        diag = ""
        try:
            import _praxis
            diag = _praxis.preflight(live=True).message()
        except Exception:  # noqa: BLE001
            diag = ""
        _block(
            "plan-completeness gate: PRAXIS UNREACHABLE — the factory cannot verify the plan, so this "
            "gate is failing CLOSED and BLOCKING. Praxis is the single source of dynamic truth.\n"
            f"  reason: {detail}\n"
            + (f"\nPREFLIGHT:\n{diag}\n" if diag else "")
            + "\nBring Praxis up and/or fix the item(s) above, then try again. For a real emergency "
            "ONLY, set FACTORY_PLAN_GATE_DISABLED=1 to stand THIS gate down (build enforcement is "
            "unaffected; loud, never silent)."
        )
        return

    # --- AUTO-BLESS: every predicate held. ALLOW and reset the escalation counter. ----------------
    if not reason:
        _reset_attempts()
        _allow("plan-completeness gate: the plan BLESSES — every mechanical predicate holds "
               "(plan_gate incl. signed contract, contradictions checked + empty, lens coverage "
               "complete). Auto-blessed with no human required. Clear the planning marker "
               "(clear_planning) as intake finishes.")

    # --- A predicate failed. Bounded terminal escalation (KTD5 / S8): after K unchanged-snapshot ---
    # failures, stamp a DUrable terminal escalation record in Praxis (S8) and ALLOW so an
    # unresolvable predicate never loops forever. The downstream build gate reads this record and
    # refuses the build phase while it exists. An operator can clear it via clear_plan_blocked().
    attempts = _bump_attempts(snapshot_hash)
    limit = _max_attempts()
    if attempts >= limit:
        try:
            import _ticket_state as ts  # noqa: F811
            ts.stamp_plan_blocked(project)
        except Exception:  # noqa: BLE001 — a Praxis failure during stamp is best-effort; still allow
            pass
        _allow(
            f"plan-completeness gate: TERMINAL plan_blocked — the plan failed to bless {attempts} "
            f"time(s) on an UNCHANGED snapshot (cap {limit}). A durable escalation record has been "
            "written to Praxis (S8); the downstream build gate will refuse the build phase while it "
            "exists. Standing down to avoid an infinite re-block; a HUMAN is required to resolve the "
            "outstanding predicate:\n"
            f"{reason}\n"
            "Edit the plan (which resets this counter) or accept/override the outstanding item, then "
            "re-run intake. If the plan will never bless, clear the escalation explicitly with "
            "clear_plan_blocked().")

    _block(
        f"plan-completeness gate: the plan does NOT bless yet (attempt {attempts}/{limit} on this "
        f"snapshot). Do not end the planning turn until it does:\n{reason}\n\n"
        "Fix the named predicate and re-check. When every predicate holds the gate auto-blesses with "
        "no human. (After the cap on an unchanged snapshot the gate escalates to a human instead of "
        "re-blocking. Emergency-only stand-down: FACTORY_PLAN_GATE_DISABLED=1 — planning only.)"
    )


def _shout_crash(exc: BaseException) -> None:
    """LOUD, never silent: a crash here means the plan was NOT verified for this Stop. The block
    reason (stdout JSON) is swallowed by the headless ``claude -p`` retry loop, so stderr is the
    only channel a human/log sees. Also records the stand-down on the project's build marker
    (best-effort) so a run that executed with a crashed gate is not later presented as gated."""
    import traceback
    sys.stderr.write(
        "[plan-completeness gate] GATE CRASHED — NOT ENFORCED for this Stop. The gate's own logic "
        f"raised {type(exc).__name__}: {exc}. The plan was NOT verified; it may not bless. This is "
        "a bug in the gate, not a Praxis outage — fix it.\n"
    )
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    try:
        import _ticket_state as ts
        proj = _active_project(os.getcwd())
        if proj:
            ts.stamp_gate_disable(proj, "PLAN_GATE_CRASHED", type(exc).__name__)
    except Exception:  # noqa: BLE001 — durable record is best-effort; the stderr shout is the floor
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A crash in the gate's OWN logic (after the fail-closed Praxis guard, which BLOCKS on its
        # own) must not wedge the agent forever — exit cleanly (allow), but LOUDLY. A gate that
        # disappears without a word is the failure mode this whole guard class exists to prevent.
        _shout_crash(exc)
        sys.exit(0)
