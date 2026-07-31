#!/usr/bin/env python3
"""Mechanical plan-gate check — run ``agent_factory.plan_gate.evaluate_plan`` over the LIVE
``prd-<project>`` requirement facts and exit non-zero (naming every reason) if the plan is rejected.

This is the ENFORCED form of af-intake-plan's B6/B9 gate: the bless step runs this and cannot be
cleared while it exits non-zero, so plan_gate stops being skippable prose. It reads the exact same
fields the gate keys off — including ``meta.tags`` / ``meta.verify`` / ``meta.decision`` — so the
architecture-decision rules (recognized by tag OR the decision marker) fire on the live plan.

    python -m agent_factory.tools.plan_gate_check <project> [--out-of-scope c1,c2,...]

On top of the PURE rules it also runs three LIVE-DATA rules the pure gate cannot express, because
each needs a Praxis read rather than the mapped requirement list (see "live-data rules" below):
``R-NO-DUPLICATE-REQUIREMENT-ID``, ``R-NO-INVISIBLE-TICKET`` (both hard) and the
``R-REJECTED-WITHOUT-AUDIT`` warning.

READ-ONLY: it only reads requirement facts; it never writes. Exit 0 = admitted, 1 = rejected (reasons
printed), 2 = could not run (Praxis unreachable, or no requirement facts for the project).

Import note: there are two ``agent_factory`` roots — the top-level namespace package (this ``tools/``
dir) and the regular ``src/agent_factory`` package (which holds ``plan_gate``/``gate``). They can't
both be on ``sys.path`` as one importable ``agent_factory``, so we file-load ``gate.py`` (pure stdlib)
and ``plan_gate.py`` under their canonical module names — robust however this tool is invoked.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # agent_factory/tools
_AF = _HERE.parent                               # agent_factory
_HOOKS = _AF / "hooks"
_SRC_AF = _AF / "src" / "agent_factory"

if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

import _praxis  # noqa: E402
from _praxis import PraxisUnreachable  # noqa: E402


def _load(modname: str, path: Path):
    """Import a module from an explicit file path, registering it under ``modname`` so a sibling's
    ``from <modname> import ...`` resolves to it (plan_gate imports ``agent_factory.gate``)."""
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# gate.py and contract_signature.py are pure stdlib; register them under their canonical names
# FIRST so plan_gate's ``from agent_factory.{gate,contract_signature} import ...`` both resolve.
_gate = _load("agent_factory.gate", _SRC_AF / "gate.py")
_cs = _load("agent_factory.contract_signature", _SRC_AF / "contract_signature.py")
_pg = _load("agent_factory.plan_gate", _SRC_AF / "plan_gate.py")
evaluate_plan = _pg.evaluate_plan
Requirement = _pg.Requirement
Reason = _gate.Reason
Verdict = _gate.Verdict


@dataclass
class PlanVerdict(Verdict):
    """A :class:`Verdict` that also carries NON-blocking ``warnings``.

    The pure gate has one severity (a reason rejects). ``R-REJECTED-WITHOUT-AUDIT`` is an
    advisory: it describes facts that are ALREADY out of the plan, so it must be shouted at the
    operator without flipping the exit code — a healthy-but-noisy plan still exits 0. Keeping the
    warnings on the verdict (rather than a third return value) preserves ``check_plan``'s
    ``(verdict, requirements)`` contract.
    """

    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- live-data rule IDs
#
# Stable rule-IDs, same contract as ``plan_gate``'s: callers key off ``rule_id``, never the prose.
# These three cannot live in the pure gate because each is a statement about the STORED FACTS
# (identity collisions, facts the categorical query cannot see, rejected facts) rather than about
# the requirement list the gate is handed — by construction the pure gate only ever sees the facts
# that were already visible, which is exactly the blind spot R-NO-INVISIBLE-TICKET closes.
R_NO_DUPLICATE_REQUIREMENT_ID = "R-NO-DUPLICATE-REQUIREMENT-ID"  # one requirement_id, one ACTIVE fact
R_NO_INVISIBLE_TICKET = "R-NO-INVISIBLE-TICKET"      # every active ticket is visible to the category read
R_REJECTED_WITHOUT_AUDIT = "R-REJECTED-WITHOUT-AUDIT"  # (warning) a ticket left the plan with no human



def _bare(project: str) -> str:
    p = str(project or "").strip()
    while p.startswith("prd-"):
        p = p[len("prd-"):]
    return p


def requirement_from_fact(fact: dict) -> Requirement:
    """Map ONE live ``prd-<project>`` requirement fact onto a plan-gate :class:`Requirement`.

    The gate keys ``id`` and ``depends_on`` on the ``requirement_id`` (e.g. "R8"), so we use
    ``meta.requirement_id`` as the id (falling back to the raw fact id). ``source`` is a top-level
    fact column; everything else lives in ``meta``. Crucially ``meta.decision`` is threaded through —
    dropping it here would re-open the tag-only hole the marker closes.
    """
    meta = fact.get("meta") or {}
    rid = str(meta.get("requirement_id") or fact.get("id") or fact.get("factId") or "")
    return Requirement(
        id=rid,
        text=str(fact.get("text") or fact.get("content") or ""),
        acceptance=str(meta.get("acceptance") or ""),
        defines=list(meta.get("defines") or []),
        references=list(meta.get("references") or []),
        source=str(fact.get("source") or meta.get("source") or ""),
        depends_on=[str(d) for d in (meta.get("depends_on") or [])],
        tags=list(meta.get("tags") or []),
        verify=str(meta.get("verify") or ""),
        decision=str(meta.get("decision") or ""),
    )


def read_contract(project: str) -> dict:
    """Read the signed-contract evidence for ``project`` and reduce it to the ``{signed,
    actions_recorded}`` field ``evaluate_plan`` gates on (``R-CONTRACT-SIGNED``).

    The ``contract-signed`` episode is recorded by intake's evaluator (via the ``praxis_record_episode``
    MCP tool → the store-only episodic lane), so we read the episodic decision log via the U1
    ``get_episodes`` wrapper and evaluate it with the PURE ``contract_signature`` helpers — keeping
    ``evaluate_plan`` free of any Praxis read. A plan is "signed with actions" iff SOME signed episode
    also records real evaluator actions (anti-Goodhart). No signed episode -> ``signed=False`` (reject).

    Raises :class:`PraxisUnreachable` (fail-closed) if the episodes can't be read.
    """
    # Look in the PLAN SNAPSHOT first, then working memory. Reading only working memory (the
    # prior behavior) made the signature session-local: intake records it in working memory and
    # `save_snapshot` copies it into `prd-<project>`, so a LATER session — or any other agent —
    # re-ran the gate against an empty working memory and saw an unsigned plan even though the
    # blessed snapshot carried the signature. The snapshot is the durable plan, so it is
    # authoritative; working memory is the in-flight fallback for the intake session itself.
    # (space, snapshot) derived inline rather than via _ticket_state.project_ref, to keep this
    # tool's imports to the stdlib-plus-_praxis set it already declares.
    bare = project[4:] if project.startswith("prd-") else project
    episodes: list = []
    try:
        episodes += _praxis.get_episodes(space=bare, snapshot=f"prd-{bare}")
    except PraxisUnreachable:  # a missing/unreadable snapshot is not a signature failure
        pass
    # The snapshot lane above is project-scoped BY CONSTRUCTION (it reads prd-<bare> directly), so it
    # stays authoritative and unfiltered. Working memory is NOT: it is one shared graph per principal,
    # so an unscoped `is_signed` match there let one project's contract satisfy another's
    # R-CONTRACT-SIGNED — a silent cross-project pass (observed: prd-af-clean admitting on
    # prd-praxis's signature). Scope only that fallback lane, by the project name its text carries.
    needle = f"prd-{bare}"
    episodes += [e for e in _praxis.get_episodes()
                 if needle in str(e.get("text") or e.get("content") or "")]
    signed = [e for e in episodes if _cs.is_signed(e)]
    if not signed:
        # NO signed contract episode exists — return the EMPTY dict (not {"signed": False, ...}) so
        # evaluate_plan reports its "no contract evidence at all" reason rather than "unsigned or
        # malformed". Both reject; the distinction tells the operator whether the negotiation never
        # happened or produced something unusable.
        return {}
    return {"signed": True, "actions_recorded": any(_cs.actions_recorded(e) for e in signed)}


def _fact_id(fact: dict) -> str:
    return str(fact.get("id") or fact.get("factId") or "")


def _meta(fact: dict) -> dict:
    m = fact.get("meta")
    return m if isinstance(m, dict) else {}


def _is_ticket(fact: dict) -> bool:
    """Is this fact recognisably a TICKET, independent of its ``category`` column?

    A plan snapshot also holds planning markers, episodes, surfaces, checks and other categories, so
    "has no category" is NOT the signal. ``meta.requirement_id`` (the plan identity the whole factory
    keys on) and ``meta.build_state`` (the build lifecycle field) are: only tickets carry them. This
    is the same identity-meta recognition the write path now uses, so the reader and the writer agree
    on what a ticket is even when the ``category`` column does not.
    """
    meta = _meta(fact)
    return bool(str(meta.get("requirement_id") or "").strip()) or bool(
        str(meta.get("build_state") or "").strip())


def _plan_identities(everything: list[dict]) -> set[str]:
    """Every name a ticket in this snapshot can be referred to by — fact ids AND requirement_ids.

    ``merged_into`` is written by hand and names the survivor either way (the observed live value is
    a requirement_id, ``"OBS18"``), so an absorption target is resolved against both namespaces.
    """
    names = {_fact_id(f) for f in everything}
    names |= {str(_meta(f).get("requirement_id") or "").strip() for f in everything}
    return {n for n in names if n}


def _is_absorbed(fact: dict, known: set[str]) -> bool:
    """Was this ticket deliberately CLOSED BY ABSORPTION into another ticket that EXISTS?

    An intake or review pass may decide a ticket has no standalone acceptance and fold it into a
    sibling whose acceptance now covers it. That closure is recorded as ``meta.build_state="merged"``
    plus ``meta.merged_into`` naming the surviving ticket, and the fact is moved out of the
    requirement category so no reader treats it as buildable work. Re-categorising it IS how the
    closure is recorded, so this is the one legitimate reason a ticket-shaped fact sits outside the
    requirement lane — and the one carve-out.

    What separates it from corruption is that it is DECLARED, so all three markers are demanded and
    a missing one still fires:

    * ``build_state == "merged"`` — the closure itself (case-insensitive; it is hand-written);
    * a non-empty ``merged_into`` — a bare ``merged`` pointing at nothing is an unfinished closure,
      and corruption never declares a survivor, it just loses the category;
    * ``merged_into`` RESOLVES to a ticket in this snapshot (``known``, from :func:`_plan_identities`)
      — absorption into a ticket that does not exist is not absorption, it is the incident's own
      failure one level deeper: work silently gone, with a field stamped on it that stops anyone
      looking. Requiring the survivor to exist is also what keeps the carve-out from degrading into
      a blanket escape hatch, since a made-up target no longer buys an exemption.

    The resolution is deliberately PERMISSIVE — any state, any category, either namespace — because
    its job is to refute "the survivor does not exist", not to re-litigate the survivor's health. A
    stricter "the survivor must itself be a visible active requirement" would make this rule fire on
    legitimate absorption chains (A folded into B, B later folded into C).
    """
    meta = _meta(fact)
    target = str(meta.get("merged_into") or "").strip()
    return (
        str(meta.get("build_state") or "").strip().lower() == "merged"
        and bool(target)
        and target in known
    )


def _belongs_to_plan(fact: dict, bare: str) -> bool:
    """Does this fact claim to belong to THIS plan snapshot?

    The enumeration is already bound to ``(space=<bare>, snapshot=prd-<bare>)``, so membership is
    structural. We only exclude a fact whose provenance explicitly names a DIFFERENT plan (a fact
    copied/mounted in from another project), and treat a missing/empty source as belonging — an
    absent source is precisely the corruption shape we are hunting, not a licence to skip.
    """
    src = str(fact.get("source") or _meta(fact).get("source")
              or _meta(fact).get("provenance") or "").strip()
    return not src.startswith("prd-") or src == f"prd-{bare}"


def duplicate_requirement_id_reasons(facts: list[dict]) -> list:
    """``R-NO-DUPLICATE-REQUIREMENT-ID`` — two or more ACTIVE facts sharing one ``requirement_id``.

    ``requirement_id`` is the plan's identity: ``depends_on`` targets it, af-build's RESOLVE keys on
    it, and the gate's own ``Requirement.id`` IS it. Two active facts wearing the same id makes every
    id-keyed read ambiguous (one arbitrarily wins, the other's acceptance is silently never built) —
    and the pure gate cannot see it, because the duplicate collapses into the id-keyed views it
    builds. Hard failure, naming every colliding fact id.

    ``facts`` is the CATEGORICAL enumeration, and that scoping is the rule — not an accident of the
    caller. The lane a fact must be in to answer an id-keyed read IS ``category="requirement"``, so
    two facts collide only when both sit in it. This is why an ABSORBED ticket (:func:`_is_absorbed`)
    needs no exemption here and must not get one: absorption moves the fact OUT of the requirement
    lane, so an absorbed ``OBS23`` alongside a live requirement ``OBS23`` is not ambiguity — the note
    is unreachable by every id-keyed reader (depends_on resolution, af-build's RESOLVE, this gate),
    which is precisely what the closure was for. Two ACTIVE facts *in the lane* remain a collision no
    matter what their meta declares; a merge marker must never buy a way out of that.
    """
    by_rid: dict[str, list[str]] = {}
    for f in facts:
        if str(f.get("state") or "active") != "active":
            continue
        rid = str(_meta(f).get("requirement_id") or "").strip()
        if rid:
            by_rid.setdefault(rid, []).append(_fact_id(f))
    return [
        Reason(R_NO_DUPLICATE_REQUIREMENT_ID,
               f"requirement_id '{rid}' is carried by {len(ids)} ACTIVE facts ({', '.join(sorted(ids))}) "
               f"— identity must be unique; every id-keyed read (depends_on, RESOLVE, this gate) is "
               f"ambiguous until one is retired")
        for rid, ids in sorted(by_rid.items()) if len(ids) > 1
    ]


def invisible_ticket_reasons(visible: list[dict], everything: list[dict], bare: str) -> list:
    """``R-NO-INVISIBLE-TICKET`` — an active ticket in the snapshot that the CATEGORICAL read misses.

    This is the write/read divergence rule. ``check_plan`` enumerates with
    ``facts_by(category="requirement", ...)``; a ticket written with a NULL/wrong ``category`` column
    never matches that predicate, so it is structurally invisible — not late, INVISIBLE, forever. The
    observed incident: seven tickets missed the identity-keyed write path (the one that stamps
    ``category="requirement"``) and the gate reported the plan as admitted-eligible. Only ``CHAT7``
    and ``CHAT11`` drew any complaint at all, and only indirectly, via ``R-NO-DANGLING-DEP`` on the
    tickets that happened to ``depends_on`` them; ``CHAT9``/``CHAT14``/``CHAT15``/``CHAT16`` were
    equally invisible and drew NOTHING, because nothing referenced them. Invisible AND unreferenced
    is how a plan loses work with no one seeing it, so the divergence itself must be the error —
    never a silent omission, and never contingent on someone else pointing at it.

    ``everything`` is the SECOND, broader enumeration (all states, NO ``category`` filter) over the
    same ``(space, snapshot)``; the rule is the set difference against ``visible``, narrowed to
    ACTIVE facts that are recognisably tickets (:func:`_is_ticket`) so the many legitimate
    non-requirement categories living in a plan snapshot cannot false-positive.

    A ticket CLOSED BY ABSORPTION (:func:`_is_absorbed`) is exempt. The rule's claim is "this is
    work the plan has silently lost"; an absorbed ticket has not been lost, it has been deliberately
    folded into another ticket that carries its acceptance, and re-categorising it out of the
    requirement lane is how that closure is RECORDED. Flagging it would demand a "repair" that
    destroys a correct decision — and a rule that fires on healthy data is a rule operators learn to
    ignore, which would blunt it for the real divergences it exists to catch.
    """
    seen = {_fact_id(f) for f in visible}
    known = _plan_identities(everything)
    missing = [f for f in everything
               if _fact_id(f) not in seen
               and str(f.get("state") or "active") == "active"
               and _is_ticket(f)
               and not _is_absorbed(f, known)
               and _belongs_to_plan(f, bare)]
    if not missing:
        return []
    named = ", ".join(sorted(
        f"{_meta(f).get('requirement_id') or '?'} ({_fact_id(f)}, category="
        f"{_meta(f).get('category') or f.get('category') or 'NULL'})" for f in missing))
    return [Reason(
        R_NO_INVISIBLE_TICKET,
        f"{len(missing)} ACTIVE ticket(s) live in prd-{bare} but are INVISIBLE to the "
        f"category=\"requirement\" enumeration: {named}. Their category column is NULL/wrong, so "
        f"EVERY reader is blind to them — this gate, incomplete_requirements, and af-build's RESOLVE "
        f"query alike. They will never be built and polling will never surface them; this is a write/"
        f"read divergence to repair at the data, not eventual consistency to wait out")]


# An audit entry's ``actor`` when the fact carries NO auditTrail of its own: ``fact_to_candidate``
# (knowledge/serve/pipeline_adapter.py) SYNTHESIZES a distilled+scored pair at READ TIME for any such
# fact. So actor=="pipeline" means "this fact never carried a real audit trail" — the signal we want.
# It is NOT evidence that some background job did the rejecting; there is no such job.
_SYNTHETIC_ACTORS = {"", "pipeline"}
# Actions a real rejection/demotion appends (facts_candidates._append_audit) with a human actor.
_REJECTION_ACTIONS = {"rejected", "superseded", "demoted", "invalidated"}


def rejected_without_audit_warnings(everything: list[dict]) -> list:
    """``R-REJECTED-WITHOUT-AUDIT`` (WARNING) — a ticket in ``rejected`` state whose ``auditTrail``
    records no human-actor rejection.

    A rejected fact is invisible to every active query, so the build silently never builds it. A
    HUMAN rejection leaves ``{"action": "rejected"|"superseded", "actor": <non-pipeline>}`` on
    ``meta.auditTrail``; the absence of that entry — no trail at all, or a trail of only read-time
    synthesized ``actor="pipeline"`` entries — is the fingerprint of the silent auto-rejection that
    flipped three already-hardened tickets with no human in the loop.

    Advisory, not blocking: these facts are already outside the plan the gate evaluates, and a
    legitimately-retired ticket must not wedge a healthy plan at exit 1. It is reported loudly so a
    human decides whether each rejection was intended.

    A ticket CLOSED BY ABSORPTION (:func:`_is_absorbed`) is exempt, for the same reason it is exempt
    from ``R-NO-INVISIBLE-TICKET``: ``merged_into`` + ``merge_reason`` IS the human record of the
    decision, so the closure is declared rather than silent, which is the only thing this rule is
    asking about. The exemption is not vacuous — an absorbed ticket is normally ACTIVE (so this rule
    never reaches it), but a closure implemented as a rejection would otherwise be warned about
    forever with nothing an operator could do to settle it.
    """
    known = _plan_identities(everything)
    out = []
    for f in everything:
        if str(f.get("state") or "") != "rejected":
            continue
        if not (str(f.get("category") or "") == "requirement" or _is_ticket(f)):
            continue
        if _is_absorbed(f, known):
            continue
        trail = _meta(f).get("auditTrail")
        entries = trail if isinstance(trail, list) else []
        if any(isinstance(e, dict)
               and str(e.get("actor") or "").strip().lower() not in _SYNTHETIC_ACTORS
               and str(e.get("action") or "").strip().lower() in _REJECTION_ACTIONS
               for e in entries):
            continue
        rid = _meta(f).get("requirement_id") or "?"
        out.append(Reason(
            R_REJECTED_WITHOUT_AUDIT,
            f"{rid} ({_fact_id(f)}) is state=rejected but its auditTrail records no human-gate "
            f"rejection ({'no auditTrail at all' if not entries else 'only non-human entries'}) — a "
            f"rejected ticket is invisible to every active query, so the build will silently never "
            f"build it. Confirm this rejection was intended, or restore the ticket"))
    return out


def check_plan(project: str, out_of_scope: list[str] | None = None):
    """Read the live plan and run the gate. Returns ``(verdict, requirements)``.

    Also reads the signed-contract episode (:func:`read_contract`) and THREADS the ``contract`` field
    into the pure ``evaluate_plan`` so ``R-CONTRACT-SIGNED`` fires on the live plan — the read happens
    here (this tool has ``_praxis``), never inside the pure gate.

    Two enumerations are read over the SAME ``(space, snapshot)``: the CATEGORICAL one the gate has
    always used (``category="requirement"``, active), and a BROADER one with NO category filter and
    ``state="any"``. The pure rules run on the categorical read; the live-data rules
    (:func:`duplicate_requirement_id_reasons`, :func:`invisible_ticket_reasons`,
    :func:`rejected_without_audit_warnings`) need the broader one — a ticket the categorical
    predicate cannot see, or a fact that left the active set entirely, is invisible by definition to
    a query that filters on category and state.

    Raises :class:`PraxisUnreachable` (fail-closed) if the facts can't be read, and ``ValueError`` if
    the project has NO requirement facts (a wrong project/org or empty plan must not silently "pass").
    """
    bare = _bare(project)
    facts = _praxis.facts_by(category="requirement", space=bare, snapshot=f"prd-{bare}")
    if not facts:
        raise ValueError(
            f"no requirement facts found for prd-{bare} (space={bare}). Wrong project/org, an "
            f"unblessed plan, or an empty snapshot — refusing to report a vacuous PASS."
        )
    # The BROADER enumeration: same graph, no category predicate, every lifecycle state. Fail-closed
    # like every other read here — an unreadable second lane must not silently degrade to "nothing
    # diverged", which is the exact failure this rule exists to stop.
    everything = _praxis.facts_by(state="any", space=bare, snapshot=f"prd-{bare}")
    requirements = [requirement_from_fact(f) for f in facts]
    contract = read_contract(bare)
    verdict = evaluate_plan(requirements, out_of_scope=out_of_scope or [], project=bare,
                            contract=contract)
    reasons = list(verdict.reasons)
    reasons += duplicate_requirement_id_reasons(facts)
    reasons += invisible_ticket_reasons(facts, everything, bare)
    return PlanVerdict(admitted=not reasons, reasons=reasons,
                       warnings=rejected_without_audit_warnings(everything)), requirements


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent_factory.tools.plan_gate_check",
        description="Run the plan done-gate over the LIVE prd-<project> facts; exit non-zero if the "
                    "plan is rejected. The ENFORCED (mechanical) form of af-intake-plan's bless gate.")
    p.add_argument("project", help="bare project name (or prd-<project>); reads snapshot prd-<project>")
    p.add_argument("--out-of-scope", default="",
                   help="comma-separated concepts declared out of scope (suppresses R-NO-DANGLING for them)")
    args = p.parse_args(argv)

    oos = [c.strip() for c in args.out_of_scope.split(",") if c.strip()]
    try:
        verdict, requirements = check_plan(args.project, out_of_scope=oos)
    except PraxisUnreachable as e:
        print(f"error: Praxis unreachable — cannot run the plan gate: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    bare = _bare(args.project)
    print(f"plan gate: prd-{bare}  ({len(requirements)} requirement(s))")
    # Warnings print BEFORE the verdict and never touch the exit code (see PlanVerdict).
    for w in getattr(verdict, "warnings", []):
        print(f"  warning: [{w.rule_id}] {w.message}", file=sys.stderr)
    if verdict.admitted:
        print("ADMITTED — the plan passes every mechanical rule.")
        return 0
    print(f"REJECTED — {len(verdict.reasons)} reason(s); the bless is BLOCKED until these are fixed:\n",
          file=sys.stderr)
    for r in verdict.reasons:
        print(f"  [{r.rule_id}] {r.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
