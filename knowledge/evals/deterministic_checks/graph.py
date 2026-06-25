"""Deterministic checks over the seeded knowledge graph (ingestion guardrails).

Unlike the :mod:`text` / :mod:`builds` checks (which inspect the agent's output
artifact), these assert that the *seed* actually populated the graph. The only
graph-derived signal a check can see is :attr:`EvalContext.injected_knowledge` —
the reader's output, i.e. the ``active`` fact texts the agent was shown (joined
by blank lines by the whole-file reader / graph ``read``). That makes it a faithful
proxy for "what landed active and retrievable": a no-op ingestor (zero active
image cards, zero active Wikipedia facts) shows up here as missing blocks, so
these checks FAIL the eval instead of letting the gap pass silently.

Each takes the :class:`EvalContext` plus the case's ``params`` and returns a
:class:`CheckResult`.
"""

from __future__ import annotations

from knowledge.evals.eval_def import CheckResult, EvalContext

# Image/asset cards from the ImageIngestor carry the ``assets/<file>`` reference
# in their card text (see image_injestor.py: "path=assets/" convention).
_ASSET_MARKER = "path=assets/"


def _active_blocks(ctx: EvalContext) -> list[str]:
    """The active fact blocks the reader injected, split on the blank-line join.

    The whole-file reader concatenates active fact texts with ``\\n\\n`` (graph
    ``read``); split on that and drop empties.
    """
    raw = ctx.injected_knowledge or ""
    return [b.strip() for b in raw.split("\n\n") if b.strip()]


def min_active_asset_cards(ctx: EvalContext, *, minimum: int = 1) -> CheckResult:
    """Pass iff at least ``minimum`` active image/asset cards were seeded.

    Counts injected fact blocks bearing the ImageIngestor's ``path=assets/``
    marker. Guards against the image ingestor producing ZERO active asset cards
    (a silent no-op the rubric/text checks can't catch).
    """
    cards = [b for b in _active_blocks(ctx) if _ASSET_MARKER in b]
    ok = len(cards) >= minimum
    return CheckResult(
        name="min_active_asset_cards",
        passed=ok,
        evidence=(
            f"{len(cards)} active asset card(s) (need >= {minimum})"
            if ok
            else f"only {len(cards)} active asset card(s) injected (need >= {minimum}); "
            "image ingestion produced no retrievable asset cards"
        ),
    )


def at_most_one_active(
    ctx: EvalContext, *, texts: list[str], winner: str | None = None
) -> CheckResult:
    """FR-005 guard: of a mutually-contradictory ``texts`` pair, never more than one
    is active.

    Active facts are read from ``injected_knowledge`` (the reader's output). The
    offline ``FakeRunner`` injects nothing, so with no injected knowledge the check
    is not applicable and passes -- it bites only on a real run that shows the agent
    the seeded graph, where the write policy's FR-005 enforcement should have
    demoted the losing side to ``proposed`` (hence out of the active read). With
    ``winner`` set, a single active side must be that text (the seed that wins the
    tie / stays live).
    """
    blocks = _active_blocks(ctx)
    if not blocks:
        return CheckResult(
            name="at_most_one_active",
            passed=True,
            evidence="no injected knowledge (live-run check; not applicable offline)",
        )
    live = [t for t in texts if any(t.strip() in b for b in blocks)]
    if len(live) > 1:
        return CheckResult(
            name="at_most_one_active",
            passed=False,
            evidence=f"FR-005 violated: {len(live)} contradictory facts are both active: {live!r}",
        )
    if winner is not None and live and live != [winner]:
        return CheckResult(
            name="at_most_one_active",
            passed=False,
            evidence=f"the live side is {live!r}, expected the winner {winner!r}",
        )
    return CheckResult(
        name="at_most_one_active",
        passed=True,
        evidence=f"<= 1 of the contradictory pair is active: {live!r}",
    )


def single_merged_fact(
    ctx: EvalContext, *, mentions: list[str], max_blocks: int = 1
) -> CheckResult:
    """Mem0 UPDATE/merge guard: the graph ends as ONE fact mentioning every term.

    For the knowledge_graph component, ``ctx.output`` is every stored fact text
    joined by blank lines (plus any ``CONTRADICTION:`` summary lines). Two related-
    additive notes should collapse to a SINGLE merged fact whose text contains all
    of ``mentions`` (e.g. both "cheese" and "chicken") — not two separate facts and
    not a flagged contradiction. Passes iff: at most ``max_blocks`` non-contradiction
    fact blocks exist, no ``CONTRADICTION:`` line is present, and exactly one block
    mentions all the required terms.
    """
    blocks = [b.strip() for b in ctx.output.split("\n\n") if b.strip()]
    fact_blocks = [b for b in blocks if not b.startswith("CONTRADICTION:")]
    has_contradiction = any("CONTRADICTION:" in b for b in blocks)
    merged = [b for b in fact_blocks if all(m.lower() in b.lower() for m in mentions)]
    ok = (
        not has_contradiction
        and len(fact_blocks) <= max_blocks
        and len(merged) == 1
    )
    if ok:
        evidence = f"single merged fact mentions {mentions!r}: {merged[0]!r}"
    elif has_contradiction:
        evidence = "a CONTRADICTION was flagged (expected an additive merge, not a clash)"
    elif len(fact_blocks) > max_blocks:
        evidence = (
            f"{len(fact_blocks)} fact blocks remain (expected <= {max_blocks}); "
            f"the additive notes were not merged: {fact_blocks!r}"
        )
    else:
        evidence = f"no single fact mentions all of {mentions!r}; blocks={fact_blocks!r}"
    return CheckResult(name="single_merged_fact", passed=ok, evidence=evidence)


def min_non_seed_facts(
    ctx: EvalContext, *, minimum: int = 1, seed_texts: list[str] | None = None
) -> CheckResult:
    """Pass iff at least ``minimum`` active facts are neither seed nor asset cards.

    ``seed_texts`` is the case's hand-authored ``direct_to_graph`` facts (passed
    verbatim by the case). An injected active block that is not one of those and
    not an asset card must have come from the ``via_ingestor`` text (the Wikipedia
    article) — i.e. a retrievable, non-seed text fact. Guards against the text
    ingestor contributing ZERO retrievable Wikipedia-derived facts.
    """
    seeds = {s.strip() for s in (seed_texts or [])}
    derived = [
        b
        for b in _active_blocks(ctx)
        if _ASSET_MARKER not in b and b not in seeds
    ]
    ok = len(derived) >= minimum
    return CheckResult(
        name="min_non_seed_facts",
        passed=ok,
        evidence=(
            f"{len(derived)} active non-seed text fact(s) (need >= {minimum})"
            if ok
            else f"only {len(derived)} active non-seed text fact(s) injected (need >= {minimum}); "
            "text ingestion contributed no retrievable Wikipedia-derived facts"
        ),
    )
