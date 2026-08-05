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
  manual_requirements  : list[str]   (subset of required_validations whose verify=="manual"; each
                                       needs an external/human-sourced pass — never worker-self-checked)
  pinned_checks        : list[ {validation_id, covers:[req_id,...], run, passed:bool|None,
                                ran_at:float|None, source:str} ]   (the synthesized validations — the
                                eval; ``source`` names where a pass came from: the default worker-run
                                source can never satisfy a manual requirement)
  run_owner            : str   (session id of the active WHOLE-SET build run this ticket is in)
  run_at               : float (epoch seconds, run-marker heartbeat; stale => run considered dead)
  run_scope            : str   (human label of the run's scope, for the gate's report)

(``pinned_checks`` keeps its key name for back-compat with the Praxis server claim view and the eval
harness; its entries now describe synthesized VALIDATIONS, not raw checks.)

WHERE BUILD STATE IS WRITTEN (and why it is NOT ``patch_meta``)
---------------------------------------------------------------
Every key above is BUILD STATE: what the loop LEARNS while executing a plan. It is not plan
content, and it is deliberately NOT written through ``PATCH /candidates/{cid}`` (``patch_meta``),
which is subject to the S12 bless guard: once ``prd-<project>`` is blessed, that path refuses
edits unless the planning marker is re-armed. Correct for the plan; fatal for the build. On a
blessed plan every claim, every check pin and every finish was refused — the loop dispatched
tickets no worker could take, no worktree and no branch was ever created, and a pin refused in
silence left ``pinned_checks: []`` that read afterwards as "RESOLVE never ran". The only
workaround was to unbless and re-bless the plan around each write, i.e. to mutate the plan as a
side effect of building it.

So build state goes through the sanctioned, UNGUARDED endpoints instead, and the guard is left
exactly as it is:

  * ``POST /requirements/{cid}/claim``        -> :func:`claim`
  * ``POST /requirements/{cid}/release``      -> :func:`release`
  * ``POST /requirements/regress``            -> ``_praxis.regress_requirements``
  * ``POST /requirements/{cid}/build-state``  -> everything else here (pins, block, run marker)

The server accepts ONLY build-lifecycle keys on that last route, so its writable surface is
disjoint from what the bless guard protects rather than a hole in it. The only ``patch_meta``
calls left in this module write the PLANNING MARKER — the guard's own control surface, which it
exempts by name.

LEASES
------
A claim is a LEASE, not a lock: one whose heartbeat is older than its ttl is auto-reclaimable, so
a dead agent never leaves a ticket dangling. The grant itself is now ATOMIC server-side (a single
conditional UPDATE), so two agents racing the same free ticket produce exactly one winner — the
double-claim the earlier client-side read-modify-write accepted as "rare and harmless" can no
longer occur at all.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, NamedTuple, Optional

import _praxis
from _praxis import PraxisUnreachable  # re-exported so gates import one place  # noqa: F401

# The pure structural resumability probe (plan 003) lives in the src package. A bare hook subprocess
# only has ``hooks/`` on its path, so add the sibling ``src/`` before importing. The module is pure
# stdlib, so this pulls in no heavy dependency.
try:  # pragma: no cover - import plumbing
    from agent_factory.resumability import resumability_report
except ImportError:  # pragma: no cover - import plumbing
    import os as _os
    _SRC = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "src")
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    from agent_factory.resumability import resumability_report

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
M_MANUAL_REQUIREMENTS = "manual_requirements"   # subset of required ids whose verify=="manual"
M_REPORT_ONLY_REQUIREMENTS = "report_only_requirements"  # subset graded + recorded but NOT gating
M_PINNED_CHECKS = "pinned_checks"           # entries are synthesized VALIDATIONS (see module doc)
M_UNIVERSAL_CONTRACT = "universal_contract"  # the universal-lane requirement entries pinned at
                                              # coverage-contract time (R33) -- pin_validations() reads
                                              # this to AUTO-author their covering entries so a worker
                                              # never has to hand-write one.
M_AUTHORED_RUNS = "authored_runs"            # {validation_id: run} captured from the DECLARED check
                                              # facts at coverage-contract time. pin_validations()
                                              # overrides the worker's synthesized `run` with the
                                              # authored one wherever a check declares a concrete
                                              # command, so a check cannot be neutered at pin time.
M_GRADED_LOOP = "graded_loop"                # {validation_id: {iters, last_defects, last_hash}} (U6);
                                              # reset to {} on a fresh claim (R33) so a re-picked ticket
                                              # starts a fresh iteration budget.
M_RUN_OWNER = "run_owner"
M_RUN_AT = "run_at"
M_RUN_SCOPE = "run_scope"
M_FINISHED_AT = "finished_at"                # READ-ONLY here. The SERVER stamps/clears this off any
                                             # build_state write (ISO-8601 UTC); a client never writes
                                             # it — see knowledge/finished_at.py.
M_UNDER_SPECIFIED = "under_specified"   # [missing structural fields] — routed to intake, never claimed (plan 003)
M_PLANNING_OWNER = "planning_owner"         # session id of the active planning (intake) session (plan 002)
M_PLANNING_AT = "planning_at"               # epoch seconds, planning-marker heartbeat; stale => dead (plan 002)
M_BLESSED_AT = "blessed_at"                 # epoch seconds, stamped at bless; signals post-bless guard (S12)
M_PLAN_ATTEMPTS = "plan_attempts"           # int, failed bless attempts on current plan hash (S8 escalation)
M_PLAN_HASH = "plan_hash"                   # str, snapshot hash last attempt was recorded against (S8)
M_PLAN_BLOCKED_AT = "plan_blocked_at"       # float, epoch seconds; non-None means plan is terminally escalated (S8)

_LEASE_KEYS = (M_CLAIM_OWNER, M_CLAIM_AT, M_CLAIM_HEARTBEAT_AT, M_CLAIM_LEASE_TTL)
_RUN_KEYS = (M_RUN_OWNER, M_RUN_AT, M_RUN_SCOPE)
_PLANNING_KEYS = (M_PLANNING_OWNER, M_PLANNING_AT)

# A MANUAL requirement (``meta.verify == "manual"``) is one the executor MAY NOT self-check: its pass
# only counts when recorded via an external/human signal, never a worker-run command. These are the
# pass ``source`` values that count as such an external attestation; a worker-run pass defaults to
# ``WORKER_PASS_SOURCE`` and can NEVER satisfy a manual requirement (see :func:`all_validations_passed`).
WORKER_PASS_SOURCE = "worker"
HUMAN_PASS_SOURCES = frozenset({"human", "manual", "external"})

# The credential a caller must present in its execution environment to record an attested
# (human-class) pass. Without it, any source value in HUMAN_PASS_SOURCES is silently forced
# to WORKER_PASS_SOURCE — a build worker cannot obtain the attested path by naming it.
_ATTESTED_CALLER_ENV = "PRAXIS_ATTESTED_CALLER"


def _derive_effective_source(source: Optional[str]) -> str:
    """Derive the effective pass source from execution context, not a self-declared parameter.

    A build worker may NOT self-declare an attested/human source: without the
    ``PRAXIS_ATTESTED_CALLER`` credential, any human-class source is silently forced to
    ``WORKER_PASS_SOURCE``. Only a caller presenting the distinct credential (set by the
    execution environment, not by the worker itself) can record an attested pass.

    Non-human sources (e.g. ``"graded-judge"``) pass through unchanged — they never satisfy
    the manual gate in :func:`all_validations_passed`, so self-declaring one is harmless.
    """
    if source is None:
        return WORKER_PASS_SOURCE
    # The attestation credential must be present in the execution environment.
    if os.environ.get(_ATTESTED_CALLER_ENV):
        return str(source)
    # Without the credential, human-source values are refused — the worker cannot self-attest.
    if str(source) in HUMAN_PASS_SOURCES:
        return WORKER_PASS_SOURCE
    # Non-human sources pass through.
    return str(source)


def _ttl_env(name: str, default: int) -> int:
    """Read a TTL override from the environment, falling back to ``default``.

    Lease windows are workload-dependent: a plan whose tickets are long-form decision records needs a
    far wider window than one of small code edits. Without an override the only way to widen a lease
    is to edit this file, which is exactly the "edit the factory to make one project work" trap.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        sys.stderr.write(f"[af] WARNING: {name}={raw!r} is not an integer — using default {default}s\n")
        return default
    if val <= 0:
        sys.stderr.write(f"[af] WARNING: {name}={val} must be positive — using default {default}s\n")
        return default
    return val


# 15 min — per-ticket claim lease. Override with AF_LEASE_TTL_S for plans whose tickets legitimately
# run longer than the default window (see :func:`release` on what a lease takeover costs).
DEFAULT_LEASE_TTL_S = _ttl_env("AF_LEASE_TTL_S", 900)
DEFAULT_RUN_TTL_S = 3600     # 60 min — whole-set run marker; refreshed at each ticket boundary
DEFAULT_PLANNING_TTL_S = 3600  # 60 min — planning-session marker; refreshed by intake heartbeat

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


def normalize_tag(tag: Any) -> str:
    """Canonicalize ONE applicability tag so matching is not silently case/whitespace sensitive.

    The check↔ticket predicate is a server-side EXACT array-membership match: a check pins onto a
    ticket iff some value in ``check.meta.applies_to`` equals some value in ``ticket.meta.tags`` (∪
    ``meta.applies_to``). Exact means a ticket tag ``"Auth"`` would NOT match a check ``applies_to
    ["auth"]`` — the check would silently drop out of the coverage contract with no error. Normalizing
    BOTH sides (author time AND this resolve-time query) to ``strip().casefold()`` removes that
    footgun. ``"*"`` (the universal-wildcard lane) is preserved verbatim.

    This is the SINGLE canonical normalizer for the factory. Its mirror on the write path lives in
    ``knowledge/mcp/server.py:_normalize_applicability`` (the hook subprocess is stdlib-only and cannot
    import the MCP package, so the two are kept identical by ``test_check_resolution_lanes`` /
    ``test_org_and_tag_normalization`` asserting they agree). Keep them in lockstep.
    """
    s = str(tag).strip()
    return s if s == "*" else s.casefold()


def _req_verify(req: Any) -> str:
    """The ``verify`` mode ("automated" | "manual" | "") declared on a requirement fact/dict."""
    if not isinstance(req, dict):
        return ""
    return str((req.get("meta") or {}).get("verify") or req.get("verify") or "").strip().casefold()


def _req_report_only(req: Any) -> bool:
    """True iff the requirement is a REPORT-ONLY universal — graded + recorded, but non-gating."""
    if not isinstance(req, dict):
        return False
    return bool((req.get("meta") or {}).get("report_only") or req.get("report_only"))


def _check_id(check: Any) -> str:
    if isinstance(check, str):
        return check
    return str((check or {}).get("id") or (check or {}).get("check_id") or "")


def _scope_of(check: Any) -> str:
    """A check's scope ("planning" | "validation" | ...), from the top-level column or meta."""
    if not isinstance(check, dict):
        return ""
    return str(check.get("scope") or (check.get("meta") or {}).get("scope") or "")


def _run_key(check: Any) -> str:
    """The executable identity of a check: its ``meta.run`` command, whitespace-normalized.

    Empty for a check that carries no command (a graded/rubric entry). Two checks sharing a non-empty
    key are the SAME work — running both costs the same minutes twice and proves nothing extra.
    """
    if not isinstance(check, dict):
        return ""
    meta = check.get("meta") or {}
    run = str(meta.get("run") or check.get("run") or "")
    return " ".join(run.split())


def _is_wildcard(check: Any) -> bool:
    """True iff the check is authored ``applies_to:["*"]`` — the universal lane."""
    if not isinstance(check, dict):
        return False
    return any(normalize_tag(t) == "*" for t in _as_list((check.get("meta") or {}).get("applies_to")))


def _declared_scope_globs(check: Any) -> list[str]:
    """Explicit path predicate authored on the check (``meta.when_changed``), if any."""
    if not isinstance(check, dict):
        return []
    meta = check.get("meta") or {}
    return [str(g).strip() for g in _as_list(meta.get("when_changed") or meta.get("when_paths"))
            if str(g).strip()]


_MODULE_HINTS = (
    re.compile(r"--prefix[=\s]+([\w.\-/]+)"),          # npm --prefix backend run test
    re.compile(r"(?:^|[;&|]\s*)cd\s+([\w.\-/]+)"),     # cd frontend && npm test
    re.compile(r"-C\s+([\w.\-/]+)"),                   # make -C service-a
    re.compile(r"(?:^|\s)--(?:cwd|dir|project-dir)[=\s]+([\w.\-/]+)"),
)


def infer_module_roots(check: Any) -> list[str]:
    """The module directories a check's command actually operates in, read off the command itself.

    A monorepo command names its own workspace — ``npm --prefix backend test``, ``cd frontend && ...``,
    ``make -C service-a`` — so the scope of a check is usually derivable without anyone authoring it.
    Returns [] when the command names no module (a repo-wide command like ``npx knip`` or a bare grep),
    which the caller must treat as "always applicable" rather than "applies to nothing".
    """
    run = _run_key(check)
    if not run:
        return []
    roots: list[str] = []
    for pat in _MODULE_HINTS:
        for m in pat.finditer(run):
            root = m.group(1).strip("/. ")
            if root and root not in (".", "..") and not root.startswith("-") and root not in roots:
                roots.append(root)
    return roots


def check_scope_globs(check: Any, *, infer: bool = True) -> list[str]:
    """The path predicate for a check: authored ``meta.when_changed`` if present, else inferred from
    the command's own module roots. Empty means unscoped — the check runs for every change."""
    declared = _declared_scope_globs(check)
    if declared:
        return declared
    return [f"{root}/**" for root in infer_module_roots(check)] if infer else []


def _path_matches(path: str, glob: str) -> bool:
    """``fnmatch`` with ``**`` meaning "this directory and everything under it"."""
    path = str(path).strip().lstrip("./")
    if glob.endswith("/**"):
        root = glob[:-3].strip("/")
        return path == root or path.startswith(root + "/")
    return fnmatch.fnmatch(path, glob)


def scope_checks_to_changes(checks: list, changed_paths: Any, *, infer: bool = True) -> tuple[list, list]:
    """Split a resolved check set into (RUN, SKIP) for a diff — so an edit confined to one module does
    not pay for every other module's suite.

    The lever this exists for: a universal ``npm --prefix backend test`` gate on a monorepo makes a
    frontend-only ticket run the entire backend suite to prove nothing about its own change. Scoping
    that to ``backend/**`` is the single biggest wall-clock win available to a build loop.

    Three deliberate fail-SAFE rules, because a silently skipped gate is worse than a slow one:

    1. An UNSCOPED check (no ``when_changed``, no inferable module root — e.g. ``npx knip``, a
       repo-wide grep) always runs. Absence of a predicate never means "applies to nothing".
    2. An UNKNOWN diff (``changed_paths`` empty or None) runs everything. We skip on evidence, never
       on the absence of it.
    3. A change OUTSIDE every module root the check set knows about — a root ``package.json``, CI
       config, a shared/ directory — runs everything. Cross-cutting edits are exactly the ones whose
       blast radius is not confined to the module they were typed in.

    Returns ``(to_run, skipped)``; each skipped check is annotated with ``meta.skipped_reason`` so the
    completion record shows a SKIP, never a silent pass.
    """
    checks = list(checks)
    paths = [str(p).strip().lstrip("./") for p in _as_list(changed_paths) if str(p).strip()]
    if not paths:
        return checks, []  # rule 2 — unknown diff, run everything

    scoped = [(chk, check_scope_globs(chk, infer=infer)) for chk in checks]
    known_roots = {g[:-3].strip("/") for _, globs in scoped for g in globs if g.endswith("/**")}
    if any(not any(p == r or p.startswith(r + "/") for r in known_roots) for p in paths):
        return checks, []  # rule 3 — a change outside every known module; blast radius is unbounded

    to_run, skipped = [], []
    for chk, globs in scoped:
        if not globs or any(_path_matches(p, g) for p in paths for g in globs):
            to_run.append(chk)  # rule 1 covers the `not globs` half
            continue
        chk = dict(chk)
        chk["meta"] = dict(chk.get("meta") or {})
        chk["meta"]["skipped_reason"] = (
            f"no changed path matches {globs} — this ticket edits none of the module(s) it covers")
        skipped.append(chk)
    return to_run, skipped


def collapse_duplicate_runs(checks: list) -> list:
    """Collapse checks whose ``meta.run`` is byte-identical (after whitespace normalization) down to
    ONE, so a ticket never executes the same command twice.

    This is the generic form of a fix every project otherwise has to make by hand: a plan accumulates
    a universal gate (``npm run build && npm test`` on ``applies_to:["*"]``) plus older lane-scoped
    checks that happen to run the exact same command, and every ticket then pays that suite two or
    three times over. Nothing is proven by the repeats — identical command, identical exit code.

    Applied per PARTITION by the callers (gating set and candidate pool separately), never across the
    boundary: collapsing a gating check into a non-gating one would silently drop a gate.

    The survivor is deterministic — the broadest applicability wins (a ``["*"]`` universal subsumes a
    lane-scoped duplicate), ties broken by check id. It carries ``meta.collapsed_duplicates`` listing
    the ids it stands in for, so the coverage contract records what was folded in rather than losing
    it silently. Checks with no ``run`` are never collapsed: a graded entry's identity is its text.
    """
    by_run: dict[str, list] = {}
    out: list = []
    for chk in checks:
        key = _run_key(chk)
        if not key:
            out.append(chk)  # no command — identity is the text, never a duplicate
            continue
        by_run.setdefault(key, []).append(chk)

    for group in by_run.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        winner, *losers = sorted(group, key=lambda c: (not _is_wildcard(c), _check_id(c)))
        # Shallow-copy so the annotation never mutates the caller's/cache's fact dict.
        winner = dict(winner)
        winner["meta"] = dict(winner.get("meta") or {})
        winner["meta"]["collapsed_duplicates"] = [cid for c in losers if (cid := _check_id(c))]
        out.append(winner)
    return out


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

    THE MATCHING MODEL (unambiguous): the applicability PREDICATE lives on the CHECK
    (``check.meta.applies_to``, a list); the IDENTITY it matches against lives on the TICKET
    (``ticket.meta.tags``, with ``ticket.meta.applies_to`` as a lenient fallback). A check pins onto a
    ticket iff ``set(normalize_tag(x) for x in check.meta.applies_to)`` intersects
    ``set(normalize_tag(x) for x in ticket.meta.tags ∪ ticket.meta.applies_to)``, plus the separate
    ``"*"`` and surface lanes below. Both sides are normalized (:func:`normalize_tag`, matching the
    author-time write path) so ``"Auth"`` vs ``"auth"`` can never silently drop a check.

    Tag union surface (the per-ticket lanes):
      * TAG match — for each IDENTITY tag on the ticket (meta.tags, or meta.applies_to as fallback),
        enumerate active ``check`` facts whose PREDICATE ``meta.applies_to`` contains that tag
        (server-side array-membership on the normalized value).
      * ``"*"`` WILDCARD — a SEPARATE lane (below): universal checks authored ``applies_to:["*"]``
        apply to EVERY ticket, incl. a tag-less one. It is separate because a per-tag query can never
        surface a ``["*"]`` check — the ticket's concrete tags never include the literal ``"*"``.
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

    # Per-ticket resolution (tag ∪ "*" ∪ surface, scope-filtered), then split by the CANDIDATE flag
    # (U1): GATING checks (candidate:false / absent) form the coverage contract; candidate:true
    # entries are the NON-GATING shared pool, returned separately by :func:`pool_candidates`.
    seen = _matching_checks(ticket, project, scope, space, snapshot)
    return collapse_duplicate_runs([v for v in seen.values() if not _is_candidate(v)])


def _is_candidate(chk: Any) -> bool:
    """True iff the check is a non-gating pool entry (``meta.candidate`` truthy)."""
    if not isinstance(chk, dict):
        return False
    return bool((chk.get("meta") or {}).get("candidate"))


def _matching_checks(ticket: Any, project: str, scope: str,
                     space: Optional[str], snapshot: Optional[str]) -> dict[str, dict]:
    """The tag ∪ ``"*"`` ∪ surface resolution, scope-filtered — the shared body behind both the
    gating resolve and the candidate pool. Returns ``{check_id: check}`` BEFORE the candidate split,
    so callers can partition it however they need. No candidate/gating decision is made here.
    """
    meta = _meta(ticket, project_ref(project).plan if project else None)
    seen: dict[str, dict] = {}

    # IDENTITY lives on the TICKET (``meta.tags``, with ``meta.applies_to`` as a lenient fallback);
    # the PREDICATE lives on the CHECK (``meta.applies_to``). Both sides are normalized
    # (:func:`normalize_tag`, matching the write path) so ``"Auth"`` and ``"auth"`` are the same tag.
    tags = _as_list(meta.get("tags")) + _as_list(meta.get("applies_to"))
    for tag in {normalize_tag(t) for t in tags if t and normalize_tag(t)}:
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
    return seen


def pool_candidates(ticket: Any, project: str = "", scope: str = "validation",
                    override: Optional[tuple[str, str]] = None) -> list[dict]:
    """The DETERMINISTIC candidate lane (U1): every ``candidate:true`` check in ``building-validation``
    that resolves onto this ticket (tag ∪ ``"*"`` ∪ surface), returned in full — NOT a top-k sample
    like :func:`retrieve_advisory_checks`. These are non-gating; they are the input the build-time
    rubric assembler (U5) tiers into promoted gating validations + one advisory aggregate. The gating
    resolve (:func:`resolve_validation_requirements`) excludes exactly this set, so a check is either
    a gate or a candidate, never both.
    """
    space, snapshot = _checks_target(project, scope, override)
    seen = _matching_checks(ticket, project, scope, space, snapshot)
    return collapse_duplicate_runs([v for v in seen.values() if _is_candidate(v)])


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

    Written through the sanctioned build-state route, not ``patch_meta``: a blessed plan refuses
    candidate edits, and a pin refused there is the exact failure that leaves a ticket with an
    EMPTY ``pinned_checks`` — which then reads as "RESOLVE never ran" and (before the finish guard)
    let the ticket self-certify. Ordering matters for the same reason: this pin must land BEFORE any
    finish, or the server's finish guard refuses the release.
    """
    req_ids = [rid for rid in (_check_id(r) for r in requirements) if rid]
    # A MANUAL requirement (verify=="manual") is recorded separately so completion can require an
    # external/human-sourced pass for it — the worker may not self-certify it (see all_validations_passed).
    manual_ids = [rid for r in requirements
                  if (rid := _check_id(r)) and _req_verify(r) == "manual"]
    # A REPORT-ONLY requirement (a report-only universal, meta.report_only) is graded + recorded but
    # excluded from the completion gate — recorded separately so all_validations_passed can skip it.
    report_only_ids = [rid for r in requirements
                       if (rid := _check_id(r)) and _req_report_only(r)]
    # The UNIVERSAL-lane entries (see :func:`universal_requirements`) carry their own frozen rubric
    # and need no worker authorship -- stash them so :func:`pin_validations` can auto-append their
    # covering entries every time it (re)pins, instead of requiring the worker to hand-author one.
    universal_entries = [r for r in requirements
                         if isinstance(r, dict) and (r.get("meta") or {}).get("universal")]
    # AUTHORED RUNS. A check that declares a concrete ``meta.run`` states the command that proves it;
    # the worker may not restate it. Capture those commands here, keyed by the same id the pinned
    # entry carries, so :func:`pin_validations` can override a synthesized ``run`` with the authored
    # one. Without this the worker's ``run`` was taken verbatim, so a declared check could be pinned
    # under its own id with an unrelated (invariably weaker) command and recorded as passing it —
    # observed live: a 3800-char check asserting byte floors and layer ids was pinned as a 171-char
    # "start the server and curl one route", and the ticket finished green over a stub UI.
    return _praxis.write_build_state(cid, {
        M_REQUIRED_VALIDATIONS: req_ids,
        M_MANUAL_REQUIREMENTS: manual_ids,
        M_REPORT_ONLY_REQUIREMENTS: report_only_ids,
        M_PINNED_CHECKS: [],
        M_UNIVERSAL_CONTRACT: universal_entries,
    }, **_ref_kw(ref))


def _norm_validation(v: Any, idx: int) -> dict:
    """Normalize one worker-authored validation into the pinned entry shape.

    Accepts a dict with ``covers`` (req id or list), ``run`` (command), and optional ``validation_id``.
    A missing id is synthesized stably from its covered requirements + index so passes can be recorded.

    A GRADED validation (``kind="graded"``) additionally carries its FROZEN ``rubric`` dict — pinned
    here at synthesis time and read back verbatim at VERIFY (U6 frozen-rubric guard), so a later edit
    to the seeded library can never move the target under an in-progress ticket. Binary validations
    omit ``kind``/``rubric`` and stay byte-compatible with the pre-graded entry shape; the gate
    (:func:`all_validations_passed`) only ever reads ``passed``, so graded extras are inert to it.
    """
    if not isinstance(v, dict):
        v = {"run": str(v)}
    covers = [str(c) for c in _as_list(v.get("covers") or v.get("requirement_id")
                                       or v.get("covers_requirement")) if c]
    vid = str(v.get("validation_id") or v.get("id") or "").strip()
    if not vid:
        base = "+".join(covers) if covers else "validation"
        vid = f"{base}#{idx}"
    entry = {
        "validation_id": vid,
        "covers": covers,
        "run": str(v.get("run") or v.get("command") or ""),
        "passed": None,
        "ran_at": None,
    }
    kind = str(v.get("kind") or "").strip().casefold()
    if kind == "graded":
        entry["kind"] = "graded"
        if isinstance(v.get("rubric"), dict):
            entry["rubric"] = v["rubric"]  # frozen at pin time; VERIFY reads this copy
        src = str(v.get("source_check_id") or "").strip()
        if src:  # links back to the seeded library check, for the U7 auto-adjust in-flight guard
            entry["source_check_id"] = src
    return entry


def _universal_covering_entries(meta: dict, already_covered: set[str]) -> list[dict]:
    """Auto-author the covering PINNED entries for every universal-lane requirement stashed by
    :func:`pin_requirements` (``meta.universal_contract``) that ``already_covered`` does not yet
    cover — the R33 "coverage authoring" guarantee: a non-exempt ticket reaches
    :func:`all_validations_passed` with no hand-authored covering validation for the universal
    lane, because the lane covers itself."""
    out: list[dict] = []
    for u in (meta.get(M_UNIVERSAL_CONTRACT) or []):
        uid = _check_id(u)
        if not uid or uid in already_covered:
            continue
        umeta = dict(u.get("meta") or {})
        entry: dict = {
            "validation_id": uid,
            "covers": [uid],
            "run": "",
            "passed": None,
            "ran_at": None,
            "kind": "graded",
            "universal": True,
        }
        if isinstance(umeta.get("rubric"), dict):
            entry["rubric"] = umeta["rubric"]
        src = str(umeta.get("source_check_id") or "").strip()
        if src:
            entry["source_check_id"] = src
        out.append(entry)
        already_covered.add(uid)
    return out


_CD_PREFIX = re.compile(r"^\s*cd\s+[^\s&;|]+\s*&&\s*")


def _same_command(a: str, b: str) -> bool:
    """Whether two pinned commands are the same gate.

    Exact text, except for a leading ``cd <path> &&``. Every ticket builds in its own worktree, so a
    worker legitimately prefixes the authored command to run it there — observed on R29 as
    ``cd /workspace/.../worktrees/agent-a224da98e3ea29e88 && <the authored command verbatim>``.
    Treating that as a substitution would reject honest work on every ticket, which is a worse
    failure than the one this guard exists to stop.

    Only the leading prefix is forgiven. Anything the worker appends, removes, or rewrites inside
    the command still counts as a different gate — that is where the real substitutions lived
    (a 3800-char check pinned as 171, a lint gate cut from 166 to 74).
    """
    return _CD_PREFIX.sub("", a or "").strip() == _CD_PREFIX.sub("", b or "").strip()


def _declared_runs(ref: Optional[tuple[str, str]] = None) -> dict[str, str]:
    """``{check fact id: authored run}`` for every check declaring a concrete command in this
    project's ``building-validation`` snapshot.

    Read LIVE rather than copied onto the ticket. An earlier version stashed this map in ticket meta
    at pin time, which silently did nothing: ``write_build_state`` only accepts the server's
    ``BUILD_STATE_META_KEYS``, so an unregistered key is dropped in transit — the write returns
    success and the field never lands. Reading the checks directly needs no new key, no server
    change, and compares against the check as authored right now.
    """
    space = ""
    if ref and ref[0]:
        space = str(ref[0])
    if not space:
        space = os.environ.get("FACTORY_PROJECT", "") or ""
    if not space:
        return {}
    # A minimal fake client (the sanctioned-routes test injects one) has no reader — degrade to "no
    # declared commands" for that SHAPE only. A real client that raises is left to propagate: a
    # lookup failure must surface, never quietly re-open the substitution hole this closes.
    reader = getattr(_praxis, "facts_by", None)
    if reader is None:
        return {}
    out: dict[str, str] = {}
    for c in (reader(category="check", space=space,
                     snapshot="building-validation") or []):
        cid = c.get("id") or c.get("cid")
        run = str(((c.get("meta") or {}).get("run")) or "").strip()
        if cid and run:
            out[str(cid)] = run
    return out


def _apply_authored_runs(pinned: list[dict], authored: dict) -> list[dict]:
    """Make a DECLARED check's own command authoritative over the worker's synthesized one.

    ``pin_requirements`` stashes ``{validation_id: run}`` for every resolved check that declares a
    concrete ``meta.run`` (:data:`M_AUTHORED_RUNS`). A pinned entry claiming one of those ids gets the
    authored command written back over whatever the worker supplied, and is stamped
    ``run_source="authored"``; entries with no authored counterpart keep the worker's command and are
    stamped ``run_source="worker"``, so the two are distinguishable in the record afterwards.

    Worker authorship remains the path for everything a check does NOT spell out — the acceptance
    floor (``<cid>::acceptance``), graded/rubric checks (which carry a frozen ``rubric`` and no
    ``run``), and any check that deliberately leaves the command to the builder. This narrows
    authorship to exactly the commands nobody declared, rather than removing it.

    Why this exists: the pin path took ``run`` verbatim from the worker, so a check could be pinned
    under its own ``validation_id`` carrying a command that tested something else entirely, pass, and
    finish the ticket. Measured on a live plan, 7 of 20 pinned checks did not match their stored
    definition — including a 3800-char UI check pinned as 171 chars and a lint/typecheck gate cut from
    166 to 74. Every one of those substitutions was weaker than the check it displaced.
    """
    if not isinstance(authored, dict):
        authored = {}
    for entry in pinned:
        # Key on what the entry COVERS, not on its own id. A worker pins under an id it invents
        # (`v-static-hygiene`) while declaring `covers: [<real check fact id>]`, so matching on
        # validation_id misses every substitution — which is exactly how a 166-char lint gate was
        # pinned as 74 chars under a `v-` alias and recorded green.
        hits = [authored[c] for c in (str(x) for x in (entry.get("covers") or []))
                if str(authored.get(c) or "").strip()]
        if len(hits) == 1:
            entry["run"] = hits[0]
            entry["run_source"] = "authored"
        elif len(hits) > 1:
            # One entry claiming to cover two checks that each declare their own command cannot
            # honour both. Leave it alone; the gate refuses it.
            entry.setdefault("run_source", "ambiguous")
        else:
            entry.setdefault("run_source", "worker")
    return pinned


def pin_validations(cid: str, validations: list,
                    ref: Optional[tuple[str, str]] = None) -> dict:
    """Pin the worker-SYNTHESIZED concrete validations onto the ticket (the eval).

    Each entry: ``{validation_id, covers:[req_id,...], run, passed=None, ran_at=None}``. Because the
    build-state write replaces ``pinned_checks`` wholesale, this TRUNCATES any prior validation state —
    the new set is THIS pass's eval. Does NOT touch ``required_validations`` (the coverage contract
    set at start): coverage is asserted at finish via :func:`coverage_gap` / :func:`all_validations_passed`.

    R33: after the worker's own list is normalized, any UNIVERSAL-lane requirement
    (``meta.universal_contract``, stashed by :func:`pin_requirements`) not already covered by the
    worker's own entries gets its own auto-authored covering entry appended here — carrying its
    frozen rubric, exactly like a worker-pinned graded check — so the worker never has to hand-write
    one and the universal lane can never deadlock coverage. Reading the ticket's current meta for
    this is best-effort: a caller exercising ONLY the pin-shape contract with a minimal fake (no
    ``get_fact``) degrades to no auto-injection rather than raising — the universal lane is additive
    sugar on top of the core pin path, never a hard dependency of it.
    """
    pinned = [_norm_validation(v, i) for i, v in enumerate(validations)]
    pinned = [p for p in pinned if p["validation_id"]]
    try:
        meta = _meta(cid, ref)
    except Exception:  # noqa: BLE001 - best-effort; see docstring
        meta = {}
    _apply_authored_runs(pinned, _declared_runs(ref))
    covered = {c for p in pinned for c in (p.get("covers") or [])}
    pinned.extend(_universal_covering_entries(meta, covered))
    return _praxis.write_build_state(cid, {M_PINNED_CHECKS: pinned}, **_ref_kw(ref))


def record_validation_pass(cid: str, validation_id: str, passed: bool,
                           ran_at: Optional[float] = None,
                           source: Optional[str] = None,
                           verdict: Optional[dict] = None,
                           ref: Optional[tuple[str, str]] = None) -> dict:
    """Record one validation's pass/fail ON THE TICKET NODE (never on the requirement fact).

    Read-modify-write of ``meta.pinned_checks``: update the matching validation's passed/ran_at/source.
    If the validation is not already pinned (set drifted), it is appended so the result is not lost.

    The effective ``source`` is DERIVED from the execution context via
    :func:`_derive_effective_source`, NOT from a self-declared parameter. A build worker passing
    ``source="human"`` without the ``PRAXIS_ATTESTED_CALLER`` credential is silently forced to
    ``WORKER_PASS_SOURCE`` — the worker cannot obtain the attested path by naming it. Only a caller
    presenting the distinct credential records an attested pass.

    When ``source`` is ``None``, the effective source defaults to ``WORKER_PASS_SOURCE``.

    IMPLICIT HEARTBEAT. Recording a validation result IS proof of liveness, so this bumps
    ``claim_heartbeat_at`` in the same write whenever the ticket holds a live claim. :func:`heartbeat`
    exists but relies on the build agent remembering to call it on a timer, which it does not reliably
    do — leaving long tickets to expire their own lease mid-work and be handed out twice. Piggybacking
    on a write the worker already makes costs no extra round-trip and cannot be forgotten.
    """
    effective_source = _derive_effective_source(source)
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
            entry["source"] = effective_source
            if verdict is not None:  # graded checks stash the cached verdict (incl. code_hash)
                entry["verdict"] = verdict
            found = True
            break
    if not found:
        appended = {"validation_id": str(validation_id), "covers": [],
                    "run": "", "passed": bool(passed), "ran_at": ran_at,
                    "source": effective_source}
        if verdict is not None:
            appended["verdict"] = verdict
        pinned.append(appended)
    patch: dict[str, Any] = {M_PINNED_CHECKS: pinned}
    if meta.get(M_CLAIM_OWNER) and meta.get(M_BUILD_STATE) == "in_progress":
        patch[M_CLAIM_HEARTBEAT_AT] = time.time()  # implicit heartbeat — see docstring
    return _praxis.write_build_state(cid, patch, **_ref_kw(ref))


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


def _covers_only(entry: dict, ids: set[str]) -> bool:
    """True iff the pinned validation covers at least one requirement and EVERY one it covers is in
    ``ids`` (used to spot a validation that exists solely to record a report-only verdict)."""
    covered = {str(c) for c in (entry.get("covers") or [])}
    return bool(covered) and covered <= ids


def all_validations_passed(ticket: Any, ref: Optional[tuple[str, str]] = None) -> bool:
    """True IFF the ticket is genuinely done: it has a coverage contract (>=1 required requirement),
    every required requirement is covered by some pinned validation (no coverage gap), there is at
    least one pinned validation, and EVERY pinned validation passed.

    A ticket with no resolved requirements returns False — it cannot self-certify "no requirements
    therefore done"; that is a BLOCK condition (use :func:`block`), surfaced for owner action, never
    a silent pass. (An intentionally validation-free ticket must carry an explicit always-pass
    requirement, authored upstream.)

    MANUAL requirements are held to a stricter bar: a ``verify="manual"`` requirement (recorded in
    ``meta.manual_requirements``) is satisfied ONLY when some covering validation passed with a
    human/external ``source`` (:data:`HUMAN_PASS_SOURCES`). A worker-run pass (the default source)
    never counts — so a manual ticket can never reach True from worker-authored validations alone.

    REPORT-ONLY requirements (``meta.report_only_requirements`` — the report-only universal lane) are
    EXCLUDED from the gate: they need no coverage and no passing validation, and a validation that
    exists solely to record one is ignored (a failing report-only verdict never blocks). This is the
    calibration rollout knob — flip a universal's ``report_only`` off (drop it from this set) to gate.
    """
    meta = _meta(ticket, ref)
    report_only = {str(r) for r in (meta.get(M_REPORT_ONLY_REQUIREMENTS) or []) if r}
    required = {str(r) for r in (meta.get(M_REQUIRED_VALIDATIONS) or []) if r} - report_only
    pinned = list(meta.get(M_PINNED_CHECKS) or [])
    if not required or not pinned:
        return False
    covered: set[str] = set()
    for entry in pinned:
        for c in (entry.get("covers") or []):
            covered.add(str(c))
    if not required.issubset(covered):   # coverage gap — compute inline (meta already extracted)
        return False
    # A pinned validation that covers ONLY report-only requirements is not gating — its pass/fail is
    # recorded (calibration) but never blocks completion.
    gating_pinned = [e for e in pinned if not _covers_only(e, report_only)]
    if not all(bool(e.get("passed")) for e in gating_pinned):
        return False
    # A DECLARED check's own command is the only thing that can satisfy it. Coverage alone is not
    # enough: the gate used to accept any entry naming the check in `covers`, whatever command it
    # actually ran, so a worker could cover a 3800-char check with `curl /runs`, or with the EMPTY
    # command `record_validation_pass` appends for an unpinned id, and finish green. Observed live:
    # a bucket-creation ticket finished having created no bucket, with six entries carrying run="".
    declared = _declared_runs(ref)
    for rid in required:
        want = str(declared.get(rid) or "").strip()
        if not want:
            continue  # no authored command (acceptance floor, graded rubric) — worker authorship stands
        if not any(
            bool(e.get("passed"))
            and _same_command(e.get("run") or "", want)
            and rid in {str(c) for c in (e.get("covers") or [])}
            for e in pinned
        ):
            return False
    # Manual requirements need an EXTERNAL/human-sourced pass — the worker may not self-certify them.
    manual = {str(r) for r in (meta.get(M_MANUAL_REQUIREMENTS) or []) if r}
    for req in manual:
        if not any(
            bool(e.get("passed"))
            and str(e.get("source") or WORKER_PASS_SOURCE) in HUMAN_PASS_SOURCES
            and req in {str(c) for c in (e.get("covers") or [])}
            for e in pinned
        ):
            return False
    return True


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

    The grant is made by the SERVER (``POST /requirements/{cid}/claim``), which applies the same
    rule as one conditional UPDATE: granted iff the ticket is not in_progress, OR ``owner`` already
    holds it (idempotent renew), OR the existing lease is STALE (auto-reclaim a dead agent). It
    stamps claim_owner/claim_at/claim_heartbeat_at/claim_lease_ttl and build_state=in_progress.
    Returns True if we now hold the lease, False if a DIFFERENT owner holds a LIVE lease, or the
    ticket is terminally ``blocked`` (needs owner action, not a build claim).

    WHY NOT ``patch_meta``. Two reasons, and the first is fatal. (1) A blessed ``prd-<project>``
    plan REFUSES candidate edits (the S12 bless guard), so the read-modify-write this used to be
    failed closed on every ticket of every blessed plan — the loop dispatched work that no worker
    could claim, and no worktree or branch was ever created. Build state is not plan content, so it
    goes through the sanctioned, unguarded route instead of unblessing the plan to build it.
    (2) The server's grant is ATOMIC at the row level, so the double-claim this function used to
    accept as "rare and harmless" cannot happen at all: two agents racing a free ticket produce
    exactly one 200 and one 409.

    R33: a ticket that was NOT already held under a live lease by ``owner`` (a genuine re-pick — free,
    stale-reclaimed, or newly assigned to a different owner) gets its graded-check iteration budget
    (``meta.graded_loop``) reset to empty. Without this, a ticket re-picked after a stale lease/owner
    change would inherit a prior session's iteration count and could trip the escalation cap on its
    very first fresh attempt; an idempotent renew by the SAME live owner leaves it untouched. That
    reset is a SEPARATE build-state write because it is factory policy, not lease mechanics — the
    claim endpoint has no business knowing about graded loops.
    """
    meta = _meta(cid, ref)
    if meta.get(M_BUILD_STATE) == "blocked":
        return False  # blocked needs owner action (af-intake-plan amend / accept), not a build claim
    fresh_pick = not (_lease_live(meta) and meta.get(M_CLAIM_OWNER) == owner)
    if _praxis.claim_requirement(cid, owner, int(ttl), **_ref_kw(ref)) is None:
        return False  # a different owner holds a live lease (409)
    if fresh_pick:
        _praxis.write_build_state(cid, {M_GRADED_LOOP: {}}, owner=owner, **_ref_kw(ref))
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
    # The build-state route rather than the dedicated /heartbeat endpoint: this write bumps the
    # WHOLE-SET run marker in the same round-trip, and /heartbeat only knows about the per-ticket
    # lease. Splitting it would cost an extra call and let the two drift apart mid-run.
    _praxis.write_build_state(cid, patch, owner=owner, **_ref_kw(ref))
    return True


def uncommitted_changes(cwd: Optional[str] = None) -> str:
    """Return the porcelain status of the git worktree at ``cwd`` — ``""`` when clean.

    POSITIVE EVIDENCE ONLY: anything that means "cannot tell" (git missing, not a repo, command
    error/timeout) returns ``""`` (treated as clean), because a worker legitimately running outside a
    git worktree must still be able to finish. We only ever block on a definite dirty answer.
    """
    try:
        p = subprocess.run(["git", "status", "--porcelain"], cwd=cwd or os.getcwd(),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:  # not a repo, or git unhappy — cannot tell, so do not block
        return ""
    return (p.stdout or "").strip()


def release(cid: str, owner: str, state: str,
            ref: Optional[tuple[str, str]] = None) -> bool:
    """Release ``owner``'s lease and set a terminal build_state ("finished" or "incomplete").

    The SERVER performs the write (``POST /requirements/{cid}/release``) — the sanctioned,
    unguarded build-state route, because a blessed ``prd-<project>`` plan refuses candidate edits
    (the S12 bless guard) and a build must not have to unbless the plan to record that it finished.

    It drops the lease keys (so nothing dangles) and stamps build_state, MERGING so identity keys
    (tags/surfaces/required_validations/pinned_checks) survive. On ``finished`` the run marker is
    also cleared (the ticket has left the active run) and the server stamps ``finished_at``. On
    ``incomplete`` the run marker is KEPT so the whole-set gate keeps the ticket in scope and forces
    it to be re-done (a clean yield does not end the run).

    A ``finished`` release is REFUSED by the server when the ticket has no pinned checks and no
    ``meta.checks_waived_reason`` — a ticket nothing gates would certify itself. So RESOLVE's
    :func:`pin_requirements` / :func:`pin_validations` must have landed before this call; that
    surfaces here as a raised :class:`PraxisUnreachable` (HTTP 400), loudly, rather than a silent
    self-certification.

    LEASE TAKEOVER — the two states are deliberately NOT symmetric on an owner mismatch:

    - ``finished`` is HONORED even when the lease was taken over, and warns loudly. Completion is a
      fact about the world — the work is built and its checks passed — not a fact about who holds a
      lease. The old behavior (refuse, return False, write nothing) meant a worker whose lease expired
      mid-ticket had its FINISHED work silently discarded: the ticket stayed incomplete, was handed to
      another agent, was rebuilt, and that agent's finish raced the same way. That is an unbounded
      rebuild loop which burns a run's whole budget while converging on nothing, and it is invisible
      because nothing is ever recorded. Honoring the completion terminates the loop; a later duplicate
      finish from the new owner is idempotent.
    - ``incomplete`` still REFUSES on mismatch (returns False, warns). A yield is a claim about the
      CURRENT attempt, so a stale owner must never regress a ticket another agent is actively building.
    """
    if state not in ("finished", "incomplete"):
        raise ValueError("state must be 'finished' or 'incomplete'")
    if state == "finished" and os.environ.get("AF_ALLOW_DIRTY_FINISH") != "1":
        dirty = uncommitted_changes()
        if dirty:
            rid = _meta(cid, ref).get("requirement_id") or cid
            sys.stderr.write(
                f"[af-build] REFUSING to finish {rid}: the worktree has uncommitted changes. A worker "
                f"builds in an ISOLATED worktree and the orchestrator integrates by MERGING its branch, "
                f"so uncommitted work is invisible to that merge — it is either lost or swept into an "
                f"unreviewed WIP commit that never passed this ticket's evals. Commit on your own branch "
                f"first (SKILL §8 step 7), then release. Set AF_ALLOW_DIRTY_FINISH=1 only when you have a "
                f"deliberate reason to finish dirty.\n--- git status --porcelain ---\n{dirty}\n"
            )
            return False
    meta = _meta(cid, ref)
    held_by = meta.get(M_CLAIM_OWNER)
    if held_by not in (owner, None):
        rid = meta.get("requirement_id") or cid
        if state != "finished":
            sys.stderr.write(
                f"[af-build] LEASE LOST: {rid} yielded incomplete by {owner!r} but the lease is held "
                f"by {held_by!r} — refusing to regress a ticket another agent is building. This "
                f"attempt's work is dropped; the holder's attempt continues.\n"
            )
            return False
        sys.stderr.write(
            f"[af-build] LEASE TAKEOVER: {rid} finished by {owner!r} but the lease is now held by "
            f"{held_by!r} — HONORING the completion (the work is built and its checks passed). The "
            f"lease expired while this ticket was still being worked, so it was handed out twice. "
            f"Widen AF_LEASE_TTL_S if this recurs.\n"
        )
    # The lease keys and (on finish) the run marker are dropped SERVER-side, in the same statement
    # as the build_state stamp, so no reader observes the half-applied middle. ``honor_takeover``
    # carries the asymmetry above across the wire: a finish survives a takeover, a yield does not
    # (and a yield by a non-holder was already refused above, so it never reaches here).
    # NOTE: no finished_at here. The SERVER dates a completion off this build_state write (it owns
    # the clock); a client stamping one only invents a second producer to disagree with it. See the
    # M_FINISHED_AT comment above.
    released = _praxis.release_requirement(cid, owner, state,
                                           honor_takeover=(state == "finished"),
                                           **_ref_kw(ref))
    if released is None:  # 409 — the lease moved on under a yield; nothing was written
        return False
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
    # "blocked" is the ONE build_state transition the build-state route makes (claim / release /
    # regress own the others, so their guards cannot be bypassed) — a block is pure build state:
    # it records what the loop learned, and changes nothing the plan says.
    _praxis.write_build_state(cid, patch, **_ref_kw(ref))
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
        # No ``owner=`` here: the run marker is stamped over the whole in-scope set BEFORE any of
        # those tickets is claimed, so the stamper holds no lease on any of them.
        _praxis.write_build_state(str(cid),
                                  {M_RUN_OWNER: owner, M_RUN_AT: now, M_RUN_SCOPE: str(scope)},
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
            _praxis.write_build_state(str(cid), {M_RUN_AT: now}, **_ref_kw(ref))
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
            _praxis.write_build_state(str(cid), {k: None for k in _RUN_KEYS}, **_ref_kw(ref))
            n += 1
    return n


# --------------------------------------------------------------------------- planning-session marker

# The planning ARMING signal (the sibling of the whole-set run marker, for the ``plan_completeness``
# Stop hook). af-intake-plan stamps it at intake START and clears it at BLESS; while a non-stale
# marker is present the plan hook is ARMED and blocks a planning session's Stop until the plan
# mechanically blesses. It lives on a single, deterministic marker fact in the plan snapshot
# (``prd-<project>``) — so, like ``run_owner``/``run_at``, it is a session-owned, heartbeated meta.

# The category the marker fact carries (mirrors SURFACE_CATEGORY server-side).
PLANNING_MARKER_CATEGORY = "planning-marker"

# The build-run marker (holds gate-disable state for the Stop hooks — a separate
# category so a build run's disable records never collide with a planning session's marker).
BUILD_MARKER_CATEGORY = "build-marker"

# Meta keys for gate-disable records (on the build marker fact).
M_GATE_DISABLE_VARS = "gate_disable_vars"    # dict[str,str]: {var_name: observed_value}
M_GATE_DISABLED_AT = "gate_disabled_at"       # float: epoch seconds when first disable was stamped


def planning_project(project: str) -> str:
    """The BARE project name (a leading ``prd-`` is stripped, so a bare project or the snapshot name
    both resolve to the same marker)."""
    return project[len("prd-"):] if project.startswith("prd-") else project


def planning_marker_id(project: str, *, create: bool = False) -> str:
    """The id of ``project``'s planning marker fact in ``prd-<project>``, or ``""`` if none exists.

    The marker's id is SERVER-GENERATED (like a surface), not a computed address — the id is
    resolved by the idempotency key ``(scope=project, category="planning-marker")``. With
    ``create=True`` the marker is materialized if absent (the bootstrap a greenfield project needs);
    with the default ``create=False`` this is a pure read that returns ``""`` when no planning
    session has ever been stamped, so the Stop hook can treat absence as "inactive".
    """
    bare = planning_project(project)
    ref = project_ref(project).plan
    if create:
        return _praxis.ensure_planning_marker(bare, **_ref_kw(ref))
    for fact in (_praxis.facts_by(category=PLANNING_MARKER_CATEGORY, **_ref_kw(ref)) or []):
        meta = dict(fact.get("meta") or {})
        if (fact.get("scope") or meta.get("project")) == bare:
            return str(fact.get("id") or "")
    return ""


def planning_live(meta: dict, now: Optional[float] = None) -> bool:
    """True iff ``meta`` carries a NON-STALE planning marker (now - planning_at <=
    DEFAULT_PLANNING_TTL_S). A stale marker (a dead/abandoned intake) is ignored so a crashed
    planning session never arms the hook forever. Mirror of :func:`run_live`."""
    if now is None:
        now = time.time()
    if not meta.get(M_PLANNING_OWNER):
        return False
    at = meta.get(M_PLANNING_AT)
    if at is None:
        return False
    try:
        return (now - float(at)) <= float(DEFAULT_PLANNING_TTL_S)
    except (TypeError, ValueError):
        return False


def stamp_planning(project: str, owner: str) -> str:
    """Mark ``project``'s planning session ACTIVE for ``owner`` (planning_owner/planning_at) on the
    plan snapshot's marker fact. Called at intake start; re-stamping heartbeats the marker (refreshes
    planning_at). Returns the marker fact id. Mirror of :func:`stamp_run`."""
    mid = planning_marker_id(project, create=True)
    ref = project_ref(project).plan
    _praxis.patch_meta(mid, {M_PLANNING_OWNER: owner, M_PLANNING_AT: time.time()}, **_ref_kw(ref))
    return mid


def clear_planning(project: str, owner: str) -> bool:
    """Clear ``owner``'s planning marker (NULL planning_owner/planning_at) — called at BLESS, when the
    plan is done and the hook should go inert. Only clears a marker THIS owner holds (an unowned
    marker is also clearable); an owner mismatch returns False UNLESS the original owner is no longer
    live (stale marker) — then the marker is reclaimable by any owner rather than stranding the
    project. Mirror of :func:`clear_run`."""
    mid = planning_marker_id(project)
    if not mid:
        return True  # never stamped => nothing to clear; the hook is already inert
    ref = project_ref(project).plan
    marker_owner = _meta(mid, ref).get(M_PLANNING_OWNER)
    if marker_owner not in (owner, None):
        # Owner mismatch — but if the original owner is no longer live (stale), allow reclaim.
        if planning_live(_meta(mid, ref)):
            return False  # original owner is still live -> can't take over
        # Marker is stale — original owner is dead, reclaim it.
    _praxis.patch_meta(
        mid,
        {**{k: None for k in _PLANNING_KEYS}, M_BLESSED_AT: time.time()},
        **_ref_kw(ref),
    )
    return True


def planning_active(project: str, owner: Optional[str] = None,
                    now: Optional[float] = None) -> bool:
    """True iff a NON-STALE planning marker is present for ``project`` — the signal the
    ``plan_completeness`` hook arms on. When ``owner`` is given, the marker is ONLY armed for the
    session that stamped it (``planning_owner`` MUST match); a live marker owned by a different
    session does NOT arm. Reads the marker fact NOT-FOUND-TOLERANTLY: a missing marker fact means
    "no planning session" (inactive), NOT "Praxis down" — a genuine PraxisUnreachable still
    propagates so the hook fails closed."""
    mid = planning_marker_id(project)
    if not mid:
        return False  # no marker fact => no planning session (NOT "Praxis down")
    ref = project_ref(project).plan
    fact = _praxis.get_fact(mid, not_found_ok=True, **_ref_kw(ref))
    meta = dict((fact or {}).get("meta") or {})
    if owner is not None and meta.get(M_PLANNING_OWNER) != owner:
        return False  # a different owner holds the marker — not armed for this caller
    return planning_live(meta, now)


# --------------------------------------------------------------------------- plan-gate escalation (S8)

class PlanEscalationError(RuntimeError):
    """A durable escalation counter could not be read or is corrupt — the caller must fail LOUD,
    never silently return zero. Raised when the planning marker's meta is present but the attempts
    field is unreadable (wrong type) or the plan_hash is missing while attempts > 0."""


def _escalation_meta(project: str) -> dict:
    """Read the planning marker's meta for ``project``. Returns ``{}`` when no marker exists
    (a greenfield project with no intake session), raises :class:`PraxisUnreachable` on transport
    failure, and raises :class:`PlanEscalationError` when the marker is readable but its escalation
    fields are corrupt — a named error, not a silent zero."""
    mid = planning_marker_id(project)
    if not mid:
        return {}
    ref = project_ref(project).plan
    try:
        fact = _praxis.get_fact(mid, not_found_ok=True, **_ref_kw(ref))
    except _praxis.PraxisUnreachable:
        raise
    except Exception as exc:
        raise PlanEscalationError(
            f"unable to read planning marker for {project}: {exc}"
        ) from exc
    return dict((fact or {}).get("meta") or {})


def read_escalation_state(project: str) -> tuple[int, str, Optional[float]]:
    """Return ``(attempts, plan_hash, blocked_at)`` from the planning marker's meta.

    Returns ``(0, "", None)`` when the marker has never recorded an attempt (the cold-start state).
    Raises :class:`PlanEscalationError` when the marker IS present but its escalation fields are
    corrupt — the "named error" the acceptance condition requires, so a downstream gate that sees
    a corrupt counter fails LOUD instead of silently treating it as zero and admitting a plan that
    should be blocked.
    """
    meta = _escalation_meta(project)
    attempts_raw = meta.get(M_PLAN_ATTEMPTS)
    plan_hash = str(meta.get(M_PLAN_HASH) or "")
    blocked_raw = meta.get(M_PLAN_BLOCKED_AT)

    if attempts_raw is None:
        return 0, "", None

    # A non-None attempts value that is NOT an int/float is a corrupt counter — surface the named error.
    try:
        attempts = int(attempts_raw)
    except (TypeError, ValueError) as exc:
        raise PlanEscalationError(
            f"planning marker for {project} has a corrupt {M_PLAN_ATTEMPTS} field: "
            f"{attempts_raw!r} is not an integer — cannot determine the escalation state. "
            f"Clear it via clear_plan_blocked() or reset the marker."
        ) from exc

    blocked_at: Optional[float] = None
    if blocked_raw is not None:
        try:
            blocked_at = float(blocked_raw)
        except (TypeError, ValueError):
            pass  # a non-numeric blocked_at is treated as None (not a hard error)

    return attempts, plan_hash, blocked_at


def is_plan_blocked(project: str) -> bool:
    """True iff the plan is terminally escalated (``plan_blocked_at`` is set on the planning marker).
    The downstream build gate calls this to refuse the build phase while the escalation exists."""
    try:
        _, _, blocked_at = read_escalation_state(project)
    except PlanEscalationError:
        # A corrupt counter means we cannot determine whether the plan is blocked — fail LOUD
        # by treating it as blocked (the gate should refuse), NOT pass silently.
        return True
    return blocked_at is not None


def bump_escalation_attempts(project: str, snapshot_hash: str) -> int:
    """Increment the failed bless attempt counter for ``snapshot_hash`` and return the new count.
    If the stored hash differs from ``snapshot_hash`` the counter resets to 1 (a changed plan is
    not penalized). Raises :class:`PlanEscalationError` on a corrupt counter."""
    attempts, stored_hash, _ = read_escalation_state(project)
    if stored_hash != snapshot_hash:
        attempts = 0  # changed plan resets the counter
    attempts += 1
    mid = planning_marker_id(project, create=True)
    ref = project_ref(project).plan
    _praxis.patch_meta(mid, {
        M_PLAN_ATTEMPTS: attempts,
        M_PLAN_HASH: snapshot_hash,
    }, **_ref_kw(ref))
    return attempts


def stamp_plan_blocked(project: str) -> None:
    """Record the terminal escalation timestamp on the planning marker — the durable signal the
    downstream gate reads to refuse the build phase. Idempotent: a subsequent call overwrites
    the timestamp (re-blocking after an operator clear)."""
    mid = planning_marker_id(project, create=True)
    ref = project_ref(project).plan
    _praxis.patch_meta(mid, {M_PLAN_BLOCKED_AT: time.time()}, **_ref_kw(ref))


def reset_escalation_attempts(project: str) -> None:
    """Clear the failed-attempt counter and hash — called when the plan blesses, so a subsequent
    intake session starts fresh. Also clears the terminal block (escalation is resolved by the
    plan finally blessing, or by an operator clearing it).

    Best-effort: a missing space (e.g. in a test environment without Praxis) is a no-op, not a
    crash. The record lives in the plan snapshot; if that snapshot is unreachable, clearing it is
    moot anyway — the downstream gate will also be unreachable and will fail closed on its own."""
    mid = None
    try:
        mid = planning_marker_id(project)
    except _praxis.PraxisUnreachable:
        return  # no Praxis available => nothing to clear; the gate has bigger problems
    if not mid:
        return
    ref = project_ref(project).plan
    try:
        _praxis.patch_meta(mid, {
            M_PLAN_ATTEMPTS: None,
            M_PLAN_HASH: None,
            M_PLAN_BLOCKED_AT: None,
        }, **_ref_kw(ref))
    except _praxis.PraxisUnreachable:
        return  # unreachable => best-effort, not a crash
    except Exception:
        pass  # best-effort clear; a missing/corrupt marker is a no-op


def clear_plan_blocked(project: str) -> bool:
    """Operator action: clear the terminal escalation on a plan that never blesses.
    Returns True on success, False if the marker does not exist (nothing to clear).
    This is the explicit operator recovery path — separate from ``reset_escalation_attempts``
    which fires automatically at bless."""
    mid = planning_marker_id(project)
    if not mid:
        return False
    ref = project_ref(project).plan
    try:
        _praxis.patch_meta(mid, {M_PLAN_BLOCKED_AT: None}, **_ref_kw(ref))
    except _praxis.PraxisUnreachable:
        raise
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- build-run marker

# The build-run marker holds gate-disable STATE for the Stop hooks — when a gate stands down because
# a disable variable is set, the variable name and observed value are recorded here as durable state
# on the project's Praxis marker. After the run terminates, clear_gate_disable removes it so a review
# can tell whether the run was fully gated.


def build_marker_project(project: str) -> str:
    """The BARE project name (a leading ``prd-`` is stripped, so a bare project or the snapshot name
    both resolve to the same marker)."""
    return project[len("prd-"):] if project.startswith("prd-") else project


def build_marker_id(project: str, *, create: bool = False) -> str:
    """The id of ``project``'s build marker fact in ``prd-<project>``, or ``""`` if none exists.

    The marker's id is SERVER-GENERATED (like a surface), resolved by the idempotency key
    ``(scope=project, category="build-marker")``. With ``create=True`` the marker is materialized
    if absent (the bootstrap a greenfield project needs); with the default ``create=False`` this is
    a pure read that returns ``""`` when no build run has ever been tracked.
    """
    bare = build_marker_project(project)
    ref = project_ref(project).plan
    if create:
        return _praxis.ensure_build_marker(bare, **_ref_kw(ref))
    for fact in (_praxis.facts_by(category=BUILD_MARKER_CATEGORY, **_ref_kw(ref)) or []):
        meta = dict(fact.get("meta") or {})
        if (fact.get("scope") or meta.get("project")) == bare:
            return str(fact.get("id") or "")
    return ""


def stamp_gate_disable(project: str, var_name: str, value: str) -> dict:
    """Record that ``var_name`` (e.g. ``"FACTORY_GATE_DISABLED"``) was observed as ``value`` when
    a Stop gate stood down. ACCUMULATES: calling with a different variable adds to the existing set
    rather than replacing it. Returns the patched fact.

    The variable name and value are written onto the project's build marker fact (in the plan
    snapshot), so a report can read which disable variables were in effect during the run.
    """
    mid = build_marker_id(project, create=True)
    ref = project_ref(project).plan
    meta = _meta(mid, ref)
    prev_vars: dict[str, str] = dict(meta.get(M_GATE_DISABLE_VARS) or {})
    prev_vars[str(var_name)] = str(value)
    return _praxis.patch_meta(mid, {
        M_GATE_DISABLE_VARS: prev_vars,
        M_GATE_DISABLED_AT: meta.get(M_GATE_DISABLED_AT) or time.time(),
    }, **_ref_kw(ref))


def clear_gate_disable(project: str) -> bool:
    """Clear the gate-disable record (remove disable variable names/values). Call when the run
    terminates so a post-run report can tell whether the run was fully gated.

    No fact yet stamped => already clear => True. Returns False only on an owner mismatch (never
    relevant for the build marker since it has no ownership check)."""
    mid = build_marker_id(project)
    if not mid:
        return True  # never stamped => nothing to clear
    ref = project_ref(project).plan
    _praxis.patch_meta(mid, {
        M_GATE_DISABLE_VARS: None,
        M_GATE_DISABLED_AT: None,
    }, **_ref_kw(ref))
    return True


def gate_disable_vars(project: str) -> dict[str, str]:
    """The disable variables recorded during this project's run (empty dict if none were set)."""
    mid = build_marker_id(project)
    if not mid:
        return {}
    ref = project_ref(project).plan
    fact = _praxis.get_fact(mid, not_found_ok=True, **_ref_kw(ref))
    meta = dict((fact or {}).get("meta") or {})
    return dict(meta.get(M_GATE_DISABLE_VARS) or {})


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

def acceptance_requirement(cid: str, acceptance_text: str,
                           verify: str = "automated") -> dict:
    """The ticket's OWN binary acceptance condition as a synthetic validation requirement.

    This is the coverage-contract FLOOR. Every build ticket must at minimum prove its acceptance
    condition, so including it guarantees the resolved contract is never empty. An empty contract is
    exactly the deadlock this prevents: with zero requirements there is nothing to cover, the worker
    pins zero validations, and ``all_validations_passed`` can never become True — the ticket can be
    neither finished nor (without an explicit block) escaped. The floor gives the worker a concrete,
    always-authorable target: the red→green acceptance test the skill already mandates.

    ``verify`` carries the ticket's own ``meta.verify`` mode onto the floor. A ``verify="manual"``
    ticket's acceptance is a human-confirmed condition (a UX feel, a visual), so the floor inherits
    ``manual`` and its pass must come from an external/human signal — the executor may not
    self-check it (see :func:`all_validations_passed`).
    """
    return {"id": f"{cid}::acceptance", "text": str(acceptance_text),
            "meta": {"acceptance": str(acceptance_text), "synthetic": "acceptance-floor",
                     "verify": str(verify or "automated").strip().casefold()}}


# --------------------------------------------------------------------------- universal lane

# Tickets tagged with one of these (or carrying ``meta.universal_exempt``) have nothing to minimize —
# a one-line config change, vendored, or generated code. A subjective universal gate on such a ticket
# would be unsatisfiable and, because a subjective fail is content-hash-cached with no iteration
# consumed, would deadlock the session. So they are OMITTED from the universal lane entirely.
_UNIVERSAL_EXEMPT_TAGS = frozenset({"vendored", "generated", "config"})

# PATH-PREDICATE exemption (R33): a ticket whose touched paths sit ENTIRELY inside one of these
# language-convention immutable/generated directory names has nothing to minimize either, even
# with no human-authored tag — a plain ``migrations/`` directory is neither gitignored nor
# specially tagged, but must never be graded as if it were hand-authored application code. Mirrors
# the tool-side immutable-dir signal af-clean's own exemption manifest uses (R3).
_UNIVERSAL_EXEMPT_PATH_DIR_NAMES = frozenset({"migrations", "testdata", "__snapshots__", "fixtures"})


class UniversalLaneUnavailable(RuntimeError):
    """The ``promote_universal`` seeded-check library could not be loaded, so the universal lane
    would silently contribute ZERO gates to the coverage contract.

    This is raised, never swallowed: an unloadable library is indistinguishable from "there are no
    universal checks" at the call site, and that ambiguity is exactly how a whole build run shipped
    with its mandatory quality gate quietly absent (observed on sotos, 2026-07-31 — the build host's
    ``python3`` was 3.9, which has no stdlib ``tomllib``, so the lazy import raised
    ``ModuleNotFoundError`` and a bare ``except`` turned it into an empty lane).
    """


class _UniversalCheck(NamedTuple):
    """A ``promote_universal`` seeded check as recovered from the out-of-process loader — the same
    duck-type :func:`universal_requirements` reads off a real ``SeededCheck``, except ``rubric`` is
    already the serialized dict (it crossed a process boundary as JSON)."""

    check_id: str
    criterion: str
    report_only: bool
    rubric: Optional[dict]


# Loader run in a SIDECAR interpreter when this one cannot import the library itself (see
# :func:`_universal_checks_out_of_process`). Emits the same fields :func:`universal_requirements` reads.
_UNIVERSAL_LOADER_SRC = """
import json
from agent_factory.rubric import rubric_to_dict
from agent_factory.seeded_checks import universal_seeded_checks
print(json.dumps([
    {"check_id": c.check_id, "criterion": c.criterion, "report_only": bool(c.report_only),
     "rubric": rubric_to_dict(c.rubric) if c.rubric is not None else None}
    for c in universal_seeded_checks()
]))
"""


def _sidecar_pythons() -> list[str]:
    """Interpreters to retry the seeded-check load in, most-explicit first. ``sys.executable`` is
    deliberately absent — it is the one that just failed."""
    hooks = os.path.dirname(os.path.abspath(__file__))
    plugin = os.path.dirname(hooks)                 # …/agent_factory
    repo = os.path.dirname(plugin)                  # the checkout that contains it
    cands = [os.environ.get("PRAXIS_HOOK_PYTHON"), os.environ.get("AF_PYTHON"),
             os.path.join(plugin, ".venv", "bin", "python"),
             os.path.join(repo, ".venv", "bin", "python"),
             "python3.14", "python3.13", "python3.12", "python3.11"]
    # Actually EXCLUDE sys.executable, rather than only claiming to. Retrying the load in the
    # interpreter that just failed cannot succeed, it just doubles the latency of every failure.
    # It used to be absent by luck: sys.executable happened to be spelled differently from the
    # repo-venv candidate. On 3.14 it resolves to exactly that path, so the "sidecar" list started
    # handing back the failing interpreter -- and the guarantee was never enforced anywhere.
    me = set()
    if sys.executable:
        me.add(sys.executable)
        me.add(os.path.realpath(sys.executable))
    out = []
    for c in cands:
        if not c or c in me:
            continue
        if os.path.isabs(c) and os.path.realpath(c) in me:
            continue
        out.append(c)
    return out


def _universal_checks_out_of_process() -> Optional[list]:
    """Load the universal seeded checks in a sidecar interpreter, for the case where THIS one cannot
    (e.g. it predates stdlib ``tomllib``). Returns ``None`` when no candidate interpreter worked —
    the caller then raises rather than pretending the lane is empty."""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    for py in _sidecar_pythons():
        try:
            out = subprocess.run([py, "-c", _UNIVERSAL_LOADER_SRC], capture_output=True, text=True,
                                 env=env, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        try:
            rows = json.loads(out.stdout)
        except ValueError:
            continue
        return [_UniversalCheck(check_id=str(r.get("check_id") or ""),
                                criterion=str(r.get("criterion") or ""),
                                report_only=bool(r.get("report_only")),
                                rubric=r.get("rubric")) for r in rows]
    return None


def _universal_checks() -> list:
    """The ``promote_universal`` seeded checks. Lazily imports the (src-layout) package so importing
    this stdlib-only hook never hard-depends on it.

    NEVER returns ``[]`` on failure. If the in-process import does not work, the load is retried in a
    sidecar interpreter; if that also fails, :class:`UniversalLaneUnavailable` is raised. An empty
    return therefore means one thing only: the library loaded and declares no universal checks.
    """
    try:
        from agent_factory.seeded_checks import universal_seeded_checks
        return universal_seeded_checks()
    except Exception as exc:  # noqa: BLE001 - degraded to a sidecar load, or re-raised loudly below
        recovered = _universal_checks_out_of_process()
        if recovered is None:
            raise UniversalLaneUnavailable(
                f"could not load the promote_universal seeded checks ({type(exc).__name__}: {exc}). "
                f"The universal quality lane would contribute ZERO gates, so this is fatal rather "
                f"than silent. Run the hooks under an interpreter that can import "
                f"agent_factory.seeded_checks (Python >= 3.11 for stdlib tomllib) — e.g. set "
                f"PRAXIS_HOOK_PYTHON — and ensure agent_factory/seeded_checks.toml is present."
            ) from exc
        sys.stderr.write(
            f"[af-build] WARNING: this interpreter ({sys.executable}) cannot import "
            f"agent_factory.seeded_checks ({type(exc).__name__}: {exc}); the universal lane was "
            f"loaded out-of-process instead. Point PRAXIS_HOOK_PYTHON at a Python >= 3.11.\n"
        )
        return recovered


def _path_dir_exempt(path: Any) -> bool:
    """True iff any path SEGMENT of ``path`` is one of the immutable/generated directory names."""
    norm = str(path or "").replace(os.sep, "/").strip("/")
    return any(seg in _UNIVERSAL_EXEMPT_PATH_DIR_NAMES for seg in norm.split("/") if seg)


def _paths_exempt(paths: Optional[list]) -> bool:
    """True iff ``paths`` is non-empty and EVERY path is exempt — a ticket touching even one
    non-exempt path is not covered by the path predicate (it still has real code to grade)."""
    ps = [str(p) for p in (paths or []) if str(p or "").strip()]
    return bool(ps) and all(_path_dir_exempt(p) for p in ps)


def _universal_exempt(ticket_meta: Optional[dict], paths: Optional[list] = None) -> bool:
    """True iff the ticket opts out of the universal lane: an exempt tag, ``meta.universal_exempt``,
    or (R33) every one of ``paths`` sits inside an immutable/generated directory (see
    :data:`_UNIVERSAL_EXEMPT_PATH_DIR_NAMES`). ``paths`` is evaluated in TWO phases by the caller —
    declared paths at pin time (:func:`start_ticket` has no diff yet, so this is best-effort/usually
    empty) and the ACTUAL touched paths re-checked at grade time (:mod:`_graded_verify`, which
    already holds the code diff) — with the grade-time result winning whenever the two disagree.
    """
    meta = ticket_meta or {}
    if meta.get("universal_exempt"):
        return True
    tags = _as_list(meta.get("tags")) + _as_list(meta.get("applies_to"))
    if any(normalize_tag(t) in _UNIVERSAL_EXEMPT_TAGS for t in tags if t):
        return True
    return _paths_exempt(paths)


def universal_requirements(cid: str, ticket_meta: Optional[dict],
                           paths: Optional[list] = None) -> list[dict]:
    """The universal graded requirements to inject onto ``cid`` — one per ``promote_universal`` seeded
    check, unless the ticket is exempt (tag/meta OR the R33 path predicate against ``paths``). Each
    carries ``kind="graded"`` + its serialized rubric + a stable id (the seeded ``check_id``) + a
    ``report_only`` flag, so a worker-synthesized validation covers it and ``verify_graded_check``
    grades it exactly like a pool graded check.
    """
    if _universal_exempt(ticket_meta, paths=paths):
        return []
    out: list[dict] = []
    for chk in _universal_checks():
        rubric = getattr(chk, "rubric", None)
        if rubric is None:  # only graded universal checks are injectable
            continue
        if not isinstance(rubric, dict):  # a real Rubric; the sidecar loader already serialized its own
            from agent_factory.rubric import rubric_to_dict
            rubric = rubric_to_dict(rubric)
        out.append({
            "id": chk.check_id,
            "text": chk.criterion,
            "meta": {
                "check_id": chk.check_id,
                "kind": "graded",
                "rubric": rubric,
                "report_only": bool(chk.report_only),
                "universal": True,
                "source_check_id": chk.check_id,
            },
        })
    return out


def migrate_pinned_universal(ticket_meta: dict, check_id: str, new_rubric: dict) -> Optional[dict]:
    """PURE (B36): the meta patch that brings any FROZEN pinned entry for ``check_id`` on ONE ticket
    back in sync with the current seeded rubric — needed because :func:`_norm_validation` freezes a
    graded entry's rubric at synthesis time, so an axis re-guidance or anchor edit to
    ``seeded_checks.toml`` never reaches a pinned entry a worker already synthesized. Returns ``None``
    (no-op) when the ticket carries no such entry, or it already matches — never patches unnecessarily.
    """
    pinned = list(ticket_meta.get(M_PINNED_CHECKS) or [])
    changed = False
    out = []
    for e in pinned:
        if e.get("source_check_id") == check_id and e.get("rubric") != new_rubric:
            e = {**e, "rubric": new_rubric}
            changed = True
        out.append(e)
    return {M_PINNED_CHECKS: out} if changed else None


def migrate_universal_pinned_entries(tickets: list[dict], check_id: str, new_rubric: dict,
                                     ref: Optional[tuple[str, str]] = None) -> list[str]:
    """Apply :func:`migrate_pinned_universal` across every ticket fact in ``tickets`` whose meta
    carries a stale frozen entry for ``check_id``. Returns the ids actually patched (I/O boundary —
    the pure decision lives in :func:`migrate_pinned_universal`, this only drives the patch calls)."""
    migrated: list[str] = []
    for t in tickets:
        cid = str(t.get("id") or "")
        meta = t.get("meta") or {}
        patch = migrate_pinned_universal(meta, check_id, new_rubric)
        if patch is not None and cid:
            _praxis.write_build_state(cid, patch, **_ref_kw(ref))
            migrated.append(cid)
    return migrated


def contract_with_floor(cid: str, acceptance_text: str, resolved: list,
                        verify: str = "automated",
                        ticket_meta: Optional[dict] = None,
                        paths: Optional[list] = None) -> list:
    """Compose the coverage contract: the resolved Praxis requirements PLUS the acceptance floor
    PLUS (when ``ticket_meta`` is given and the ticket is not exempt) the always-on universal lane.

    Prepends :func:`acceptance_requirement` (deduped) when the ticket has a non-empty acceptance
    condition, so a ticket with NO matching Praxis checks still has exactly one thing to validate —
    its own acceptance — and can therefore be finished. A ticket with neither resolved checks NOR an
    acceptance condition returns an empty list: a genuine planning defect the build surfaces by
    ``block()``-ing the ticket (never a silent wedge), since there is nothing it could honestly prove.

    ``verify`` is the ticket's own ``meta.verify`` mode, threaded onto the floor so a manual ticket's
    acceptance floor is itself manual (worker-self-certification barred).

    ``ticket_meta`` (the ticket's own meta, passed by :func:`start_ticket`) drives the UNIVERSAL lane:
    the ``promote_universal`` seeded checks are appended (deduped by id) on every NON-exempt ticket,
    tag-independent. It is a no-op when ``ticket_meta`` is None (the pure callers — preview/tests keep
    their exact contract) or the contract is otherwise empty (the block path must survive, never be
    masked into buildable by a report-only universal).

    ``paths`` (R33) is the PIN-TIME phase of the path-predicate exemption: the ticket's DECLARED
    touched paths, if any — usually empty (:func:`start_ticket` has no diff to derive them from yet),
    so this is a best-effort, conservative-by-default input. The authoritative re-check against the
    ACTUAL touched paths happens at grade time (:mod:`_graded_verify`), which wins on disagreement.
    """
    reqs = list(resolved)
    text = str(acceptance_text or "").strip()
    if text:
        floor = acceptance_requirement(cid, text, verify=verify)
        if floor["id"] not in {_check_id(r) for r in reqs}:
            reqs = [floor] + reqs
    if ticket_meta is not None and reqs:
        existing = {_check_id(r) for r in reqs}
        for u in universal_requirements(cid, ticket_meta, paths=paths):
            if u["id"] not in existing:
                reqs.append(u)
                existing.add(u["id"])
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
    tmeta = _meta(cid, plan)
    resolved = resolve_validation_requirements(cid, project=project, scope="validation",
                                               override=override)
    verify_mode = str(tmeta.get("verify") or "automated").strip().casefold()

    # --- RESUMABILITY GUARD (plan 003) -------------------------------------------------------------
    # BEFORE leasing, probe whether a cold worker could reconstruct "done" from state alone. We feed
    # the probe the factory's own verify DEFAULT (absent == "automated", as below) and no id universe,
    # so it routes PRECISELY on the coverability contract — the same empty-contract case
    # contract_with_floor would otherwise force the worker to block() on. A non-resumable ticket is
    # NOT claimed: mark it under_specified (surfaced to intake) and return None. Keep this block
    # localized/additive — it is shared with plan 001's claim path.
    probe = resumability_report({**tmeta, "verify": verify_mode}, resolved)
    if not probe["resumable"]:
        _praxis.write_build_state(cid, {M_UNDER_SPECIFIED: probe["missing"]}, **_ref_kw(plan))
        return None
    # --- END RESUMABILITY GUARD --------------------------------------------------------------------

    if not claim(cid, owner, ttl=ttl, ref=plan):
        return None
    # The probe passed — if a prior pass had routed this ticket under_specified, that gap is now
    # resolved, so clear the marker (a None value REMOVES the key). Only written when set,
    # so a ticket that was never routed claims byte-identically to before.
    if tmeta.get(M_UNDER_SPECIFIED):
        _praxis.write_build_state(cid, {M_UNDER_SPECIFIED: None}, **_ref_kw(plan))
    # UNION of plan 003 (resumability guard, above) + plan 001 (universal lane): the contract is
    # composed AFTER the claim, threading ``ticket_meta=tmeta`` so plan 001's report-only universal
    # minimalism check is injected onto every non-exempt ticket. ``paths`` is the R33 PIN-TIME phase
    # of the path-predicate exemption: the ticket's own DECLARED touched paths (``meta.paths``), if
    # any were authored — there is no diff yet at this point, so the grade-time re-check (which does
    # have the diff) is what actually enforces/discharges the predicate; this is best-effort only.
    requirements = contract_with_floor(cid, tmeta.get("acceptance"), resolved,
                                       verify=verify_mode, ticket_meta=tmeta,
                                       paths=_as_list(tmeta.get("paths")))
    # COVERAGE-GAP WARNING (build-time visibility of the intake defect): a verify="automated" ticket
    # that resolves ZERO declared checks is buildable via its acceptance floor, but it means NO declared
    # gate exists for it — exactly the floor-only defect `resolve_preview --require-coverage` blocks at
    # intake. Surface it loudly here too so the gap is visible even on a plan authored before that guard.
    if verify_mode == "automated" and not resolved:
        sys.stderr.write(
            f"[af-build] WARNING: ticket {cid} is verify=automated but resolved NO declared checks — "
            f"only its acceptance floor. It is buildable, but no declared validation gate covers it. "
            f"Author a building-validation check for its tags (see af-intake-build-validation) or "
            f"confirm it should be verify=manual.\n"
        )
    # BRIEFING: hand the worker everything the ticket already knows, at claim time.
    #
    # A regressed ticket carried a full failure report in meta (why it came back, the failing test,
    # what the rebuild must address) and NOTHING read it: `audit_disposition` appeared four times in
    # the codebase and every one was a write. So a worker re-claimed a ticket that post-merge
    # verification had already diagnosed, saw only the original acceptance condition, and re-derived
    # the same diagnosis from scratch — or repeated the approach that had just failed.
    #
    # Emitting on stderr (same channel as the coverage warning above) rather than changing the return
    # type keeps every existing caller working, and lands the briefing in the worker's own command
    # output at the moment it claims — it cannot be skipped by not knowing which field to read.
    sys.stderr.write(ticket_briefing(cid, tmeta))
    pin_requirements(cid, requirements, ref=plan)
    return requirements


# Ticket-meta keys that are plumbing, not working context. Everything NOT in here is surfaced to the
# worker, so a field added later shows up automatically instead of waiting for someone to remember it.
_BRIEFING_SKIP = frozenset({
    "build_state", "claim_owner", "claim_at", "claim_heartbeat_at", "claim_lease_ttl",
    "pinned_checks", "auditTrail", "embedding", "requirement_id", "title",
})


def ticket_briefing(cid: str, meta: Optional[dict]) -> str:
    """Everything a cold worker should know before touching this ticket, as printable text.

    Ordered by what changes the worker's first action: why it came back (a regressed ticket's
    report), then its contract, then the rest of its authored context. Returns "" when there is
    nothing worth saying, so a first build stays quiet.
    """
    m = dict(meta or {})
    lines: list[str] = []

    detail = m.get("regression_detail")
    disposition = str(m.get("audit_disposition") or "").strip()
    if detail or disposition:
        lines.append(f"[af-build] TICKET {cid} CAME BACK — read this before writing code.")
        if isinstance(detail, dict):
            src = str(detail.get("source") or "").strip()
            if src:
                lines.append(f"  source        : {src}")
            for key, label in (("reason", "what failed"), ("evidence", "evidence"),
                               ("required_fix", "the rebuild must")):
                val = str(detail.get(key) or "").strip()
                if val:
                    lines.append(f"  {label:<14}: {val}")
            if str(detail.get("source") or "") == "post-merge-verification":
                lines.append("  NOTE          : the previous attempt was GREEN in its own worktree and "
                             "failed only once merged, so repeating that approach reproduces the "
                             "failure. The defect is integration-level — build against the CURRENT "
                             "integrated tree.")
        elif detail:
            lines.append(f"  detail        : {detail}")
        if disposition and not isinstance(detail, dict):
            lines.append(f"  disposition   : {disposition}")

    if m.get("block_reason"):
        lines.append(f"[af-build] TICKET {cid} was previously BLOCKED: {m['block_reason']}")
    if m.get(M_UNDER_SPECIFIED):
        lines.append(f"[af-build] TICKET {cid} was routed under_specified on: {m[M_UNDER_SPECIFIED]}")

    # Everything else the ticket carries, so "all the information" is not a curated subset.
    rest = {k: v for k, v in m.items()
            if k not in _BRIEFING_SKIP and k not in ("regression_detail", "audit_disposition",
                                                     "block_reason", M_UNDER_SPECIFIED)
            and v not in (None, "", [], {})}
    if rest and lines:
        lines.append(f"[af-build] TICKET {cid} other authored context:")
        for k in sorted(rest):
            v = str(rest[k])
            lines.append(f"  {k:<14}: {v if len(v) <= 400 else v[:400] + ' …'}")

    return ("\n".join(lines) + "\n") if lines else ""
