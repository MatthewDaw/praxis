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
- **Device closed set** — af-intake-plan stamps ``meta.device`` on every ticket to name the
  concurrency lane (``cpu``/``gpu``) it counts against af-build's admission caps. An absent
  value is treated as ``cpu`` (so an already-blessed plan authored before this rule keeps
  passing); a value outside the closed set rejects, naming the offending ticket.

Contradiction detection (zero unresolved contradictions) is delegated to Praxis
(`praxis_get_contradictions`) and is not re-implemented here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent_factory.contract_signature import actions_recorded, is_signed
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
R_EXTERNAL_STATE_LIVE = "R-EXTERNAL-STATE-NEEDS-LIVE-CHECK"  # a ticket claiming external state needs a
#                                                              check that queries that system for real
R_DEVICE_CLOSED_SET = "R-DEVICE-CLOSED-SET"  # meta.device (default "cpu") must be in the closed set

# The closed set of concurrency lanes a ticket's ``meta.device`` may name. af-build admits its
# dependency-ready frontier under one cap per lane (``max_cpu_parallel`` / ``max_gpu_parallel``) —
# see R15; a value outside this set cannot be routed to any cap, so it is rejected rather than
# silently defaulted.
DEVICE_CLOSED_SET = frozenset({"cpu", "gpu"})
DEFAULT_DEVICE = "cpu"

# A ticket that claims state OUTSIDE the repo — an object in a bucket, a provisioned instance, a
# serving endpoint — cannot be proven by anything that runs entirely inside the repo. Mocked clients,
# fixtures and constants all demonstrate the code's SHAPE while the external world stays untouched.
#
# Why this rule exists. On a real plan, five tickets reached build_state="finished" this way and not
# one of them had done its job: three acquisition tickets went green against a moto-mocked S3 with no
# bucket in the account; a UI ticket claiming a service that "binds to localhost" passed with no server
# in the tree; a reconstruction ticket returned its "measured" reprojection error from a module
# constant. Every acceptance condition was honest prose. Every check verified inputs the ticket itself
# invented. The gap was structural, so the fix is too: if a ticket's own words claim external state,
# SOMETHING it resolves must go and look.
# Two tiers, because a single flat vocabulary produced false positives immediately. Run across four
# unrelated plans, the flat version flagged af-clean — a TEXT-ANALYSIS project with no cloud surface —
# on the sentence "one rule yields an auto-appliable lexical instance and a report-only semantic
# instance". "instance", "queue", "topic", "endpoint" and "lambda" are ordinary technical English
# before they are infrastructure.
#
# STRONG terms name a specific external system and stand alone.
_EXTERNAL_STRONG_RE = re.compile(
    r"\b("
    r"s3|ec2|rds|dynamodb|cloudfront|api gateway|object storage|"
    r"bucket|uploads? to|uploaded to|transferred to|binds? to|listens? on|"
    r"dns record|https?://"
    r")\b",
    re.IGNORECASE,
)
# WEAK terms are infrastructure ONLY in an infrastructure context, so they need corroboration —
# either a STRONG term elsewhere in the same ticket, or an infra-ish identity tag.
_EXTERNAL_WEAK_RE = re.compile(
    r"\b("
    r"instance|provision(?:ed|s|ing)?|terminat(?:e|ed|es|ion)|"
    r"deploy(?:ed|s|ment)?|endpoint|serves?|serving|queue|topic|lambda|dns"
    r")\b",
    re.IGNORECASE,
)
# Tags that make a WEAK term count. Identity the plan already carries — no new authoring burden.
_INFRA_TAGS = frozenset({
    "infrastructure", "deployment", "deploy", "aws", "cloud", "ec2", "s3", "iam",
    "terraform", "cdk", "networking", "compute", "provisioning", "acquisition", "hosting",
})
# Commands that actually leave the process and touch something. A check whose run is only pytest/ruff/
# mypy/grep proves the tree, never the world.
_LIVE_COMMAND_RE = re.compile(
    r"\b(aws|boto3|rclone|curl|wget|gcloud|az|kubectl|terraform|psql|nc|dig|http)\b",
    re.IGNORECASE,
)
# ...but a "live" command running against a mocked or local-stub endpoint proves nothing either.
#
# Matched as USAGE, not as vocabulary. A first cut scanned for the bare word "moto" anywhere in the
# command and disqualified a genuine `aws s3 ls` check whose own FAILURE MESSAGE read "a moto mock
# does not satisfy this check" — the check was right, its prose mentioned the thing it rules out, and
# the rule punished it for saying so. Anchor on how a mock is actually invoked instead.
_MOCKED_RE = re.compile(
    r"(--with\s+moto|\bimport\s+moto\b|\bfrom\s+moto\b|\bmock_aws\b|\bmoto\.server\b"
    r"|\blocalstack\b|\bhttpretty\b|responses\.activate"
    r"|endpoint[-_]url[= ]\s*[\"']?https?://(localhost|127\.0\.0\.1))",
    re.IGNORECASE,
)

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
    device: str = ""                                  # af-intake-plan's meta.device; "" == DEFAULT_DEVICE ("cpu")


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


def _contract_evidence(contract: object) -> tuple[bool, bool] | None:
    """Normalize threaded contract evidence to ``(signed, actions_recorded)``, or ``None`` when NO
    usable contract evidence was supplied (absent / empty / not a dict).

    Two shapes are accepted, so a caller can hand over whichever it has:

    - the REDUCED form ``{"signed": bool, "actions_recorded": bool}`` that
      ``tools/plan_gate_check.read_contract`` produces from the episode log; and
    - a RAW ``contract_signature.build_signed_payload`` dict (or a read-back fact wrapping one in
      ``meta.episode``), validated by the pure ``is_signed`` / ``actions_recorded`` helpers — so a
      payload whose ``kind`` is not ``contract-signed`` reads as UNSIGNED rather than as valid.

    ``None`` (the absent verdict) is what makes the rule fail CLOSED: the caller supplied nothing to
    check, which is a rejection, not a stand-down.
    """
    if not isinstance(contract, dict) or not contract:
        return None
    if "signed" in contract or "actions_recorded" in contract:
        return bool(contract.get("signed")), bool(contract.get("actions_recorded"))
    return is_signed(contract), actions_recorded(contract)


def evaluate_plan(
    requirements: list[Requirement],
    out_of_scope: list[str] | None = None,
    project: str | None = None,
    contract: dict | None = None,
    checks: list[dict] | None = None,
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
    ``hooks/_praxis`` and passes ``{"signed": bool, "actions_recorded": bool}`` here; a raw
    ``build_signed_payload`` dict (``kind``/``n_assertions``/``actions``/``signer``) is also
    accepted. This is the ``R-CONTRACT-SIGNED`` rule: a blessed plan requires a signed contract
    whose evaluator ACTIONS were recorded (anti-Goodhart — the count is informational, never the
    gate).

    The rule FAILS CLOSED on missing evidence: ``contract=None``, ``{}``, a non-dict, or a
    payload whose ``kind`` is not ``contract-signed`` all REJECT with ``R-CONTRACT-SIGNED``.
    A plan that never had a contract negotiated at all is strictly MORE dangerous than one
    signed lazily, so "no evidence supplied" can never be an admit path for a HARD bless
    predicate (af-intake-plan's B9 must be unclearable while the gate rejects). The absent
    case carries its own message so the operator can tell "no contract at all" apart from
    "signed with no evaluator actions".
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

        device = _norm(r.device) or DEFAULT_DEVICE
        if device not in DEVICE_CLOSED_SET:
            reasons.append(
                Reason(
                    R_DEVICE_CLOSED_SET,
                    f"{r.id}: meta.device '{r.device}' is outside the closed set "
                    f"({sorted(DEVICE_CLOSED_SET)}) — name the concurrency lane this ticket counts "
                    f"against, or drop the field to default to '{DEFAULT_DEVICE}'",
                )
            )

    # --- R-EXTERNAL-STATE-NEEDS-LIVE-CHECK. Stands down entirely when the caller threads no
    # checks (keeps evaluate_plan pure and every existing caller/test unchanged); fires only on an
    # automated ticket whose own text claims external state.
    if checks is not None:
        live_tags: set[str] = set()
        live_any = False
        for c in checks:
            meta = c if "run" in c else (c.get("meta") or {})
            run = str(meta.get("run") or "")
            if not run or not _LIVE_COMMAND_RE.search(run) or _MOCKED_RE.search(run):
                continue
            applies = [_norm(a) for a in (meta.get("applies_to") or [])]
            if "*" in applies:
                live_any = True
            live_tags.update(a for a in applies if a and a != "*")
        for r in requirements:
            if r.id in decision_ids or _norm(r.verify) == "manual":
                continue
            blob = f"{r.text} {r.acceptance}"
            claim = _EXTERNAL_STRONG_RE.search(blob)
            # A weak term counts only in an infrastructure context: an infra identity tag on the
            # ticket. Without that corroboration it is ordinary technical English and is ignored.
            if claim is None and {_norm(x) for x in r.tags} & _INFRA_TAGS:
                claim = _EXTERNAL_WEAK_RE.search(blob)
            if not claim:
                continue
            if live_any or ({_norm(t) for t in r.tags} & live_tags):
                continue
            reasons.append(
                Reason(
                    R_EXTERNAL_STATE_LIVE,
                    f"{r.id}: claims external state ('{claim.group(0)}') but resolves no check whose "
                    f"command queries that system. A mocked client, a fixture or a constant proves the "
                    f"code's shape while the external world stays untouched — author a check whose run "
                    f"actually looks (aws/rclone/curl/...), or mark the ticket verify=\"manual\".",
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
    # pure function never reads Praxis. The rule FAILS CLOSED: absent/empty/wrong-kind evidence rejects
    # (its own message), unsigned rejects, signed-but-no-actions (a padded count) rejects; the raw
    # n_assertions count is informational only, never the gate.
    evidence = _contract_evidence(contract)
    if evidence is None:
        reasons.append(
            Reason(
                R_CONTRACT_SIGNED,
                "plan carries NO contract evidence at all — no contract-signed episode was supplied "
                "to the gate, so the adversarial evaluator step never ran. This is a HARD bless "
                "predicate and fails CLOSED: an un-negotiated plan is more dangerous than a lazily "
                "signed one. Run the intake negotiation + signing step, then re-run the gate.",
            )
        )
    else:
        signed, acted = evidence
        if not signed:
            reasons.append(
                Reason(
                    R_CONTRACT_SIGNED,
                    "plan has no signed contract — the supplied evidence is unsigned or malformed "
                    "(not a well-formed contract-signed payload with a signer). An evaluator "
                    "(separate from the planner) must adversarially cut/merge/tighten the testable "
                    "assertions and SIGN the result (a contract-signed episode). Run the intake "
                    "negotiation + signing step.",
                )
            )
        elif not acted:
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

    def evaluate(self, input: dict) -> Verdict:
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
                device=r.get("device") or (r.get("meta") or {}).get("device", ""),
            )
            for r in input.get("requirements", [])
        ]
        return evaluate_plan(
            requirements,
            out_of_scope=input.get("out_of_scope", []),
            project=input.get("project"),
            # The signed-contract evidence, when a case supplies it. Absent -> None -> the
            # R-CONTRACT-SIGNED rule REJECTS (it fails closed), so a case that means to exercise
            # another rule's pass-path must supply a valid contract explicitly.
            contract=input.get("contract"),
            # Declared build-validation checks, threaded the same way as the contract so
            # evaluate_plan stays pure. Absent -> None -> R-EXTERNAL-STATE-NEEDS-LIVE-CHECK
            # stands down, which keeps every pre-existing case's verdict unchanged.
            checks=input.get("checks"),
        )


register("plan_gate", PlanGate())
