#!/usr/bin/env python3
"""
build-completeness gate — THE SINGLE factory *Stop* hook.

This is the one and only gate of the factory's collapsed gate spine. The old preflight / wireframe /
plan-audit / review gates are GONE: everything they used to enforce is now either a ticket or a check
in Praxis, and this gate enforces the one question they all reduce to — *"are there incomplete
tickets/checks for the active build scope, and is this session in the middle of building them?"* —
LIVE against Praxis. There is no manifest. There is no ``.factory/*.json`` build/validation state.

SINGLE SOURCE OF DYNAMIC TRUTH = Praxis
---------------------------------------
The gate reads build/validation state live from Praxis via ``hooks/_praxis.py`` and
``hooks/_ticket_state.py`` (see ``docs/factory-state-contract.md`` for the canonical meta keys and
API). It writes NO local state. "A build run is active" is NOT a file flag — it is *"this session
owns a live, unfinished in_progress claim"*, read from Praxis.

ARMING (stay inert for ordinary repo conversation)
--------------------------------------------------
A build run is active for THIS session IFF either signal is present in Praxis:
  * WHOLE-SET RUN MARKER — af-build, at run start, stamps every in-scope incomplete ticket with a
    ``run_owner``/``run_at`` marker (``_ticket_state.stamp_run``). While ANY ticket carries this
    session's non-stale marker, the run is active and the gate enforces the ENTIRE marked (scoped)
    set — this is what closes the between-ticket window where the session momentarily holds no claim.
  * OWNED LIVE CLAIM — a live ``in_progress`` lease owned by this session (the legacy/fallback
    signal; also covers a run that pre-dates the marker plumbing).
If neither is present, no build is active for this session, so the gate ALLOWS the stop and stays
inert — ordinary conversation in a repo that merely *has* a ``prd-<project>`` is never blocked.

ENFORCE
-------
While a run is active the gate BLOCKS until the whole scoped set is finished: any ticket this session
still owns unfinished, OR any scoped claimable incomplete ticket remains. The block message is
actionable — which tickets, what is unmet, and the lifecycle to follow (claim/heartbeat, resolve
REQUIREMENTS by query, SYNTHESIZE validations that cover them, run + record each pass ON THE TICKET
NODE, release as finished). The worker cannot end its turn until the scoped set is done.

BLOCKED tickets (terminal ``build_state="blocked"`` — an uncoverable requirement, a credential only
the owner can supply) are EXCLUDED from the churn set but SURFACED prominently in every message, so a
genuinely unprogressable ticket is "a clear thing that forces a stop" rather than a silent forever-block.

FAIL-CLOSED
-----------
Praxis is a HARD dependency. If it is unreachable / unauthenticated / errors (``PraxisUnreachable``),
the gate BLOCKS loudly — it NEVER fails open. A gate that cannot prove build state must not let work
pass. The ONLY way out when Praxis is down is to bring Praxis up, or to set the documented, LOUD
emergency escape hatch ``FACTORY_GATE_DISABLED=1`` (never silent — it prints why it stood down).
"""

import json
import os
import sys

# The helper modules (_praxis, _ticket_state) live next to this file. A bare hook
# subprocess may be launched with an arbitrary cwd, so make sure our own directory is importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# --------------------------------------------------------------------------- hook I/O

def _allow(advice: str = "") -> None:
    if advice:
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": advice}
        }))
    sys.exit(0)


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


# --------------------------------------------------------------------------- project / identity

def _active_project(cwd: str) -> str:
    """Resolve the active ``prd-<project>`` from the environment or the cwd — NEVER a manifest file.

    Order: ``FACTORY_PROJECT`` env (with or without a ``prd-`` prefix) → the basename of the cwd.
    Returns the full ``prd-<name>`` form the Praxis ``/requirements/incomplete`` endpoint expects.
    """
    raw = os.environ.get("FACTORY_PROJECT", "").strip()
    if not raw:
        raw = os.path.basename(os.path.normpath(cwd or os.getcwd()))
    raw = raw.strip()
    if not raw:
        return ""
    return raw if raw.startswith("prd-") else f"prd-{raw}"


def _session_owner(data: dict) -> str:
    """This session's claim-owner identity (matches the owner the build loop claims tickets with)."""
    return str(data.get("session_id") or data.get("sessionId") or "").strip()


# --------------------------------------------------------------------------- ticket views

def _rid(item: dict) -> str:
    for k in ("id", "factId", "fact_id", "requirement_id", "rid", "cid"):
        v = item.get(k)
        if v:
            return str(v)
    return "?"


def _label(item: dict) -> str:
    for k in ("title", "name", "summary"):
        v = item.get(k)
        if v:
            return str(v)[:80]
    text = item.get("text") or item.get("requirement") or ""
    return (str(text)[:80] or _rid(item))


def _claim_view(item: dict):
    """Return ``(owner, build_state, lease_live)`` for an incomplete-requirement item, tolerating
    either a server-derived ``claim`` view or the raw ``meta`` keys (or both)."""
    import _ticket_state as ts

    claim = item.get("claim") or {}
    meta = item.get("meta") or {}
    merged = dict(meta)
    for k, v in claim.items():
        if v is not None:
            merged[k] = v

    owner = merged.get(ts.M_CLAIM_OWNER) or claim.get("owner")
    build_state = merged.get(ts.M_BUILD_STATE) or "incomplete"
    if "lease_live" in claim:
        live = bool(claim.get("lease_live"))
    else:
        merged[ts.M_BUILD_STATE] = build_state
        live = ts._lease_live(merged)
    return (str(owner) if owner else None), str(build_state), bool(live)


def _ready_to_finish(item: dict) -> bool:
    """True iff the ticket has a pinned check contract that is fully satisfied (≥1, all passed)."""
    import _ticket_state as ts
    try:
        return ts.all_checks_passed(item if item.get("meta") else _rid(item))
    except Exception:  # noqa: BLE001 - never let an enrichment read crash the gate
        return False


# --------------------------------------------------------------------------- main

def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        data = {}
    cwd = data.get("cwd") or os.getcwd()

    # --- Emergency escape hatch (documented + LOUD, never silent). ----------------------------
    if os.environ.get("FACTORY_GATE_DISABLED") == "1":
        _allow("build-completeness gate STOOD DOWN: FACTORY_GATE_DISABLED=1 is set. The factory is "
               "NOT verifying build state right now — incomplete tickets/checks may remain unbuilt. "
               "Unset FACTORY_GATE_DISABLED to restore enforcement.")

    project = _active_project(cwd)
    owner = _session_owner(data)

    # --- Read the single source of dynamic truth (fail-closed). -------------------------------
    # NOTE on fan-out: a supervisor that delegated building to sub-agents owns NO live claim of its
    # own (the builders claim tickets under their own session ids), so the arming rule below leaves
    # it inert automatically — no special subagent-deferral plumbing is needed or kept.
    try:
        import _praxis
        incomplete = _praxis.incomplete_requirements(project)
    except Exception as exc:  # noqa: BLE001
        # FAIL-CLOSED: a gate that cannot reach Praxis can prove nothing, so it BLOCKS. It NEVER
        # fails open. (PraxisUnreachable is the contract signal; any import/transport failure is
        # treated identically — the truth is unavailable.)
        try:
            from _praxis import PraxisUnreachable  # noqa: F811
            is_unreachable = isinstance(exc, PraxisUnreachable)
        except Exception:  # noqa: BLE001
            is_unreachable = True
        detail = str(exc) if is_unreachable else f"{type(exc).__name__}: {exc}"
        _block(
            "build-completeness gate: PRAXIS UNREACHABLE — the factory cannot verify build state, so "
            "this gate is failing CLOSED and BLOCKING. Praxis is the single source of dynamic truth; "
            "without it there is no way to know whether tickets/checks are still incomplete.\n"
            f"  reason: {detail}\n"
            "Bring Praxis up (default http://localhost:8000; check PRAXIS_API_BASE_URL / "
            "PRAXIS_API_KEY / PRAXIS_ORG / auth) and try again. For a real emergency ONLY, set "
            "FACTORY_GATE_DISABLED=1 to stand the gate down (loud, never silent)."
        )

    if not isinstance(incomplete, list):
        incomplete = []

    import _ticket_state as ts

    # --- Partition the incomplete set by claim ownership, run-marker scope, and blocked state. -
    owned_unfinished: list[dict] = []   # this session owns a LIVE in_progress lease on these
    claimable: list[dict] = []          # free / stale / ours, IN SCOPE — work this session may drive
    blocked: list[dict] = []            # terminal build_state="blocked" — surfaced, never churned
    run_marked = False                  # does ANY ticket carry this session's non-stale run marker?
    # (tickets a DIFFERENT owner holds a live lease on, or another run's marker, are left to them)

    for item in incomplete:
        if not isinstance(item, dict):
            continue
        c_owner, build_state, live = _claim_view(item)
        meta = item.get("meta") or {}
        # Is this ticket part of THIS session's active whole-set run (non-stale marker)? Detect this
        # BEFORE the finished/blocked skips so a still-marked ticket keeps the run armed for surfacing.
        in_run = bool(owner) and meta.get(ts.M_RUN_OWNER) == owner and ts.run_live(meta)
        if in_run:
            run_marked = True

        # DONENESS IS THE EVAL, NOT THE COUNT. A ticket is done iff its resolved validations (the eval)
        # all passed AND cover the contract, recorded as the hard enum build_state="finished". Honor
        # that enum here even if the count-derived incomplete list still lists it.
        if str(build_state) == "finished":
            continue
        # BLOCKED is terminal-pending-owner: surface it, but never count it as churnable work.
        if str(build_state) == "blocked":
            blocked.append(item)
            continue

        if live and c_owner == owner and owner:
            owned_unfinished.append(item)
        elif live and c_owner and c_owner != owner:
            continue  # actively leased by someone else
        else:
            # Claimable. When a run marker exists, the run defines SCOPE: only marked tickets count
            # as in-scope churn. Without a marker (legacy/fallback run), every claimable ticket counts.
            claimable.append({"_item": item, "_in_run": in_run})

    def _fmt(items: list[dict], limit: int = 40) -> str:
        lines = []
        for it in items[:limit]:
            if str((it.get("meta") or {}).get(ts.M_BUILD_STATE)) == "blocked":
                reason = str((it.get("meta") or {}).get(ts.M_BLOCK_REASON) or "").strip()
                tail = f" — BLOCKED: {reason}" if reason else " — BLOCKED"
            elif _ready_to_finish(it):
                tail = " — validations PASSED + cover the contract, release as finished"
            else:
                tail = ""
            lines.append(f"  - {_rid(it)}: {_label(it)}{tail}")
        more = "" if len(items) <= limit else f"\n  ...and {len(items) - limit} more."
        return "\n".join(lines) + more

    # --- ARMING: a build run is active IFF this session owns a live claim, OR a non-stale run ---
    # marker scopes work to this session. Either signal arms; neither => inert (ordinary repo chat).
    if not owned_unfinished and not run_marked:
        _allow()

    # Scope the claimable set: if a run marker is present, restrict to marked tickets (the declared
    # scope); otherwise (pure owned-claim/legacy run) every claimable incomplete ticket is in scope.
    if run_marked:
        scoped_claimable = [c["_item"] for c in claimable if c["_in_run"]]
    else:
        scoped_claimable = [c["_item"] for c in claimable]

    # --- DEPENDENCY READINESS: split claimable into "ready to pop now" (every prerequisite finished)
    # and "waiting on deps". FIND only pops READY tickets; a ticket whose depends_on names an
    # unfinished/in_progress job stays parked until that job finishes. ``unfinished`` is computed over
    # the WHOLE incomplete set (not just scope) so a cross-scope prerequisite still gates correctly.
    unfinished = ts.unfinished_ids(incomplete)

    def _pending(it: dict) -> list[str]:
        return ts.pending_deps(it, unfinished)

    ready = [it for it in scoped_claimable if not _pending(it)]
    waiting = [it for it in scoped_claimable if _pending(it)]

    def _fmt_dep(items: list[dict], limit: int = 40) -> str:
        lines = []
        for it in items[:limit]:
            pend = _pending(it)
            tail = f" — waiting on {', '.join(pend)}" if pend else ""
            lines.append(f"  - {_rid(it)}: {_label(it)}{tail}")
        more = "" if len(items) <= limit else f"\n  ...and {len(items) - limit} more."
        return "\n".join(lines) + more

    # --- DONE? Armed, but no claimable work remains in scope (only finished + blocked left). -------
    if not owned_unfinished and not scoped_claimable:
        advice = ""
        if blocked:
            advice = ("build-completeness gate: scoped build set is FINISHED, but "
                      f"{len(blocked)} ticket(s) are BLOCKED and need owner action (they were NOT "
                      f"built):\n{_fmt(blocked)}\n"
                      "Resolve each via af-intake amend (supply the missing requirement/credential) "
                      "or record an explicit accept; they will not auto-complete.")
        _allow(advice)

    # --- DEPENDENCY STALL: armed, work remains, but NOTHING is owned or ready — every remaining
    # ticket is waiting on a dependency that will never finish on its own (a cycle, or all deps are
    # blocked). Surface it as a clear, actionable stall rather than churning forever on nothing.
    if not owned_unfinished and not ready and waiting:
        _block(
            f"build-completeness gate: DEPENDENCY STALL for {project}. "
            f"{len(waiting)} ticket(s) remain but NONE is ready — each waits on an unfinished/blocked "
            f"prerequisite, so no ticket can be popped:\n{_fmt_dep(waiting)}"
            + (f"\n\nBLOCKED prerequisites ({len(blocked)}):\n{_fmt(blocked)}" if blocked else "")
            + "\n\nThis is a cycle or a chain rooted on a blocked ticket. Break it: fix/unblock the "
            "root prerequisite (af-intake amend / accept), correct a wrong depends_on edge, or block() "
            "the unsatisfiable dependents. The loop cannot progress until a root becomes ready."
        )

    # --- ENFORCE: armed and READY work remains. Block until the whole scoped set is finished. ------
    parts: list[str] = [
        f"build-completeness gate: NOT DONE for {project}."
    ]
    if owned_unfinished:
        parts.append(f" This session owns {len(owned_unfinished)} unfinished in_progress ticket(s):\n"
                     f"{_fmt(owned_unfinished)}")
    if ready:
        scope_word = "scoped run" if run_marked else "incomplete set"
        parts.append(f"\n\nReady to claim in the {scope_word} ({len(ready)} ticket(s), all prerequisites "
                     f"finished) — pop EXACTLY ONE and ship it end-to-end before looking at the next:"
                     f"\n{_fmt(ready)}")
    if waiting:
        parts.append(f"\n\nWaiting on dependencies ({len(waiting)} ticket(s)) — do NOT claim until "
                     f"their prerequisites finish:\n{_fmt_dep(waiting)}")
    if blocked:
        parts.append(f"\n\nBLOCKED ({len(blocked)} ticket(s)) — excluded from churn, need owner "
                     f"action (af-intake amend / accept), surfaced so they are never silently "
                     f"dropped:\n{_fmt(blocked)}")

    _block(
        "".join(parts) + "\n\n"
        "Do not end the turn. Per the per-ticket lifecycle (docs/factory-state-contract.md):\n"
        "  1. heartbeat your live claim(s) so the lease (and run marker) stay valid;\n"
        "  2. POP a READY ticket (all depends_on finished) and claim it — never a waiting one;\n"
        "  3. resolve its validation REQUIREMENTS by QUERY (tag union surface);\n"
        "  4. SYNTHESIZE concrete validations that faithfully COVER every requirement, pin them;\n"
        "  5. run each validation + record each pass ON THE TICKET NODE (record_validation_pass);\n"
        "  6. when coverage is complete and every validation passes, release(state=\"finished\");\n"
        "  7. repeat until no ready ticket remains.\n"
        "A ticket that genuinely cannot be covered/run (credential-only, unsatisfiable) -> block() it "
        "so it is surfaced for owner action instead of wedging the loop. To intentionally end the run, "
        "clear_run() the scope. (Emergency-only stand-down: FACTORY_GATE_DISABLED=1.)"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        # A crash in the gate's own logic must not wedge the agent forever. This catches only
        # UNEXPECTED errors AFTER the fail-closed Praxis check above (which BLOCKS on its own); a
        # bug here should not masquerade as "Praxis down", so we exit cleanly (allow).
        sys.exit(0)
