"""The plan done-gate: a deterministic verifier the af-plan skill runs before
admitting a PRD (Milestone 1a).

The skill (an LLM) drafts each requirement and tags it with the concepts it
*defines* and the concepts it *references*; this module then mechanically checks
the closure properties that prose review keeps missing. Pushing the gate into
tested code (rather than leaving it as skill prose) is the thin-harness
discipline: the rules below are the same ones the skill claims to enforce, but
here they are executable and covered by evals.

Rules enforced (each failure is a rejection reason, never a silent pass):

- **Binary acceptance** — every requirement needs a non-empty acceptance
  condition. ("every requirement maps to >=1 binary acceptance condition.")
- **No vague terms** — a requirement may not use an unquantified vague term
  (fast, secure, scalable, most-users, ...) without a measurable threshold.
- **No dangling concept reference (H14)** — every concept a requirement
  *references* must be *defined* by some admitted requirement or explicitly
  declared out of scope. This is the gap that let an undefined "team streak"
  slip into prd-team-app: R2 referenced it, no requirement defined it, and the
  prose gate admitted R2 anyway.
- **No impl depends_on a decision** — a build ticket may not ``depends_on`` a
  DECISION ticket (recognized by the ``architecture-decision`` tag OR af-intake-plan's
  ``meta.decision`` marker); and a decision ticket must be ``verify="manual"``
  (human-accepted), not a machine-built impl end-state.
  This rejects the D1–D5 dependency-inversion that wedged prd-sotos, where decision
  tickets sat first in build order but could only go green after the impl they gated.

Contradiction detection (zero unresolved contradictions) is delegated to Praxis
(`praxis_get_contradictions`) and is not re-implemented here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent_factory.gate import Reason, Verdict, register

# Stable rule-IDs (KTD5). Each emitted reason carries the constant for the rule that
# produced it, so coverage/harvesting attribute a verdict to a rule by field, not by
# parsing the message prose. These strings are part of the gate's public contract.
R_ACCEPT_BINARY = "R-ACCEPT-BINARY"      # every requirement maps to >=1 binary acceptance
R_NO_VAGUE = "R-NO-VAGUE"                # no unquantified vague term without a threshold
R_NO_DANGLING = "R-NO-DANGLING"          # every referenced concept is defined or out of scope
R_HAS_SOURCE = "R-HAS-SOURCE"            # every requirement carries its project source tag
R_NO_DANGLING_DEP = "R-NO-DANGLING-DEP"  # every depends_on target is a requirement in this plan
R_NO_DEP_CYCLE = "R-NO-DEP-CYCLE"        # the depends_on graph is acyclic (build order is realizable)
R_NO_IMPL_DEPENDS_ON_DECISION = "R-NO-IMPL-DEPENDS-ON-DECISION"  # no ticket may depends_on a decision
R_DECISION_NOT_END_STATE = "R-DECISION-NOT-END-STATE"           # a decision is human-accepted, not built
R_CONTRACT_SIGNED = "R-CONTRACT-SIGNED"  # a blessed plan carries a signed contract w/ evaluator actions

# A PURE ARCHITECTURE DECISION admitted as a requirement ticket MUST carry ONLY this neutral tag (so it
# resolves ZERO implementation checks) and be ``verify="manual"`` (a human accepts/overrides it at the
# gate). It must NEVER be a ``depends_on`` prerequisite of its own implementation ticket. Modeling a
# decision as a buildable, impl-tagged, automated-end-state ticket that its impl ticket depends_on is the
# D1–D5 anti-pattern: the decision sits topologically FIRST but can only go green LAST, so a fresh build's
# entire ready frontier is decisions that nothing can satisfy — a hard dependency inversion that wedges
# the run. These two rules reject that shape mechanically.
DECISION_TAG = "architecture-decision"

# A requirement's ``source`` must name the project's PRD (``prd-<project>``). When the
# gate is told the project, the tag must equal ``prd-<project>`` exactly; otherwise it
# must at least be a non-empty ``prd-...`` tag. This catches the generation-drift escape
# where requirements were tagged ``scope="team-app"`` with NO ``source="prd-team-app"``,
# so the Praxis completeness query (which filters ``source="prd-<project>"``) returned
# empty and the build wrongly believed everything was done.
SOURCE_RE = re.compile(r"^prd-.+")

# Vague qualifiers that must be replaced with a measurable threshold before a
# requirement is admitted. Matched as whole words/phrases, case-insensitively.
VAGUE_TERMS = (
    "fast",
    "quickly",
    "slow",
    "secure",
    "scalable",
    "performant",
    "robust",
    "reliable",
    "most users",
    "most-users",
    "user-friendly",
    "intuitive",
    "soon",
    "lots of",
)


@dataclass
class Requirement:
    """One requirement as the plan skill hands it to the gate.

    ``defines`` are the domain concepts this requirement introduces (lower-cased
    for matching); ``references`` are the concepts it depends on. The skill is
    responsible for populating these; the gate verifies their closure.
    """

    id: str
    text: str
    acceptance: str = ""
    defines: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    source: str = ""
    depends_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)     # identity tags (checks/ decision rules key off these)
    verify: str = ""                                  # "automated" | "manual" — a decision must be manual
    decision: str = ""                                # af-intake-plan's meta.decision marker (see DECISION_TAG)


# The gate's decision type is the shared contract :class:`Verdict` (reasons carry a
# structured ``rule_id``). ``GateVerdict`` is kept as a backward-compatible alias.
GateVerdict = Verdict


def _norm(concept: str) -> str:
    return concept.strip().lower()


def _vague_terms_in(text: str) -> list[str]:
    low = text.lower()
    return [t for t in VAGUE_TERMS if re.search(rf"\b{re.escape(t)}\b", low)]


def _find_dep_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle in the ``depends_on`` graph as an id path (``[A, B, A]``), or None if
    acyclic. Deterministic: nodes and edges are visited in plan order, so the same plan always
    reports the same cycle. Only edges to known nodes are present (dangling deps are a separate
    rule), so a cycle here is a genuine unrealizable build order, not a typo.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt) == GRAY:                  # back-edge into the current path == cycle
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt, BLACK) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        color[node] = BLACK
        stack.pop()
        return None

    for n in graph:                                     # dict preserves plan order
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def evaluate_plan(
    requirements: list[Requirement],
    out_of_scope: list[str] | None = None,
    project: str | None = None,
    contract: dict | None = None,
) -> Verdict:
    """Run the done-gate over a PRD's requirements; return admit/reject + reasons.

    Admits only when every rule passes for every requirement. Each violation
    contributes a structured :class:`Reason` (rule-ID + human-readable message) so the
    skill can report exactly what the human must fix and coverage can attribute the
    verdict to a rule. The admit/reject decision and message text are unchanged from the
    earlier string-reason form — only the reason carrier gained its ``rule_id`` field.

    ``project`` is the project the PRD belongs to. When given, every requirement's
    ``source`` must equal ``f"prd-{project}"`` exactly; when omitted, ``source`` must be a
    non-empty ``prd-...`` tag (``^prd-.+``). This is the ``R-HAS-SOURCE`` rule — a
    requirement that lacks its project source tag is REJECTED, so generation drift cannot
    slip a source-less plan past the gate and make the downstream completeness query
    (which filters ``source="prd-<project>"``) silently return empty.

    ``contract`` is the signed-contract evidence THREADED IN by the caller — this function
    stays PURE and never reads Praxis (feasibility finding: ``src/agent_factory`` has no
    client). ``tools/plan_gate_check.py`` reads the ``contract-signed`` episode via
    ``hooks/_praxis`` and passes ``{"signed": bool, "actions_recorded": bool}`` here. This is
    the ``R-CONTRACT-SIGNED`` rule: a blessed plan requires a signed contract whose evaluator
    ACTIONS were recorded (anti-Goodhart — the count is informational, not the gate). ``None``
    means the evidence was not supplied (the pure-eval / back-compat lane) and the rule stands
    down; an explicit dict gates on ``signed`` + ``actions_recorded``.
    """
    reasons: list[Reason] = []
    defined = {_norm(c) for r in requirements for c in r.defines}
    oos = {_norm(c) for c in (out_of_scope or [])}
    known = defined | oos
    expected_source = f"prd-{project}" if project is not None else None

    # Tickets that are pure architecture DECISIONS. A decision is human-accepted, not machine-built,
    # so (a) it must be verify="manual" (R-DECISION-NOT-END-STATE), and (b) nothing may depends_on it
    # (R-NO-IMPL-DEPENDS-ON-DECISION) — see DECISION_TAG for the anti-pattern this prevents.
    #
    # Recognize a decision by the neutral tag OR by af-intake-plan's ``meta.decision`` marker (values
    # like "default-flagged" / "human-decided-..."). Tag-ONLY recognition left a hole: the ORIGINAL
    # mistake — a decision admitted with IMPL tags (["cdk","cognito"]), verify="automated", an impl
    # end-state acceptance, depended_on by an impl ticket — carries no "architecture-decision" tag and so
    # slipped past both rules and ADMITted (the exact dependency-inversion they exist to reject). The
    # marker is stamped on every decision fact regardless of its tags, so it closes that hole.
    decision_ids = {
        r.id
        for r in requirements
        if DECISION_TAG in {_norm(t) for t in r.tags}
        or str(getattr(r, "decision", "")).strip()
    }

    for r in requirements:
        if not r.acceptance.strip():
            reasons.append(
                Reason(R_ACCEPT_BINARY, f"{r.id}: no binary acceptance condition")
            )

        if r.id in decision_ids and _norm(r.verify) != "manual":
            reasons.append(
                Reason(
                    R_DECISION_NOT_END_STATE,
                    f"{r.id}: architecture-decision ticket must be verify=\"manual\" (a human accepts "
                    f"or overrides the design at the gate), not machine-built with an impl end-state "
                    f"acceptance (got verify='{r.verify}'). Record the impl end-state on the "
                    f"implementation ticket instead.",
                )
            )

        src = r.source.strip()
        if expected_source is not None:
            source_ok = src == expected_source
        else:
            source_ok = bool(SOURCE_RE.match(src))
        if not source_ok:
            expected = expected_source if expected_source is not None else "prd-<project>"
            reasons.append(
                Reason(
                    R_HAS_SOURCE,
                    f"{r.id}: missing/!= project source "
                    f"(expected {expected}, got '{r.source}')",
                )
            )

        for term in sorted(set(_vague_terms_in(f"{r.text} {r.acceptance}"))):
            reasons.append(
                Reason(
                    R_NO_VAGUE,
                    f"{r.id}: vague term '{term}' without a measurable threshold",
                )
            )

        for ref in r.references:
            if _norm(ref) not in known:
                reasons.append(
                    Reason(
                        R_NO_DANGLING,
                        f"{r.id}: dangling reference to undefined concept '{ref}' "
                        f"(define it in a requirement or declare it out of scope)",
                    )
                )

    # --- Dependency-DAG closure (the build-order graph af-build's next_ready_ticket walks). A
    # depends_on edge naming a requirement not in this plan is unrealizable (the prerequisite can
    # never finish), and a cycle means no ticket is ever ready — both are stalls the build loop
    # would otherwise discover only at run time, so the plan gate rejects them up front.
    req_ids = {r.id for r in requirements}
    dep_graph: dict[str, list[str]] = {}
    for r in requirements:
        present: list[str] = []
        for dep in r.depends_on:
            if dep in decision_ids:
                # A build ticket may NEVER be gated by a decision — the decision is baked into this
                # ticket's own content/acceptance, and impl tickets depend only on real build
                # prerequisites (producer -> consumer, entity -> its surfaces, infra -> first user).
                reasons.append(
                    Reason(
                        R_NO_IMPL_DEPENDS_ON_DECISION,
                        f"{r.id}: depends_on '{dep}' which is an architecture-decision ticket "
                        f"(a decision must never gate a build ticket — it sits first but goes green "
                        f"last, wedging the run). Bake the decision into this ticket's "
                        f"content/acceptance and drop the edge.",
                    )
                )
            if dep not in req_ids:
                reasons.append(
                    Reason(
                        R_NO_DANGLING_DEP,
                        f"{r.id}: depends_on '{dep}' which is not a requirement in this plan "
                        f"(add the prerequisite or fix the edge)",
                    )
                )
            else:
                present.append(dep)
        dep_graph[r.id] = present

    cycle = _find_dep_cycle(dep_graph)
    if cycle:
        reasons.append(
            Reason(
                R_NO_DEP_CYCLE,
                f"dependency cycle: {' -> '.join(cycle)} "
                f"(no ticket in the cycle can ever be ready; break it)",
            )
        )

    # --- R-CONTRACT-SIGNED (plan-level). A blessed plan must carry a signed contract whose evaluator
    # ACTIONS (cuts/merges/additions) were recorded — the anti-Goodhart bless predicate (KTD3). The
    # evidence is threaded IN by the caller (plan_gate_check reads the contract-signed episode); this
    # pure function never reads Praxis. ``contract=None`` = evidence not supplied (pure-eval lane) ->
    # rule stands down. An explicit dict gates: unsigned rejects; signed-but-no-actions (a padded count)
    # rejects; the raw n_assertions count is informational only, never the gate.
    if contract is not None:
        if not bool(contract.get("signed")):
            reasons.append(
                Reason(
                    R_CONTRACT_SIGNED,
                    "plan has no signed contract — an evaluator (separate from the planner) must "
                    "adversarially cut/merge/tighten the testable assertions and SIGN the result "
                    "(a contract-signed episode). Run the intake negotiation + signing step.",
                )
            )
        elif not bool(contract.get("actions_recorded")):
            reasons.append(
                Reason(
                    R_CONTRACT_SIGNED,
                    "contract is signed but records NO evaluator actions (cuts/merges/additions) — a "
                    "signature over an unchanged draft is a padded-count Goodhart target, not real "
                    "adversarial review. The evaluator must actually falsify/cut/merge/tighten "
                    "assertions before signing.",
                )
            )

    return Verdict(admitted=not reasons, reasons=reasons)


class PlanGate:
    """The plan done-gate as a :class:`~agent_factory.gate.Gate` implementation.

    ``evaluate`` accepts a component ``input`` block (the case ``input``: a list of
    ``requirements`` and optional ``out_of_scope``), builds :class:`Requirement` objects,
    and delegates to :func:`evaluate_plan`. Registered under ``"plan_gate"`` so the eval
    harness reaches it only via the registry.
    """

    def evaluate(self, input: dict) -> Verdict:  # noqa: A002 - contract name
        requirements = [
            Requirement(
                id=r["id"],
                text=r.get("text", ""),
                acceptance=r.get("acceptance", ""),
                defines=r.get("defines", []),
                references=r.get("references", []),
                source=r.get("source", ""),
                depends_on=r.get("depends_on", []),
                tags=r.get("tags", []),
                verify=r.get("verify", ""),
                # The decision marker may ride at the top level of the case input or inside meta —
                # accept either so neither the case author nor the live fact→Requirement mapper can
                # drop it on the way in (dropping it would defeat the item-1 fix).
                decision=r.get("decision") or (r.get("meta") or {}).get("decision", ""),
            )
            for r in input.get("requirements", [])
        ]
        return evaluate_plan(
            requirements,
            out_of_scope=input.get("out_of_scope", []),
            project=input.get("project"),
            # The signed-contract evidence, when a case supplies it. Absent -> None -> the
            # R-CONTRACT-SIGNED rule stands down (existing cases are unaffected).
            contract=input.get("contract"),
        )


register("plan_gate", PlanGate())
