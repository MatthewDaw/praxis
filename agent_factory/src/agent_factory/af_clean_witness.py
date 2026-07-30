"""af-clean's witness-tiered applier gate (R13 / B22, B12+D2).

The cleaner is not the applier (B22): a candidate finding may only become an applied diff when
its own evidence clears a required **witness tier**, never on the strength of the rule that
produced it alone. The three witness tiers, weakest to strongest:

1. ``TIER_NAMED_TOOL`` — a named tool's located finding.
2. ``TIER_AST_CORPUS_EXECUTION`` — AST no-reference **and** absence from the B6 string-dispatch
   corpus **and** execution evidence over a named observation window.
3. ``TIER_SURVIVED_TOMBSTONE`` — a survived tombstone.

No witness (``witness_tier=None``), or a witness below the required tier, downgrades the proposal
to ``"report"`` — it is never applied.

**The aggression dial is pinned.** ``DEFAULT_REQUIRED_WITNESS_TIER`` is tier 2 — B22's hard floor
("the dial ... may never go below tier 2") — and :func:`decide` takes no caller-supplied tier
argument, so there is no lowering mechanism in v1.

**Must-not-happen guards are refused at every dial setting**, independent of witness tier: a
symbol present in the B6 corpus (R6), an unbound test deletion (B18 requires it be bound to a
same-unit symbol deletion), and blame-flagged defensive code (R23/B21, a "scar").

**D2 (same-job rule ceiling vs. instance):** a rule's declared ``tier_ceiling`` is the highest
tier ANY of its findings may reach (B12) — ``"advise"`` never applies regardless of witness. For a
rule whose instances can be either a lexical or a semantic-only match (e.g. same-job
consolidation), a ``match_kind="semantic"`` instance additionally requires human confirmation and
is held to ``"report"`` even when its ``tier_ceiling`` is ``"enforce"`` and its witness clears the
required tier; a ``match_kind="lexical"`` instance (or a rule with no lexical/semantic split at
all, ``match_kind=None``) is unaffected by this rule.

This module is PURE: no I/O, no LLM calls. It only decides, given evidence already gathered by
other af-clean modules (:mod:`agent_factory.af_clean_string_corpus`,
:mod:`agent_factory.af_clean_scar_detection`, B17/B18's coverage classification), whether a
proposal may be applied.
"""

from __future__ import annotations

from dataclasses import dataclass

# Witness tiers (B22), weakest to strongest.
TIER_NAMED_TOOL = 1
TIER_AST_CORPUS_EXECUTION = 2
TIER_SURVIVED_TOMBSTONE = 3

# The aggression dial. Pinned at B22's hard floor (tier 2); v1 has no mechanism to lower it.
DEFAULT_REQUIRED_WITNESS_TIER = TIER_AST_CORPUS_EXECUTION

APPLY = "apply"
REPORT = "report"


@dataclass(frozen=True)
class Proposal:
    """One candidate finding, with the evidence the witness gate needs to decide its fate.

    ``tier_ceiling`` is the rule's declared highest reachable tier (B12): ``"enforce"`` may
    auto-apply, ``"advise"`` may never leave ``"report"``. ``match_kind`` distinguishes a lexical
    from a semantic-only instance of a same-job-style rule (D2); ``None`` for rules with no such
    split. ``witness_tier`` is the highest witness tier this instance's own evidence actually
    satisfies (``None`` = no witness at all). ``is_scar``, ``corpus_present``, and the test-deletion
    pair carry the §3.1 must-not-happen guards, each refused unconditionally regardless of tier.
    """

    rule: str
    tier_ceiling: str
    match_kind: str | None = None
    witness_tier: int | None = None
    is_scar: bool = False
    corpus_present: bool = False
    is_test_deletion: bool = False
    bound_to_symbol_deletion: bool = False


@dataclass(frozen=True)
class Decision:
    """The gate's verdict: ``"apply"`` or ``"report"``, with a human-readable reason."""

    action: str
    reason: str


def _apply(reason: str) -> Decision:
    return Decision(APPLY, reason)


def _report(reason: str) -> Decision:
    return Decision(REPORT, reason)


def decide(proposal: Proposal) -> Decision:
    """Decide whether ``proposal`` may be applied, or is downgraded to report-only.

    Evaluated in order: the must-not-happen guards (refused at every dial setting), the rule's
    tier ceiling, D2's semantic-instance human-confirmation requirement, then the witness tier
    against the pinned :data:`DEFAULT_REQUIRED_WITNESS_TIER`. There is no ``required_tier``
    parameter — the dial is not adjustable from this call site.
    """
    if proposal.is_scar:
        return _report(
            f"{proposal.rule}: blame-flagged defensive code (scar) is refused at every dial setting"
        )

    if proposal.corpus_present:
        return _report(
            f"{proposal.rule}: symbol present in the B6 string-dispatch corpus is refused "
            "unconditionally"
        )

    if proposal.is_test_deletion and not proposal.bound_to_symbol_deletion:
        return _report(
            f"{proposal.rule}: unbound test deletion is refused unconditionally (B18 requires "
            "binding to a same-unit symbol deletion)"
        )

    if proposal.tier_ceiling != "enforce":
        return _report(
            f"{proposal.rule}: rule ceiling is {proposal.tier_ceiling!r}, never auto-applied"
        )

    if proposal.match_kind == "semantic":
        return _report(
            f"{proposal.rule}: semantic-only match requires human confirmation (D2), held at report"
        )

    if proposal.witness_tier is None or proposal.witness_tier < DEFAULT_REQUIRED_WITNESS_TIER:
        return _report(
            f"{proposal.rule}: no witness at the required tier ({DEFAULT_REQUIRED_WITNESS_TIER}) "
            "-- downgraded to report"
        )

    return _apply(
        f"{proposal.rule}: witness tier {proposal.witness_tier} clears the required tier "
        f"{DEFAULT_REQUIRED_WITNESS_TIER}"
    )
