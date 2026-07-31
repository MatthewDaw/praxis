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

from _gate_common import active_project as _active_project  # noqa: E402
from _gate_common import allow as _allow  # noqa: E402
from _gate_common import block as _block  # noqa: E402
from _gate_common import classify_unreachable, session_touched  # noqa: E402


# --------------------------------------------------------------------------- project / identity

def _session_owner(data: dict) -> str:
    """This session's claim-owner identity (matches the owner the build loop claims tickets with).

    ``FACTORY_TICKET_OWNER``, when set, OVERRIDES the CLI-assigned ``session_id`` (mirrors the
    ``FACTORY_PROJECT`` override pattern in ``_gate_common.active_project``). This is the seam a
    remote-job RESUME relies on (R29): the box service's background-session launcher stamps the
    relaunched ``claude --bg`` process with the JOB-SCOPED owner id recorded on the job row (R31),
    not a freshly-minted per-session id — every ``claude --bg`` invocation gets its own new
    ``session_id`` from the CLI, so a resume that let the gate fall through to the raw session_id
    would own neither the prior run's ticket claims nor its run marker, and the completeness gate
    would see no live claim and no matching run marker and go INERT (session ends immediately,
    having built nothing, while recording itself failed) — the exact silent failure this ticket
    closes. Unset (the default, every non-remote session), behavior is byte-identical to before.
    """
    override = str(os.environ.get("FACTORY_TICKET_OWNER") or "").strip()
    if override:
        return override
    return str(data.get("session_id") or data.get("sessionId") or "").strip()


# --------------------------------------------------------------------------- plan escalation guard (S8)

# TERMINAL PLAN ESCALATION (S8): the plan-completeness gate writes a durable escalation record to
# Praxis when its bounded-attempt cap is reached. THIS gate (build-completeness) must read that record
# and refuse the build phase while it exists — the downstream enforcement leg of the two-gate contract.
# This check runs BEFORE any ticket enumeration, so it blocks the build phase regardless of which
# session escalated or whether this session owns any tickets.

def _plan_escalation_check(project: str) -> str:
    """Check for a terminal plan escalation. Returns "" (empty) if the plan is clear, or a block
    reason string if the build phase must be refused. Raises PlanEscalationError on a corrupt counter
    so the caller can fail LOUD.

    FAIL-CLOSED on an UNKNOWN answer. An unexpected error here means we could not determine whether
    the plan is terminally escalated, and "could not determine" must never read as "clear" — that is
    how a gate silently stops gating. The ONE exception is a Praxis transport/import failure, which
    returns "" so it falls through to the dedicated PRAXIS UNREACHABLE block in :func:`main` (which
    also BLOCKS, with a far better message); every other exception blocks right here.
    """
    if not project:
        return ""
    try:
        import _ticket_state as ts  # inside the guard: a broken import must not fail open either
        escalation_error = ts.PlanEscalationError
    except Exception:  # noqa: BLE001 — a missing/broken helper is Praxis-unavailable-shaped
        return ""  # main()'s fail-closed read re-raises the same import failure and BLOCKS
    try:
        if ts.is_plan_blocked(project):
            return (
                f"build-completeness gate: PLAN BLOCKED for {project} — the plan terminally "
                "escalated (plan_blocked). The plan-completeness gate wrote a durable escalation "
                "record to Praxis; this gate now refuses the build phase while it exists. No ticket "
                "can be built until the plan's outstanding predicate is resolved and the escalation "
                "is cleared.\n\n"
                "Recovery paths:\n"
                "  1. Fix the failing plan predicate and re-run intake until the plan blesses — "
                "the bless auto-clears the escalation.\n"
                "  2. If the plan will never bless (unresolvable contradiction, out-of-scope term), "
                f"an operator may clear the escalation explicitly via clear_plan_blocked('{project}').\n"
                "  3. For emergency-only stand-down of THIS gate: FACTORY_GATE_DISABLED=1."
            )
    except escalation_error as exc:
        return (
            f"build-completeness gate: CORRUPT ESCALATION STATE for {project} — the plan escalation "
            f"counter cannot be read (PlanEscalationError). This is the 'named error' guard: a "
            f"corrupt or unreadable counter refuses the build phase rather than silently returning "
            f"zero and admitting a plan that should be blocked. Clear or repair the escalation state "
            f"on the planning marker before building. Detail: {exc}"
        )
    except Exception as exc:  # noqa: BLE001 — classified below; NEVER swallowed into "clear"
        is_unreachable, _ = classify_unreachable(exc)
        if is_unreachable:
            # Praxis (or its client module) is unavailable. Fall through: the fail-closed
            # ``incomplete_requirements`` read in main() BLOCKS on the same failure with the
            # preflight diagnostic attached, so this is a deferral to a better block, not a pass.
            return ""
        return (
            f"build-completeness gate: PLAN ESCALATION STATE UNREADABLE for {project} — the "
            f"escalation guard raised an unexpected {type(exc).__name__} ({exc}), so this gate could "
            f"NOT determine whether the plan is terminally escalated. 'Unknown' is not 'clear': a "
            f"guard that cannot answer refuses the build phase rather than silently admitting a plan "
            f"that may be blocked. Repair the planning marker / escalation state, then re-run. "
            f"(Emergency-only stand-down: FACTORY_GATE_DISABLED=1.)"
        )
    return ""


# --------------------------------------------------------------------------- no-op scope fast-path

# Substrings that mean THIS session engaged the factory in some way. The set is deliberately BROAD:
# a false positive only costs a fall-through to the normal (fail-closed) Praxis read — never a
# fail-OPEN — while a miss must never let an active build stand down, so we err toward "looks like
# factory work". A real builder session is saturated with these (it calls the praxis_* MCP tools and
# stamps run markers); an ordinary coding/chat session in a repo that merely HAS a plan contains none.
_FACTORY_SIGNALS = (
    "af-build", "af-intake", "factory_project", "factory_gate",
    "prd-", "praxis_", "mcp__praxis", "incomplete_requirements",
    "build_state", "claim_owner", "run_owner", "stamp_run", "building-validation",
)


def _emit_preflight_once(diagnostic: str) -> None:
    """Print a Praxis-auth diagnostic to STDERR the FIRST time a given failure is seen, then stay
    quiet for it. The Stop block reason (stdout JSON) is consumed by the headless `claude -p` retry
    loop and never surfaces to a human, so without this a misconfigured hook is invisible; a one-shot
    stderr line (deduped by a marker file so a tight loop doesn't spam) is what makes it diagnosable.
    """
    if not diagnostic:
        return
    try:
        import _praxis
        marker = _praxis._cache_path().parent / ".hook_preflight_emitted"
        digest = __import__("hashlib").sha256(diagnostic.encode("utf-8")).hexdigest()[:16]
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
            return  # already shouted about this exact failure
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest, encoding="utf-8")
    except Exception:  # noqa: BLE001 — dedup is best-effort; still emit if the marker fails
        pass
    print(f"[build-completeness gate] {diagnostic}", file=sys.stderr, flush=True)


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
        return ts.all_validations_passed(item if item.get("meta") else _rid(item))
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

    # --- Emergency escape hatches (documented + LOUD, never silent). -----------------------
    # When a disable variable causes a stand-down, RECORD the variable name and observed value
    # as durable state on the project's Praxis build marker so a run that executed with a
    # disabled gate cannot be presented as a fully gated run.
    auth_disabled = os.environ.get("PRAXIS_AUTH_DISABLED") == "1"
    gate_disabled = os.environ.get("FACTORY_GATE_DISABLED") == "1"

    if gate_disabled or auth_disabled:
        # Resolve the project so we can stamp the marker. Best-effort: if stamping fails,
        # still stand down (the disable var is the authority, not the marker write).
        try:
            import _ticket_state as ts
            _proj = _active_project(cwd)
            if _proj:
                if gate_disabled:
                    ts.stamp_gate_disable(_proj, "FACTORY_GATE_DISABLED", "1")
                if auth_disabled:
                    ts.stamp_gate_disable(_proj, "PRAXIS_AUTH_DISABLED", "1")
        except Exception:  # noqa: BLE001 - marker write is best-effort; never block the stand-down
            pass

        parts: list[str] = []
        if gate_disabled:
            parts.append("FACTORY_GATE_DISABLED=1: the factory is NOT verifying build state — "
                         "incomplete tickets/checks may remain unbuilt")
        if auth_disabled:
            parts.append("PRAXIS_AUTH_DISABLED=1: Praxis auth is bypassed — gate enforcement cannot "
                         "verify identity")
        _allow("build-completeness gate STOOD DOWN: " + " | ".join(parts)
               + ". Unset the named variable(s) to restore enforcement.")

    # Load the factory ``.env`` BEFORE resolving the project. ``_active_project`` reads
    # ``FACTORY_PROJECT`` from ``os.environ``, but that override lives in ``<repo>/.env`` (the same
    # place every other factory credential — PRAXIS_API_KEY / PRAXIS_ORG / PRAXIS_API_BASE_URL — is
    # configured), and a bare Stop-hook subprocess does NOT inherit a shell-sourced ``.env``. The
    # dotenv is loaded as a side effect of importing ``_praxis`` (its module-load ``_load_dotenv()``);
    # doing it here, ahead of ``_active_project``, is what makes the ``FACTORY_PROJECT`` .env override
    # actually take effect — so a repo whose dir name differs from the Praxis project name resolves
    # the RIGHT ``prd-<project>`` instead of silently falling back to the cwd basename and going inert.
    #
    # Best-effort + fail-closed-preserving: if ``_praxis`` cannot even be imported here we swallow it
    # and fall through — the real ``import _praxis`` + ``incomplete_requirements()`` below re-raises the
    # same failure and the fail-closed block BLOCKS loudly. This early load NEVER makes the gate fail
    # open (a wrong-project value would at worst appear in that BLOCK message, never allow a stop).
    try:
        import _praxis
        _praxis._load_dotenv()
    except Exception:  # noqa: BLE001 — a broken/absent _praxis re-raises in the fail-closed block below
        pass

    project = _active_project(cwd)
    owner = _session_owner(data)

    # --- S8 plan-escalation guard: if the plan is terminally escalated, refuse the build phase ---
    # BEFORE any ticket enumeration. This is the downstream enforcement leg — the plan-completeness
    # gate writes the escalation record; this gate reads it and blocks. The check is fail-closed:
    # a corrupt counter (PlanEscalationError) also refuses.
    escalation_block = _plan_escalation_check(project)
    if escalation_block:
        _block(escalation_block)

    # --- NO-OP FAST-PATH: a session that never touched the factory has nothing to verify. -----
    # The gate is loaded on EVERY session (any repo with the plugin), including ones doing zero
    # factory work. Such a session owns no claim and carries no run marker, so the arming rule below
    # would ALLOW it anyway — but only AFTER a hard Praxis read that fails CLOSED when Praxis is down,
    # needlessly blocking an unrelated session. If the transcript proves this session never engaged
    # the factory (zero signals), stand down here WITHOUT the Praxis dependency. This is safe: a real
    # build's transcript is saturated with factory signals, and any uncertainty (unreadable/oversized
    # transcript) returns None and falls through to the fail-closed read — it can never fail open.
    if session_touched(data.get("transcript_path"), _FACTORY_SIGNALS) is False:
        _allow()

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
        _, detail = classify_unreachable(exc)

        # PINPOINT the cause instead of a generic "check PRAXIS_* / auth". Preflight names EXACTLY
        # which of PRAXIS_API_BASE_URL / the identity cache / COGNITO_CLIENT_ID / PRAXIS_ORG is
        # missing or failing, and whether this is a MISCONFIG (never self-heals — the exact thing
        # that turned into a silent headless loop) or a transient outage. Emit it to stderr ONCE so
        # it surfaces even in `claude -p`, where the block reason itself is swallowed by the retry.
        diag = ""
        try:
            diag = _praxis.preflight(live=True).message()
        except Exception:  # noqa: BLE001 — a preflight crash must not replace the block with nothing
            diag = ""
        _emit_preflight_once(diag)
        _block(
            "build-completeness gate: PRAXIS UNREACHABLE — the factory cannot verify build state, so "
            "this gate is failing CLOSED and BLOCKING. Praxis is the single source of dynamic truth; "
            "without it there is no way to know whether tickets/checks are still incomplete.\n"
            f"  reason: {detail}\n"
            + (f"\nPREFLIGHT:\n{diag}\n" if diag else "")
            + "\nBring Praxis up (default http://localhost:8000) and/or fix the item(s) above, then "
            "try again. If this is a MISCONFIG it will NOT resolve by retrying — fix the named piece. "
            "For a real emergency ONLY, set FACTORY_GATE_DISABLED=1 to stand the gate down (loud, "
            "never silent)."
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
                      "Resolve each via af-intake-plan amend (supply the missing requirement/credential) "
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
            "root prerequisite (af-intake-plan amend / accept), correct a wrong depends_on edge, or block() "
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
                     f"action (af-intake-plan amend / accept), surfaced so they are never silently "
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


def _shout_crash(exc: BaseException) -> None:
    """LOUD, never silent: a crash in the gate's own logic means the gate did NOT enforce, and that
    must be visible. The Stop block reason (stdout JSON) is swallowed by the headless ``claude -p``
    retry loop, so stderr is the only channel a human/log actually sees — the same reason
    :func:`_emit_preflight_once` exists. Also records the stand-down on the project's build marker
    (best-effort) so a run that executed with a crashed gate cannot later be presented as gated."""
    import traceback
    sys.stderr.write(
        "[build-completeness gate] GATE CRASHED — NOT ENFORCED for this Stop. The gate's own logic "
        f"raised {type(exc).__name__}: {exc}. Build state was NOT verified; incomplete tickets or "
        "unrun checks may remain. This is a bug in the gate, not a Praxis outage — fix it.\n"
    )
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    try:
        import _ticket_state as ts
        proj = _active_project(os.getcwd())
        if proj:
            ts.stamp_gate_disable(proj, "GATE_CRASHED", type(exc).__name__)
    except Exception:  # noqa: BLE001 — durable record is best-effort; the stderr shout is the floor
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # A crash in the gate's own logic must not wedge the agent forever. This catches only
        # UNEXPECTED errors AFTER the fail-closed Praxis check above (which BLOCKS on its own); a
        # bug here should not masquerade as "Praxis down", so we exit cleanly (allow) — but LOUDLY,
        # never silently. A gate that disappears without a word is the exact failure this guard
        # used to cause: three tickets went FINISHED with a mandatory check that had silently
        # vanished, and nothing anywhere reported it.
        _shout_crash(exc)
        sys.exit(0)
