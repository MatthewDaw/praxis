#!/usr/bin/env python3
"""
Per-ticket lifecycle helpers, built on :mod:`_praxis`. Pure plumbing — deterministic, reads/writes
Praxis live, holds NO state of its own (no JSON manifests). This module defines the CANONICAL meta
keys for a ticket's build lifecycle; see ``docs/factory-state-contract.md``.

TWO-TIER VALIDATION (requirements -> synthesized validations)
-------------------------------------------------------------
A TICKET is a requirement fact in the ``prd-<project>`` graph. It carries identity (tags, surfaces,
semantics) but NEVER an authored list of its checks. Two distinct layers:

  * VALIDATION REQUIREMENTS — abstract "what must be proven" facts, stored in Praxis as
    ``category="check"``. WHICH apply to a ticket is a QUERY (tag union surface), resolved fresh at
    ticket start (``resolve_validation_requirements``). These are read-only during a build.
  * VALIDATIONS — concrete, executable instances the worker AUTHORS to faithfully COVER the resolved
    requirements (each declares the requirement ids it ``covers`` and a ``run`` command whose exit
    code is the external signal). The worker pins these onto the ticket (``pin_validations``).

A ticket is finished IFF (a) it has >=1 pinned validation, (b) EVERY resolved requirement is covered
by some pinned validation (no coverage gap), and (c) every pinned validation passed. The resolved
requirements are the coverage contract; the synthesized validations are the eval.

Lifecycle (see contract doc):
  start  -> claim, resolve_validation_requirements(), pin the requirement ids as the coverage
            contract (truncate prior pinned validations)
  build  -> the worker SYNTHESIZES validations covering every requirement, pin_validations()
  verify -> run each validation's command; record_validation_pass() per validation
  done   -> all_validations_passed() (coverage satisfied + all pass) AND release(state="finished")
  block  -> a requirement that cannot be covered/run (uncoverable, credential-only) -> build_state
            "blocked" (surfaced by the gate, excluded from churn) — NEVER a silent forever-deadlock

CANONICAL META KEYS (on the requirement/ticket node):
  build_state          : "incomplete" | "in_progress" | "finished" | "blocked"
  block_reason         : str   (why a ticket is blocked; surfaced, requires owner action)
  claim_owner          : str   (session/agent id holding the lease)
  claim_at             : float (epoch seconds, when first claimed)
  claim_heartbeat_at   : float (epoch seconds, last liveness bump)
  claim_lease_ttl      : int   (seconds; lease is stale when now - heartbeat > ttl)
  required_validations : list[str]   (resolved requirement ids — THIS pass's coverage contract)
  pinned_checks        : list[ {validation_id, covers:[req_id,...], run, passed:bool|None,
                                ran_at:float|None} ]   (the synthesized validations — the eval)
  run_owner            : str   (session id of the active WHOLE-SET build run this ticket is in)
  run_at               : float (epoch seconds, run-marker heartbeat; stale => run considered dead)
  run_scope            : str   (human label of the run's scope, for the gate's report)

(``pinned_checks`` keeps its key name for back-compat with the Praxis server claim view and the eval
harness; its entries now describe synthesized VALIDATIONS, not raw checks.)

RACE-TOLERANCE (v1)
-------------------
Claiming is a read-modify-write over ``patch_meta`` (PATCH /candidates/{cid}, which MERGES meta).
There is NO server-side CAS here: we read the live lease, decide locally, then write. Two agents can
therefore both decide a stale/free ticket is theirs and both write — a rare double-claim. That is
HARMLESS wasted work (idempotent rebuild), not corruption, so v1 accepts it. The lease is a LEASE,
not a lock: a lease whose heartbeat is older than its ttl is auto-reclaimable, so nothing dangles.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple, Optional

import _praxis
from _praxis import PraxisUnreachable  # re-exported so gates import one place  # noqa: F401

CHECK_CATEGORY = "check"

# Canonical meta keys.
M_BUILD_STATE = "build_state"
M_DEPENDS_ON = "depends_on"                 # prerequisite ticket ids that must be FINISHED first
M_BLOCK_REASON = "block_reason"
M_CLAIM_OWNER = "claim_owner"
M_CLAIM_AT = "claim_at"
M_CLAIM_HEARTBEAT_AT = "claim_heartbeat_at"
M_CLAIM_LEASE_TTL = "claim_lease_ttl"
M_REQUIRED_VALIDATIONS = "required_validations"
M_PINNED_CHECKS = "pinned_checks"           # entries are synthesized VALIDATIONS (see module doc)
M_RUN_OWNER = "run_owner"
M_RUN_AT = "run_at"
M_RUN_SCOPE = "run_scope"

_LEASE_KEYS = (M_CLAIM_OWNER, M_CLAIM_AT, M_CLAIM_HEARTBEAT_AT, M_CLAIM_LEASE_TTL)
_RUN_KEYS = (M_RUN_OWNER, M_RUN_AT, M_RUN_SCOPE)

DEFAULT_LEASE_TTL_S = 900    # 15 min — per-ticket claim lease
DEFAULT_RUN_TTL_S = 3600     # 60 min — whole-set run marker; refreshed at each ticket boundary

# The checks/state seam (org -> space -> snapshot tenancy). Every project is exactly ONE space
# (``space == the bare project name``); inside it the plan/ticket STATE lives in the ``prd-<project>``
# snapshot and the per-scope validation checks live in their own dedicated snapshots:
#   * scope="validation" (af-build per-ticket)   -> snapshot "building-validation"
#   * scope="planning"   (af-intake-plan whole-plan)  -> snapshot "planning-validation"
# ``project_ref`` is the SINGLE typed source of truth for these three (space, snapshot) pairs; it
# replaces the old free-form ``checks_ref`` plumbing (a sentinel + a 4-way branch). The skills may
# still override the checks reference per-invocation (their ``--checks-space`` argument) by passing a
# single explicit ``(space, snapshot)`` pair as ``override=``.
DEFAULT_VALIDATION_CHECKS_SNAPSHOT = "building-validation"
DEFAULT_PLANNING_CHECKS_SNAPSHOT = "planning-validation"


class ProjectRef(NamedTuple):
    """The three ``(space, snapshot)`` pairs a project's factory lanes bind to.

    ``plan`` holds ticket STATE (``prd-<project>``); ``validation`` / ``planning`` hold the per-scope
    checks. All three sit in the same project space (``space == the bare project name``).
    """

    plan: tuple[str, str]
    validation: tuple[str, str]
    planning: tuple[str, str]

    def for_scope(self, scope: str) -> tuple[str, str]:
        """The checks ``(space, snapshot)`` a resolve scope reads. Scope is REQUIRED — a check read
        must always resolve to a real snapshot, never working memory:
        ``"validation"`` -> ``building-validation``, ``"planning"`` -> ``planning-validation``.
        Any other value (including ``None``) is a programming error.
        """
        if scope == "validation":
            return self.validation
        if scope == "planning":
            return self.planning
        raise ValueError(
            f"unsupported check scope {scope!r}; expected 'validation' or 'planning'"
        )


def project_ref(project: str) -> ProjectRef:
    """Build the typed ``(space, snapshot)`` references for ``project``.

    A leading ``prd-`` is stripped, so callers may pass either the bare project name or the
    ``prd-<project>`` snapshot name and get the same references.
    """
    bare = project[4:] if project.startswith("prd-") else project
    return ProjectRef(
        plan=(bare, f"prd-{bare}"),
        validation=(bare, DEFAULT_VALIDATION_CHECKS_SNAPSHOT),
        planning=(bare, DEFAULT_PLANNING_CHECKS_SNAPSHOT),
    )


def _checks_target(project: str, scope: str,
                   override: Optional[tuple[str, str]]) -> tuple[str, str]:
    """The ``(space, snapshot)`` a check READ binds to: the explicit ``override`` pair if given, else
    the per-scope default from :func:`project_ref`."""
    return override or project_ref(project).for_scope(scope)


# --------------------------------------------------------------------------- helpers

def _ref_kw(ref: Optional[tuple[str, str]]) -> dict:
    """Unpack a ``(space, snapshot)`` plan ref into ``_praxis`` space/snapshot kwargs.

    Ticket STATE lives on the ``prd-<project>`` snapshot (``ref = project_ref(project).plan``),
    so every state read/write threads that ref. ``None`` (the default) resolves to the
    caller's working memory — the back-compat lane for non-project callers.
    """
    return {"space": ref[0], "snapshot": ref[1]} if ref else {}


def _meta(ticket: Any, ref: Optional[tuple[str, str]] = None) -> dict:
    """Extract the meta dict from a ticket id (str) or an already-fetched fact (dict)."""
    if isinstance(ticket, str):
        ticket = _praxis.get_fact(ticket, **_ref_kw(ref))
    return dict((ticket or {}).get("meta") or {})


def _ticket_id(ticket: Any) -> str:
    if isinstance(ticket, str):
        return ticket
    cid = (ticket or {}).get("id") or (ticket or {}).get("factId")
    if not cid:
        raise ValueError("ticket fact has no id")
    return str(cid)


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _check_id(check: Any) -> str:
    if isinstance(check, str):
        return check
    return str((check or {}).get("id") or (check or {}).get("check_id") or "")


def _scope_of(check: Any) -> str:
    """A check's scope ("planning" | "validation" | ...), from the top-level column or meta."""
    if not isinstance(check, dict):
        return ""
    return str(check.get("scope") or (check.get("meta") or {}).get("scope") or "")


# --------------------------------------------------------------------------- requirement resolution

def resolve_validation_requirements(ticket: Any, project: str = "",
                                    scope: str = "validation",
                                    override: Optional[tuple[str, str]] = None) -> list[dict]:
    """Resolve WHICH abstract validation REQUIREMENTS apply — a fresh QUERY, never a pre-bound list.
    These are the "what must be proven" facts the worker must then COVER with synthesized validations.

    The check facts are READ from the ``(space, snapshot)`` given by :func:`project_ref` for ``scope``
    (validation -> building-validation, planning -> planning-validation), independent of the
    ``prd-<project>`` snapshot used for state. The skills' ``--checks-space`` argument overrides that
    by passing a single explicit ``(space, snapshot)`` pair as ``override=``.

    ``scope`` is the ONE seam between the two callers — everything downstream (pin / coverage / pass)
    is identical:
      * ``scope="planning"`` (af-intake-plan WHOLE-PLAN pass) — planning lenses are GLOBAL considerations
        (``applies_when``, NOT tag/surface-bound), so resolve the ENTIRE active planning checklist
        regardless of the subject's tags/surfaces: the whole plan must satisfy every lens. ``ticket``
        is the plan-anchor subject the coverage contract hangs on.
      * ``scope="validation"`` (af-build PER-TICKET) — tag union surface match (below), filtered to
        validation-scope checks.

    Tag union surface (the per-ticket lanes):
      * TAG match — for each tag on the ticket (meta.tags / meta.applies_to), enumerate active
        ``check`` facts whose ``meta.applies_to`` contains that tag (array-membership; ``"*"`` wildcard).
      * SURFACE match — for each surface the ticket renders, enumerate checks bound via ``renders``.

    This function is the MANDATORY (precise) contract only: tag ∪ ``"*"`` wildcard ∪ surface. The
    SEMANTIC lane is deliberately separate (:func:`retrieve_advisory_checks`) and ADVISORY — it feeds
    the worker candidate checks as inspiration but never gates completion, so a fuzzy retrieval that is
    irrelevant is simply not authored, while a precise tag/surface/wildcard match is always covered.
    """
    # Which (space, snapshot) the CHECK reads target (default per scope; overridable by the skills).
    space, snapshot = _checks_target(project, scope, override)

    # PLANNING — the whole plan must satisfy every active planning lens (global, applies_when-bound),
    # so resolve the entire planning checklist; the per-ticket tag/surface lanes don't apply.
    if scope == "planning":
        out: dict[str, dict] = {}
        for chk in _praxis.facts_by(category=CHECK_CATEGORY, space=space, snapshot=snapshot):
            if _scope_of(chk) == "planning":
                cid = _check_id(chk)
                if cid:
                    out.setdefault(cid, chk)
        return list(out.values())

    # The TICKET (its tags/surfaces) is read from the PLAN snapshot (prd-<project>),
    # separate from the CHECK reads above; a ticket passed as an already-fetched dict
    # needs no fetch, a bare cid resolves against the plan ref.
    meta = _meta(ticket, project_ref(project).plan if project else None)
    seen: dict[str, dict] = {}

    tags = _as_list(meta.get("tags")) + _as_list(meta.get("applies_to"))
    for tag in {str(t) for t in tags if t}:
        for chk in _praxis.facts_by(category=CHECK_CATEGORY, meta={"applies_to": tag},
                                    space=space, snapshot=snapshot):
            cid = _check_id(chk)
            if cid:
                seen.setdefault(cid, chk)

    # Universal ("*") gates apply to EVERY ticket (incl. a tag-less one) — pull them explicitly. A
    # per-tag query can NEVER surface a ``["*"]`` check, because the ticket's concrete tags never
    # include the literal "*" (array-membership matches the STORED value, not a wildcard). Without
    # this the baseline typecheck/build/lint/test floor authored as ``applies_to:["*"]`` silently
    # fails to resolve. This lane is MANDATORY (part of the coverage contract), like tag/surface.
    for chk in _praxis.facts_by(category=CHECK_CATEGORY, meta={"applies_to": "*"},
                                space=space, snapshot=snapshot):
        cid = _check_id(chk)
        if cid:
            seen.setdefault(cid, chk)

    surfaces = _as_list(meta.get("surfaces")) + _as_list(meta.get("screen_ids")) \
        + _as_list(meta.get("screen_id"))
    if project:
        for screen in {str(s) for s in surfaces if s}:
            try:
                for chk in _praxis.surface_checks(project, screen, space=space, snapshot=snapshot):
                    cid = _check_id(chk)
                    if cid:
                        seen.setdefault(cid, chk)
            except PraxisUnreachable:
                raise
            except Exception:  # noqa: BLE001 - a malformed surface entry must not drop tag matches
                continue

    if scope:  # e.g. scope="validation" — restrict the per-ticket match to that check scope
        seen = {k: v for k, v in seen.items() if _scope_of(v) == scope}
    return list(seen.values())


def retrieve_advisory_checks(ticket: Any, project: str = "", scope: str = "validation",
                             override: Optional[tuple[str, str]] = None,
                             top_k: int = 10) -> list[dict]:
    """The SEMANTIC lane — ADVISORY candidate checks discovered by hybrid retrieval against the
    ticket's own text (title + acceptance). These are INSPIRATION for the worker's synthesis step,
    NOT the coverage contract: they are never pinned as ``required_validations`` and never gate
    completion. The worker folds the relevant ones into its authored validations and ignores the
    rest — so an irrelevant retrieval is harmless (the point of keeping semantics OUT of the hard
    gate). Reads from the checks ``(space, snapshot)`` (same seam/default as the mandatory lanes;
    override with an explicit ``(space, snapshot)`` pair). Returns ``category="check"`` hits only,
    de-duplicated, filtered to ``scope``.
    """
    space, snapshot = _checks_target(project, scope, override)
    plan = project_ref(project).plan if project else None
    fact = ticket if isinstance(ticket, dict) else _praxis.get_fact(ticket, **_ref_kw(plan))
    text = " ".join(str(x) for x in (
        (fact or {}).get("text") or (fact or {}).get("content") or "",
        _meta(fact).get("acceptance") or "",
    ) if x).strip()
    if not text:
        return []
    out: dict[str, dict] = {}
    for hit in _praxis.context(text, top_k=top_k, space=space, snapshot=snapshot):
        if str(hit.get("category") or (hit.get("meta") or {}).get("category") or "") != CHECK_CATEGORY:
            continue
        if scope and _scope_of(hit) not in ("", scope):  # allow unscoped hits; drop cross-scope ones
            continue
        cid = _check_id(hit)
        if cid:
            out.setdefault(cid, hit)
    return list(out.values())


# --------------------------------------------------------------------------- coverage contract

def pin_requirements(cid: str, requirements: list,
                     ref: Optional[tuple[str, str]] = None) -> dict:
    """Pin the resolved REQUIREMENT ids as this pass's coverage contract and TRUNCATE any prior
    synthesized validations. After this, the ticket lists what must be covered (``required_validations``)
    and has an empty validation set (``pinned_checks``) the worker must now author + pin.
    """
    req_ids = [rid for rid in (_check_id(r) for r in requirements) if rid]
    return _praxis.patch_meta(cid, {
        M_REQUIRED_VALIDATIONS: req_ids,
        M_PINNED_CHECKS: [],
    }, **_ref_kw(ref))


def _norm_validation(v: Any, idx: int) -> dict:
    """Normalize one worker-authored validation into the pinned entry shape.

    Accepts a dict with ``covers`` (req id or list), ``run`` (command), and optional ``validation_id``.
    A missing id is synthesized stably from its covered requirements + index so passes can be recorded.
    """
    if not isinstance(v, dict):
        v = {"run": str(v)}
    covers = [str(c) for c in _as_list(v.get("covers") or v.get("requirement_id")
                                       or v.get("covers_requirement")) if c]
    vid = str(v.get("validation_id") or v.get("id") or "").strip()
    if not vid:
        base = "+".join(covers) if covers else "validation"
        vid = f"{base}#{idx}"
    return {
        "validation_id": vid,
        "covers": covers,
        "run": str(v.get("run") or v.get("command") or ""),
        "passed": None,
        "ran_at": None,
    }


def pin_validations(cid: str, validations: list,
                    ref: Optional[tuple[str, str]] = None) -> dict:
    """Pin the worker-SYNTHESIZED concrete validations onto the ticket (the eval).

    Each entry: ``{validation_id, covers:[req_id,...], run, passed=None, ran_at=None}``. Because
    ``patch_meta`` replaces ``pinned_checks`` wholesale, this TRUNCATES any prior validation state —
    the new set is THIS pass's eval. Does NOT touch ``required_validations`` (the coverage contract
    set at start): coverage is asserted at finish via :func:`coverage_gap` / :func:`all_validations_passed`.
    """
    pinned = [_norm_validation(v, i) for i, v in enumerate(validations)]
    pinned = [p for p in pinned if p["validation_id"]]
    return _praxis.patch_meta(cid, {M_PINNED_CHECKS: pinned}, **_ref_kw(ref))


def record_validation_pass(cid: str, validation_id: str, passed: bool,
                           ran_at: Optional[float] = None,
                           ref: Optional[tuple[str, str]] = None) -> dict:
    """Record one validation's pass/fail ON THE TICKET NODE (never on the requirement fact).

    Read-modify-write of ``meta.pinned_checks``: update the matching validation's passed/ran_at. If
    the validation is not already pinned (set drifted), it is appended so the result is not lost.
    """
    if ran_at is None:
        ran_at = time.time()
    meta = _meta(cid, ref)
    pinned = list(meta.get(M_PINNED_CHECKS) or [])
    found = False
    for entry in pinned:
        eid = entry.get("validation_id") or entry.get("check_id")
        if str(eid) == str(validation_id):
            entry["passed"] = bool(passed)
            entry["ran_at"] = ran_at
            found = True
            break
    if not found:
        pinned.append({"validation_id": str(validation_id), "covers": [],
                       "run": "", "passed": bool(passed), "ran_at": ran_at})
    return _praxis.patch_meta(cid, {M_PINNED_CHECKS: pinned}, **_ref_kw(ref))


def coverage_gap(ticket: Any, ref: Optional[tuple[str, str]] = None) -> list[str]:
    """Requirement ids in the coverage contract NOT covered by any pinned validation.

    Empty list == every resolved requirement is faithfully covered. A non-empty list means the
    synthesized validations do not yet cover the contract — the ticket cannot be finished.
    """
    meta = _meta(ticket, ref)
    required = {str(r) for r in (meta.get(M_REQUIRED_VALIDATIONS) or []) if r}
    if not required:
        return []
    covered: set[str] = set()
    for entry in (meta.get(M_PINNED_CHECKS) or []):
        for c in (entry.get("covers") or []):
            covered.add(str(c))
    return sorted(required - covered)


def all_validations_passed(ticket: Any, ref: Optional[tuple[str, str]] = None) -> bool:
    """True IFF the ticket is genuinely done: it has a coverage contract (>=1 required requirement),
    every required requirement is covered by some pinned validation (no coverage gap), there is at
    least one pinned validation, and EVERY pinned validation passed.

    A ticket with no resolved requirements returns False — it cannot self-certify "no requirements
    therefore done"; that is a BLOCK condition (use :func:`block`), surfaced for owner action, never
    a silent pass. (An intentionally validation-free ticket must carry an explicit always-pass
    requirement, authored upstream.)
    """
    meta = _meta(ticket, ref)
    required = {str(r) for r in (meta.get(M_REQUIRED_VALIDATIONS) or []) if r}
    pinned = list(meta.get(M_PINNED_CHECKS) or [])
    if not required or not pinned:
        return False
    covered: set[str] = set()
    for entry in pinned:
        for c in (entry.get("covers") or []):
            covered.add(str(c))
    if not required.issubset(covered):   # coverage gap — compute inline (meta already extracted)
        return False
    return all(bool(e.get("passed")) for e in pinned)


# --------------------------------------------------------------------------- claiming / lease

def _lease_live(meta: dict, now: Optional[float] = None) -> bool:
    """True iff the ticket is in_progress with a non-stale heartbeat (now - hb <= ttl)."""
    if now is None:
        now = time.time()
    if meta.get(M_BUILD_STATE) != "in_progress":
        return False
    hb = meta.get(M_CLAIM_HEARTBEAT_AT)
    ttl = meta.get(M_CLAIM_LEASE_TTL)
    if hb is None or ttl is None:
        return False
    try:
        return (now - float(hb)) <= float(ttl)
    except (TypeError, ValueError):
        return False


def claim(cid: str, owner: str, ttl: int = DEFAULT_LEASE_TTL_S,
          ref: Optional[tuple[str, str]] = None) -> bool:
    """Claim a ticket (incomplete -> in_progress) for ``owner``, race-tolerantly.

    Read the live lease, then claim IFF the ticket is free to claim: not in_progress, OR ``owner``
    already holds it (idempotent renew), OR the existing lease is STALE (auto-reclaim). On success
    stamps claim_owner/claim_at/claim_heartbeat_at/claim_lease_ttl and sets build_state=in_progress.
    Returns True if we now hold the lease, False if a DIFFERENT owner holds a LIVE lease, or the
    ticket is terminally ``blocked`` (needs owner action, not a build claim).

    Race note: two agents can both read a free ticket and both write — a rare, harmless double-claim
    (see module docstring). No server CAS is assumed.
    """
    meta = _meta(cid, ref)
    if meta.get(M_BUILD_STATE) == "blocked":
        return False  # blocked needs owner action (af-intake-plan amend / accept), not a build claim
    now = time.time()
    if _lease_live(meta, now) and meta.get(M_CLAIM_OWNER) != owner:
        return False  # a different owner holds a live lease
    held_at = meta.get(M_CLAIM_AT)
    if meta.get(M_CLAIM_OWNER) != owner or held_at is None:
        held_at = now  # first claim by this owner stamps claim_at
    _praxis.patch_meta(cid, {
        M_BUILD_STATE: "in_progress",
        M_CLAIM_OWNER: owner,
        M_CLAIM_AT: held_at,
        M_CLAIM_HEARTBEAT_AT: now,
        M_CLAIM_LEASE_TTL: int(ttl),
    }, **_ref_kw(ref))
    return True


def heartbeat(cid: str, owner: str, ref: Optional[tuple[str, str]] = None) -> bool:
    """Bump ``claim_heartbeat_at`` IFF ``owner`` still holds a live lease. Also refreshes the
    whole-set run marker on this ticket (``run_at``) so the active run stays live. Returns success.

    If the lease has gone stale or been taken over, returns False without writing — the owner has
    lost the lease and should re-claim (or yield).
    """
    meta = _meta(cid, ref)
    if meta.get(M_CLAIM_OWNER) != owner or not _lease_live(meta):
        return False
    patch: dict[str, Any] = {M_CLAIM_HEARTBEAT_AT: time.time()}
    if meta.get(M_RUN_OWNER) == owner:
        patch[M_RUN_AT] = time.time()
    _praxis.patch_meta(cid, patch, **_ref_kw(ref))
    return True


def release(cid: str, owner: str, state: str,
            ref: Optional[tuple[str, str]] = None) -> bool:
    """Release ``owner``'s lease and set a terminal build_state ("finished" or "incomplete").

    Drops the lease keys (so nothing dangles) and stamps build_state, MERGING so identity keys
    (tags/surfaces/required_validations/pinned_checks) survive. On ``finished`` the run marker is
    also cleared (the ticket has left the active run). On ``incomplete`` the run marker is KEPT so
    the whole-set gate keeps the ticket in scope and forces it to be re-done (a clean yield does not
    end the run). Only the holding owner may release; a mismatch returns False without writing.
    ``patch_meta`` cannot delete keys, so cleared keys are NULLED out.
    """
    if state not in ("finished", "incomplete"):
        raise ValueError("state must be 'finished' or 'incomplete'")
    meta = _meta(cid, ref)
    if meta.get(M_CLAIM_OWNER) not in (owner, None):
        return False
    patch: dict[str, Any] = {M_BUILD_STATE: state}
    for k in _LEASE_KEYS:
        patch[k] = None  # MERGE can't remove keys; null them so _lease_live reads not-live
    if state == "finished":
        for k in _RUN_KEYS:
            patch[k] = None  # left the run cleanly
    _praxis.patch_meta(cid, patch, **_ref_kw(ref))
    return True


def block(cid: str, owner: str, reason: str,
          ref: Optional[tuple[str, str]] = None) -> bool:
    """Mark a ticket TERMINALLY BLOCKED — it cannot proceed autonomously (an uncoverable requirement,
    a credential/secret only the owner can supply, an unsatisfiable target). The gate surfaces blocked
    tickets prominently but EXCLUDES them from the churn set, so a blocked ticket is "a clear thing
    that forces a stop and cannot be progressed" — never a silent forever-deadlock.

    Sets build_state="blocked" + block_reason, and clears BOTH the lease and the run marker (the
    ticket has left the active run; clearing it must be owner action via af-intake-plan amend / accept).
    Only the holding owner (or an unclaimed ticket) may block; mismatch returns False.
    """
    meta = _meta(cid, ref)
    if meta.get(M_CLAIM_OWNER) not in (owner, None):
        return False
    patch: dict[str, Any] = {M_BUILD_STATE: "blocked", M_BLOCK_REASON: str(reason)}
    for k in _LEASE_KEYS:
        patch[k] = None
    for k in _RUN_KEYS:
        patch[k] = None
    _praxis.patch_meta(cid, patch, **_ref_kw(ref))
    return True


# --------------------------------------------------------------------------- whole-set run marker

def run_live(meta: dict, now: Optional[float] = None) -> bool:
    """True iff ``meta`` carries a NON-STALE whole-set run marker (now - run_at <= DEFAULT_RUN_TTL_S).

    The run marker is how the gate knows a build run is active for a whole (optionally scoped) set,
    independent of whether the session currently holds a per-ticket claim — that is what closes the
    between-ticket stop window. A stale marker (a dead/abandoned run) is ignored so nothing strands.
    """
    if now is None:
        now = time.time()
    if not meta.get(M_RUN_OWNER):
        return False
    at = meta.get(M_RUN_AT)
    if at is None:
        return False
    try:
        return (now - float(at)) <= float(DEFAULT_RUN_TTL_S)
    except (TypeError, ValueError):
        return False


def stamp_run(cids: list[str], owner: str, scope: str = "all",
              ref: Optional[tuple[str, str]] = None) -> int:
    """Mark each ticket id as belonging to ``owner``'s active WHOLE-SET run (run_owner/run_at/run_scope).

    Called at run start over the resolved in-scope incomplete ticket ids. This is the persisted,
    scope-bearing "a build run is active" signal the gate enforces against — so the gate keeps
    blocking until the ENTIRE marked set is finished, not just while a single claim is held. Returns
    the count stamped. Stamping is idempotent (re-stamping refreshes run_at).
    """
    now = time.time()
    n = 0
    for cid in cids:
        if not cid:
            continue
        _praxis.patch_meta(str(cid), {M_RUN_OWNER: owner, M_RUN_AT: now, M_RUN_SCOPE: str(scope)},
                           **_ref_kw(ref))
        n += 1
    return n


def refresh_run(cids: list[str], owner: str,
                ref: Optional[tuple[str, str]] = None) -> int:
    """Bump ``run_at`` on each still-in-scope ticket this session owns the run for (heartbeat the
    whole-set marker so a long run never goes stale mid-flight). Call at each ticket boundary. Only
    refreshes tickets actually carrying THIS owner's marker. Returns the count refreshed."""
    now = time.time()
    n = 0
    for cid in cids:
        if not cid:
            continue
        if _meta(cid, ref).get(M_RUN_OWNER) == owner:
            _praxis.patch_meta(str(cid), {M_RUN_AT: now}, **_ref_kw(ref))
            n += 1
    return n


def clear_run(cids: list[str], owner: str,
              ref: Optional[tuple[str, str]] = None) -> int:
    """Clear this session's whole-set run marker from each ticket (NULL run_owner/run_at/run_scope).

    Call when the run ends legitimately — the scoped set is finished (or intentionally aborted). After
    this the gate sees no active run for the session and goes inert. Only clears tickets carrying THIS
    owner's marker. Returns the count cleared."""
    n = 0
    for cid in cids:
        if not cid:
            continue
        if _meta(cid, ref).get(M_RUN_OWNER) == owner:
            _praxis.patch_meta(str(cid), {k: None for k in _RUN_KEYS}, **_ref_kw(ref))
            n += 1
    return n


# --------------------------------------------------------------------------- dependency readiness

def deps_of(ticket: Any, ref: Optional[tuple[str, str]] = None) -> list[str]:
    """The prerequisite ticket ids this ticket ``depends_on`` (must be FINISHED before it can run)."""
    return [str(d) for d in _as_list(_meta(ticket, ref).get(M_DEPENDS_ON)) if d]


def _ids_of(item: dict) -> set[str]:
    """Every id a dependency might name this ticket by — its fact id AND its plan requirement id —
    so ``depends_on`` may be written as either ``"R12"`` (requirement id) or the raw fact id."""
    ids: set[str] = set()
    for k in ("id", "factId", "fact_id"):
        v = item.get(k)
        if v:
            ids.add(str(v))
    meta = item.get("meta") or {}
    for k in ("requirement_id", "rid"):
        v = meta.get(k)
        if v:
            ids.add(str(v))
    return ids


def unfinished_ids(items: list[dict]) -> set[str]:
    """The id set of every ticket in ``items`` that is NOT finished (incomplete | in_progress |
    blocked). A dependency is SATISFIED iff none of the ids it names appears here."""
    out: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        if (it.get("meta") or {}).get(M_BUILD_STATE) == "finished":
            continue
        out |= _ids_of(it)
    return out


def is_ready(item: dict, unfinished: set[str]) -> bool:
    """True iff every prerequisite of ``item`` is satisfied — i.e. NONE of its ``depends_on`` ids is
    still in the ``unfinished`` set (so it depends on no unfinished or in-progress job)."""
    deps = set(deps_of(item))
    return not (deps & unfinished)


def pending_deps(item: dict, unfinished: set[str]) -> list[str]:
    """Which of ``item``'s dependencies are still unfinished (empty == ready to claim)."""
    return sorted(set(deps_of(item)) & unfinished)


def ready_tickets(items: list[dict]) -> list[dict]:
    """From a live incomplete set, the tickets that are CLAIMABLE NOW — not finished, not blocked,
    and depending on no unfinished/in-progress job. The dependency-respecting queue front. Order within
    the result preserves the server's (dependency/recency) order.

    NOTE: af-build works strictly ONE ticket end-to-end; use :func:`next_ready_ticket` to pop the single
    front. This full list exists for the gate's report and for picking among equally-ready candidates —
    not for batching work.
    """
    unfinished = unfinished_ids(items)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if (it.get("meta") or {}).get(M_BUILD_STATE) in ("finished", "blocked"):
            continue
        if is_ready(it, unfinished):
            out.append(it)
    return out


def next_ready_ticket(items: list[dict]) -> Optional[dict]:
    """Pop the SINGLE next dependency-ready ticket (queue front), or None if nothing is ready.

    This is the only thing FIND needs: af-build claims and fully ships ONE ticket end-to-end before it
    even looks at another, so it pops one here, works it to ``finished``, then calls FIND again. None
    means either the scoped set is done, or every remaining ticket is waiting/blocked (a stall the gate
    surfaces). It never returns a batch — one ticket at a time is the whole discipline.
    """
    ready = ready_tickets(items)
    return ready[0] if ready else None


# --------------------------------------------------------------------------- acceptance floor

def acceptance_requirement(cid: str, acceptance_text: str) -> dict:
    """The ticket's OWN binary acceptance condition as a synthetic validation requirement.

    This is the coverage-contract FLOOR. Every build ticket must at minimum prove its acceptance
    condition, so including it guarantees the resolved contract is never empty. An empty contract is
    exactly the deadlock this prevents: with zero requirements there is nothing to cover, the worker
    pins zero validations, and ``all_validations_passed`` can never become True — the ticket can be
    neither finished nor (without an explicit block) escaped. The floor gives the worker a concrete,
    always-authorable target: the red→green acceptance test the skill already mandates.
    """
    return {"id": f"{cid}::acceptance", "text": str(acceptance_text),
            "meta": {"acceptance": str(acceptance_text), "synthetic": "acceptance-floor"}}


def contract_with_floor(cid: str, acceptance_text: str, resolved: list) -> list:
    """Compose the coverage contract: the resolved Praxis requirements PLUS the acceptance floor.

    Prepends :func:`acceptance_requirement` (deduped) when the ticket has a non-empty acceptance
    condition, so a ticket with NO matching Praxis checks still has exactly one thing to validate —
    its own acceptance — and can therefore be finished. A ticket with neither resolved checks NOR an
    acceptance condition returns an empty list: a genuine planning defect the build surfaces by
    ``block()``-ing the ticket (never a silent wedge), since there is nothing it could honestly prove.
    """
    reqs = list(resolved)
    text = str(acceptance_text or "").strip()
    if text:
        floor = acceptance_requirement(cid, text)
        if floor["id"] not in {_check_id(r) for r in reqs}:
            reqs = [floor] + reqs
    return reqs


# --------------------------------------------------------------------------- start

def start_ticket(cid: str, owner: str, project: str = "",
                 ttl: int = DEFAULT_LEASE_TTL_S,
                 override: Optional[tuple[str, str]] = None) -> Optional[list[dict]]:
    """Convenience: claim, then resolve the validation REQUIREMENTS (PLUS the acceptance-condition
    floor) and pin them as this pass's coverage contract (truncating any prior synthesized validations).

    Validation checks are READ from the ``scope="validation"`` default snapshot (building-validation)
    inside the project space; pass the skill's ``--checks-space`` as an explicit ``(space, snapshot)``
    ``override=`` to redirect the read.

    Returns the requirement facts the worker must now COVER with synthesized validations — ALWAYS
    including the ticket's own acceptance condition as a floor (so the contract is never empty and the
    ticket can never be wedged "no evals therefore un-closeable"), or None if the claim was lost to
    another live owner / the ticket is blocked. The ticket does NOT auto-pin validations here —
    synthesis is the worker's job, and ``all_validations_passed`` stays False until it covers + passes
    every requirement. If the returned list is EMPTY (a ticket with no checks AND no acceptance
    condition — a planning defect), the worker must ``block()`` it: there is nothing to validate.
    """
    # Ticket STATE lives on the plan snapshot; claim/read/pin all bind to it. Check
    # reads use their own per-scope snapshot (resolve derives it from project + override).
    plan = project_ref(project).plan if project else None
    if not claim(cid, owner, ttl=ttl, ref=plan):
        return None
    resolved = resolve_validation_requirements(cid, project=project, scope="validation",
                                               override=override)
    requirements = contract_with_floor(cid, _meta(cid, plan).get("acceptance"), resolved)
    pin_requirements(cid, requirements, ref=plan)
    return requirements
