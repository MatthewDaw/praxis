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

import uuid

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


def distinct_fact_blocks(
    ctx: EvalContext, *, min_blocks: int = 2
) -> CheckResult:
    """Slot-guard guard (loss point B): distinct facts must NOT collapse into one.

    Mirror of :func:`single_merged_fact`. For the knowledge_graph component,
    ``ctx.output`` is every stored fact text joined by blank lines (plus any
    ``CONTRADICTION:`` summary lines). Two distinct-but-overlapping rules must
    survive as SEPARATE fact blocks — a silent over-merge concatenates them into one
    block, which ``requires_all_substrings`` alone would not catch (both fragments
    still appear in the merged text). Passes iff at least ``min_blocks`` non-
    contradiction fact blocks remain.
    """
    blocks = [b.strip() for b in ctx.output.split("\n\n") if b.strip()]
    fact_blocks = [b for b in blocks if not b.startswith("CONTRADICTION:")]
    ok = len(fact_blocks) >= min_blocks
    return CheckResult(
        name="distinct_fact_blocks",
        passed=ok,
        evidence=(
            f"{len(fact_blocks)} distinct fact block(s) (need >= {min_blocks})"
            if ok
            else f"only {len(fact_blocks)} fact block(s) (need >= {min_blocks}); "
            f"distinct facts were over-merged into one: {fact_blocks!r}"
        ),
    )


def retrieves_fact_for_query(
    ctx: EvalContext,
    *,
    seed_facts: list[str],
    query: str,
    expect_substring: str,
    top_k: int = 3,
) -> CheckResult:
    """Pass iff ``search(query, top_k)`` over ``seed_facts`` surfaces ``expect_substring``.

    This is the hybrid-retrieval (vector + BM25 via RRF) regression check. It
    drives the *real* user-facing retrieval path on the Postgres store: it seeds
    ``seed_facts`` as active facts into a fresh, isolated ``(org_id, user_id)``
    tenant, runs ``search`` with the cached (offline, deterministic) embedder, and
    asserts the fact containing ``expect_substring`` lands in the top-``top_k`` hits.

    The case is built so the keyword fact ranks OUT of top-k under pure pgvector
    cosine (RED on the unmodified store) and IN once a BM25 keyword branch is fused
    in with Reciprocal Rank Fusion (GREEN). Deterministic and offline: the cached
    embedder replays committed real vectors (recording misses only with a key), and
    Postgres full-text ranking is fully deterministic.

    Requires a reachable Postgres DSN (the case declares ``embedder: cached`` /
    ``substrate: vector``); without one the harness SKIPs the case before it runs.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    # Resolve the same cached embedder the harness wires for an ``embedder: cached``
    # case (committed real vectors, deterministic offline).
    class _CachedAxis:
        embedder = "cached"

    embedder = _eval_embedder(_CachedAxis())

    conn = db.connect()
    db.bootstrap()  # ensure the tsvector column / GIN index exist on this DB
    org = "eval_hybrid_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn,
        org,
        user,
        embedder=embedder,
        # Distinct seed texts must coexist (no overwrite/merge collapse): a plain
        # redact + exact-dedup policy keeps each fact as its own active row.
        policy=[Redactor(), Deduper()],
    )
    try:
        for text in seed_facts:
            graph.write(text, state="active")
        # Hybrid is opt-in (default off): this check exercises the keyword-fusion
        # path explicitly, since that is the capability under test.
        hits = graph.search(query, top_k=top_k, hybrid=True)
        texts = [h.fact.text for h in hits]
        found = any(expect_substring in t for t in texts)
        return CheckResult(
            name="retrieves_fact_for_query",
            passed=found,
            evidence=(
                f"top-{top_k} for {query!r} includes a fact with {expect_substring!r}: {texts!r}"
                if found
                else f"top-{top_k} for {query!r} MISSED {expect_substring!r}; got {texts!r}"
            ),
        )
    finally:
        # Drop the throwaway tenant so the live store isn't polluted across runs.
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


def retrieval_prefers_proven_over_failed(
    ctx: EvalContext,
    *,
    query: str,
    proven_text: str,
    failed_text: str,
    proven_successes: int = 5,
    failed_failures: int = 5,
    top_k: int = 2,
) -> CheckResult:
    """Outcome/trust-feedback spec: a fact whose advice demonstrably FAILED must not
    outrank a PROVEN fact for the same query.

    Seeds two competing, both-``active`` facts answering the same question into a
    fresh isolated tenant:

      * ``failed_text`` — an approach that was tried and repeatedly failed. It is
        written to be *more lexically/semantically similar to the query* (it echoes
        the query's wording), so pure-similarity retrieval ranks it first.
      * ``proven_text`` — the approach that actually worked, phrased differently.

    The outcome history is fed back the way the factory loop would: ``record_outcome``
    logs ``failed_failures`` failures on the failed fact and ``proven_successes``
    successes on the proven one. The check then runs the real ``search`` and asserts
    the proven fact ranks ABOVE the failed one.

    Without outcome/trust weighting this FAILS: ``search`` ranks purely by cosine
    (+ optional BM25), so the failed approach — being more query-similar — wins, and
    no recorded outcome can change that. With the utility multiplier folded into
    ranking, the repeatedly-failed fact decays below the proven one and this PASSES.

    Requires a reachable Postgres DSN (``embedder: cached`` / ``substrate: vector``);
    without one the harness SKIPs the case.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    embedder = _eval_embedder(_CachedAxis())

    conn = db.connect()
    db.bootstrap()
    org = "eval_outcome_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=embedder, policy=[Redactor(), Deduper()]
    )
    try:
        proven_id = graph.write(proven_text, state="active")
        failed_id = graph.write(failed_text, state="active")
        # Feed verification outcomes back the way the factory loop would.
        for _ in range(proven_successes):
            graph.record_outcome(proven_id, success=True)
        for _ in range(failed_failures):
            graph.record_outcome(failed_id, success=False)
        hits = graph.search(query, top_k=top_k)
        ranked = [h.fact.text for h in hits]
        proven_rank = ranked.index(proven_text) if proven_text in ranked else 1_000
        failed_rank = ranked.index(failed_text) if failed_text in ranked else 1_000
        ok = proven_rank < failed_rank
        return CheckResult(
            name="retrieval_prefers_proven_over_failed",
            passed=ok,
            evidence=(
                f"proven fact ({proven_successes} successes) ranks #{proven_rank} vs "
                f"failed fact ({failed_failures} failures) #{failed_rank} for {query!r}"
                + (
                    ""
                    if ok
                    else " — retrieval surfaced the demonstrably-failed advice first "
                    "(outcome/trust not weighted in ranking)"
                )
            ),
        )
    finally:
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


def derivation_surfaces_stale_when_source_invalidated(
    ctx: EvalContext,
    *,
    source_text: str,
    derived_text: str,
) -> CheckResult:
    """H5 (derivation edges) red spec: when a source fact is invalidated, a learning
    derived from it must surface as suspect.

    Seeds a source fact and a learning derived from it (both ``active``) in a fresh
    isolated tenant, records the derivation as a ``derived_from`` edge (the
    ``fact_edges`` storage already supports arbitrary edge kinds via ``add_edge``),
    then rejects the source. The learning is now built on retired knowledge.

    Asserts the graph can name it: ``graph.stale_derived()`` returns the learning.

    RED today: there is no derivation traversal / propagation — ``stale_derived``
    does not exist (treated here as "nothing surfaced"), so the learning stays active
    and untraced after its source is rejected. GREEN once H5 adds the traversal +
    the stale-derived surface.

    Requires a Postgres DSN (``embedder: cached`` / ``substrate: vector``); the
    harness SKIPs the case without one.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    embedder = _eval_embedder(_CachedAxis())
    conn = db.connect()
    db.bootstrap()
    org = "eval_deriv_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=embedder, policy=[Redactor(), Deduper()]
    )
    try:
        source_id = graph.write(source_text, state="active")
        derived_id = graph.write(derived_text, state="active")
        # Record the derivation (fact_edges already supports arbitrary kinds).
        graph.add_edge(derived_id, source_id, "derived_from")
        # The source is found to be wrong and retired — through the reject chokepoint
        # (set_state), which fires the H5 propagation hook.
        graph.set_state(source_id, "rejected")
        # H5 surface: learnings whose derivation source was invalidated.
        try:
            stale_ids = [f.id for f in graph.stale_derived()]
        except AttributeError:
            stale_ids = []  # not implemented yet -> nothing surfaced -> RED
        ok = derived_id in stale_ids
        return CheckResult(
            name="derivation_surfaces_stale_when_source_invalidated",
            passed=ok,
            evidence=(
                "derived learning surfaced as stale after its source was rejected"
                if ok
                else "source rejected but the derived learning was NOT surfaced as stale "
                "(no derivation traversal / stale-derived surface — H5 gap)"
            ),
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


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


# --- Episodic memory (H4) + query-time exclusion (H2) red-specs ----------------
# These drive the real Postgres write/retrieval path via an isolated throwaway
# tenant (mirrors retrieves_fact_for_query). Each targets the intended H4/H2 API;
# until it exists the check fails cleanly (caught), so the case is a RED gate.
# Needs a Postgres DSN (embedder: cached / substrate: vector) else the harness SKIPs.

def _episodic_graph():
    """A throwaway-tenant PostgresVectorGraph wired like the other DSN-backed checks."""
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    org = "eval_episodic_" + uuid.uuid4().hex[:12]
    graph = PostgresVectorGraph(
        conn, org, "u1", embedder=_eval_embedder(_CachedAxis()), policy=[Redactor(), Deduper()]
    )
    return graph, conn, org


def _episodic_facts(graph):
    """Active facts currently tagged category='episodic' in the tenant."""
    return [f for f in graph.all_facts(state="active") if (f.category or "") == "episodic"]


def episode_stored_whole_and_immutable(
    ctx: EvalContext, *, episode_a: str, episode_b: str, semantic_text: str
) -> CheckResult:
    """H4 store-only lane + immutability: a decision log is whole, append-only, and
    never merged/contradicted.

    Records two decision-note episodes on one topic plus a semantic fact, then asserts:
    each episode is stored WHOLE (one row, no atomization); both episodes persist (no
    dedup/merge); neither is rejected/superseded (no contradiction); and a semantic
    write on the topic is not contradiction-flagged against an episode. Uses
    ``graph.record_episode`` (the store-only producer) — absent today, so RED until H4.
    """
    graph, conn, org = _episodic_graph()
    try:
        try:
            graph.record_episode(episode_a)
            graph.record_episode(episode_b)
        except (AttributeError, TypeError):
            return CheckResult(
                name="episode_stored_whole_and_immutable",
                passed=False,
                evidence="record_episode (store-only episode lane) not available — H4 not built",
            )
        eps = _episodic_facts(graph)
        whole = {e.text for e in eps}
        both_whole = episode_a in whole and episode_b in whole  # not atomized
        both_persist = len(eps) >= 2  # no dedup/merge collapse
        none_retired = all(e.state == "active" for e in eps)  # no supersession/reject
        graph.write(semantic_text, state="active")
        eps_after = _episodic_facts(graph)
        not_flagged = all(e.state == "active" for e in eps_after) and len(eps_after) >= 2
        ok = both_whole and both_persist and none_retired and not_flagged
        return CheckResult(
            name="episode_stored_whole_and_immutable",
            passed=ok,
            evidence=(
                "episodes stored whole, both persist, none retired, semantic write not flagged"
                if ok
                else f"whole={both_whole} persist={both_persist} active={none_retired} "
                f"semantic_not_flagged={not_flagged} (H4 store-only/immutability gap)"
            ),
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))


def episodic_reserved_tag_integrity(
    ctx: EvalContext, *, non_episode_text: str
) -> CheckResult:
    """H4 §1c: a NON-episode write must not silently land with category='episodic'.

    Writes a plain semantic fact through the normal pipeline tagged 'episodic'. The
    reserved tag is load-bearing (it routes to the store-only lane and out of recall),
    so a non-episode using it must be rejected or namespaced. RED today: the normal
    write accepts the tag and stores an episodic-category active fact with no
    ``meta.episode`` — a stray fact that would silently vanish from recall.
    """
    graph, conn, org = _episodic_graph()
    try:
        try:
            graph.write(non_episode_text, state="active", category="episodic")
        except Exception:
            # A hard reject is an acceptable integrity enforcement.
            return CheckResult(
                name="episodic_reserved_tag_integrity", passed=True,
                evidence="non-episode write tagged 'episodic' was rejected",
            )
        # Integrity holds if no active 'episodic' fact lacking meta.episode exists.
        strays = [
            f for f in _episodic_facts(graph)
            if not (isinstance(f.meta, dict) and f.meta.get("episode"))
        ]
        ok = not strays
        return CheckResult(
            name="episodic_reserved_tag_integrity",
            passed=ok,
            evidence=(
                "reserved tag enforced (no stray episodic-tagged non-episode)"
                if ok
                else f"{len(strays)} non-episode fact(s) silently stored as category='episodic' "
                "(reserved-tag integrity gap)"
            ),
        )
    finally:
        conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))


def context_excludes_episodic(
    ctx: EvalContext, *, semantic_text: str, episode_text: str, query: str
) -> CheckResult:
    """H2: server-side exclusion keeps episodes out of semantic recall (cosine AND
    keyword), while include-filters still reach them.

    Seeds a semantic fact and an episode, then asserts ``search(exclude_categories=
    ['episodic'])`` returns the semantic fact and omits the episode on BOTH the cosine
    and the hybrid (keyword-fused) branch, and that an include filter
    (``filters={'category':'episodic'}``) still returns the episode. RED today:
    ``search`` has no ``exclude_categories`` param.
    """
    graph, conn, org = _episodic_graph()
    try:
        graph.write(semantic_text, state="active")
        graph.record_episode(episode_text)
        try:
            cos = graph.search(query, top_k=5, exclude_categories=["episodic"])
            kw = graph.search(query, top_k=5, exclude_categories=["episodic"], hybrid=True)
        except TypeError:
            return CheckResult(
                name="context_excludes_episodic", passed=False,
                evidence="search() has no exclude_categories param — H2 not built",
            )
        def texts(hits):
            return {h.fact.text for h in hits}
        excluded = (
            semantic_text in texts(cos) and episode_text not in texts(cos)
            and episode_text not in texts(kw)
        )
        incl = graph.search(query, top_k=5, filters={"category": "episodic"})
        include_still_works = episode_text in texts(incl)
        ok = excluded and include_still_works
        return CheckResult(
            name="context_excludes_episodic",
            passed=ok,
            evidence=(
                "episode excluded on cosine+keyword; include-filter still returns it"
                if ok
                else f"excluded={excluded} include_works={include_still_works} (H2 exclusion gap)"
            ),
        )
    finally:
        conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))


def stale_episode_findable_not_in_context(
    ctx: EvalContext, *, basis_text: str, episode_text: str, query: str
) -> CheckResult:
    """H4×H5×H2: a decision whose basis was invalidated stays in the episode log but
    never re-enters semantic recall.

    Seeds a basis fact and an episode derived from it, invalidates the basis (so H5
    flags the episode stale), then asserts the episode is still findable via the
    episode-log query AND still excluded from semantic search. RED until H4 tag +
    H2 exclusion exist together.
    """
    graph, conn, org = _episodic_graph()
    try:
        basis_id = graph.write(basis_text, state="active")
        try:
            graph.record_episode(episode_text, derived_from=[basis_id])
        except (AttributeError, TypeError):
            return CheckResult(
                name="stale_episode_findable_not_in_context", passed=False,
                evidence="record_episode not available — H4 not built",
            )
        graph.set_state(basis_id, "rejected")  # H5 hook flags the episode stale
        log = graph.search(query, top_k=5, filters={"category": "episodic"})
        in_log = episode_text in {h.fact.text for h in log}
        try:
            ctx_hits = graph.search(query, top_k=5, exclude_categories=["episodic"])
        except TypeError:
            return CheckResult(
                name="stale_episode_findable_not_in_context", passed=False,
                evidence="search() has no exclude_categories param — H2 not built",
            )
        not_in_context = episode_text not in {h.fact.text for h in ctx_hits}
        ok = in_log and not_in_context
        return CheckResult(
            name="stale_episode_findable_not_in_context",
            passed=ok,
            evidence=(
                "stale episode still in episode-log, excluded from semantic context"
                if ok
                else f"in_log={in_log} excluded_from_context={not_in_context} (H4/H5/H2 gap)"
            ),
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
        conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))


def retrieval_prefers_recent_over_stale(
    ctx: EvalContext,
    *,
    query: str,
    stale_text: str,
    recent_text: str,
    stale_age_days: int = 400,
) -> CheckResult:
    """H3 (temporal decay) red spec: a stale, unconfirmed fact must not outrank a
    fresh one on age alone.

    Seeds two facts for the same query: ``stale_text`` (written to be *more*
    query-similar, then backdated ~``stale_age_days``) and ``recent_text`` (the
    current truth, just written). Asserts the recent fact ranks ABOVE the stale one.

    Without time decay this FAILS: ``search`` ranks by similarity*utility only, so the
    older-but-more-similar fact wins regardless of age. With a recency-decay factor
    folded into ranking, the stale fact's weight decays and the fresh one wins.

    Requires a Postgres DSN (``embedder: cached`` / ``substrate: vector``).
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    org = "eval_decay_" + uuid.uuid4().hex[:12]
    graph = PostgresVectorGraph(
        conn, org, "u1", embedder=_eval_embedder(_CachedAxis()), policy=[Redactor(), Deduper()]
    )
    try:
        stale_id = graph.write(stale_text, state="active")
        graph.write(recent_text, state="active")
        # Backdate the stale fact so only age (not similarity/outcomes) differs.
        conn.execute(
            "UPDATE facts SET created_at = now() - make_interval(days => %s), "
            "valid_at = now() - make_interval(days => %s) "
            "WHERE id = %s AND org_id = %s",
            (stale_age_days, stale_age_days, stale_id, org),
        )
        ranked = [h.fact.text for h in graph.search(query, top_k=5)]
        recent_rank = ranked.index(recent_text) if recent_text in ranked else 1_000
        stale_rank = ranked.index(stale_text) if stale_text in ranked else 1_000
        ok = recent_rank < stale_rank
        return CheckResult(
            name="retrieval_prefers_recent_over_stale",
            passed=ok,
            evidence=(
                f"recent fact ranks #{recent_rank} vs stale ({stale_age_days}d) "
                f"#{stale_rank} for {query!r}"
                + ("" if ok else " — stale fact wins on similarity; no recency decay (H3 gap)")
            ),
        )
    finally:
        conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))


def derived_learning_not_merged_into_source(
    ctx: EvalContext, *, requirement_text: str, learning_text: str
) -> CheckResult:
    """A learning written with ``derived_from=[req]`` must NOT merge into its source.

    The agent factory writes each requirement as its own fact, then writes a learning
    *about* how that requirement was implemented, passing ``derived_from=[requirement_id]``
    and a distinct ``category``. A derivation explicitly declares a NEW fact built on the
    source -- it must stay a distinct node (carrying a ``derived_from`` edge), not be folded
    back into the source. Today the Mem0-style Augmenter ignores both ``derived_from`` and
    ``category`` and merges the learning into the requirement: ``write()`` returns the source
    id, losing the learning's metadata and the derivation edge.

    Drives the REAL production write policy (``default_write_policy`` -> includes the
    Augmenter) in a fresh isolated tenant. Asserts the learning is its own distinct fact.
    RED today (verified live 2026-06-25: learn_id == req_id under the full policy, while
    ``[Redactor, Deduper]`` alone keeps them distinct -- so the Augmenter is the culprit).
    GREEN once a ``derived_from``-carrying write is exempt from the merge.

    Requires a Postgres DSN AND an OpenRouter key (the Augmenter's judge is a live LLM call);
    the harness SKIPs without them. GREEN: a derived_from-carrying write is now exempt from
    the merge (see WriteDecision.derived, threaded in PostgresVectorGraph.write).
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        default_write_policy,
    )
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    org = "eval_derivmerge_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=_eval_embedder(_CachedAxis()), policy=default_write_policy()
    )
    try:
        req_id = graph.write(requirement_text, state="active", category="requirement")
        learn_id = graph.write(
            learning_text, state="active", category="learning", derived_from=[req_id]
        )
        n_active = len(graph.all_facts(state="active"))
        ok = learn_id is not None and learn_id != req_id and n_active >= 2
        return CheckResult(
            name="derived_learning_not_merged_into_source",
            passed=ok,
            evidence=(
                f"derived learning kept distinct (req={req_id}, learn={learn_id}, "
                f"{n_active} active facts)"
                if ok
                else "derived learning MERGED into its source requirement: write() returned "
                f"the source id (req={req_id} == learn={learn_id}, {n_active} active fact). "
                "The Augmenter ignored derived_from + category."
            ),
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


def surface_binding_governs_screen(
    ctx: EvalContext,
    *,
    project: str,
    screen_id: str,
    requirement_text: str,
    other_requirement_text: str,
) -> CheckResult:
    """A typed RENDERS binding makes a requirement *govern* a wireframe screen, and the
    bidirectional coverage gate is exact and rejection-aware.

    The agent factory's wireframe->code step asks "which requirements govern screen s-X?"
    and runs a two-sided completeness gate ("every screen is covered by a requirement, and
    every requirement renders some screen"). This drives the REAL ``renders`` edge path on
    a fresh isolated tenant: two ``active`` ``category="requirement"`` facts are written, but
    ONLY the first is bound to the surface via ``graph.bind_surface`` (which ``ensure_surface``s
    a ``category="surface"`` fact and adds a ``renders`` edge).

    Asserts:
      (a) ``requirements_for_surface(project, screen_id)`` returns the bound requirement and
          NOT the other (the binding governs the screen precisely);
      (b) ``surface_coverage(project)`` reports the OTHER requirement in
          ``uncoveredRequirements`` and reports NO uncovered surface for ``screen_id``
          (the bound side covers its screen; the unbound side is flagged);
      (c) after ``set_state(req_id, "rejected")`` the requirement drops from
          ``requirements_for_surface`` AND the surface reappears in ``uncoveredSurfaces``
          (active-only filtering: a rejected endpoint drops from every result, so the screen
          is once again uncovered -- referential integrity via rejection, no stale hook).

    Requires a Postgres DSN (``embedder: cached`` / ``substrate: vector``); the harness SKIPs
    the case without one.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    embedder = _eval_embedder(_CachedAxis())
    conn = db.connect()
    db.bootstrap()
    org = "eval_surface_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=embedder, policy=[Redactor(), Deduper()]
    )
    try:
        req_id = graph.write(requirement_text, state="active", category="requirement")
        other_id = graph.write(
            other_requirement_text, state="active", category="requirement"
        )
        # Bind ONLY the first requirement to the surface (typed renders edge).
        graph.bind_surface(req_id, screen_id, project, title="Today")

        # (a) the binding governs the screen precisely.
        governing = [f.id for f in graph.requirements_for_surface(project, screen_id)]
        a_bound_only = req_id in governing and other_id not in governing

        # (b) two-sided coverage gate: the unbound requirement is uncovered; the bound
        # surface is NOT uncovered.
        cov = graph.surface_coverage(project)
        uncovered_reqs = {f.id for f in cov["uncoveredRequirements"]}
        uncovered_surface_screens = {
            (f.meta or {}).get("screen_id") for f in cov["uncoveredSurfaces"]
        }
        b_other_uncovered = other_id in uncovered_reqs
        b_surface_covered = screen_id not in uncovered_surface_screens
        b_ok = b_other_uncovered and b_surface_covered

        # (c) rejection drops the endpoint from every result; the screen is uncovered again.
        graph.set_state(req_id, "rejected")
        governing_after = [
            f.id for f in graph.requirements_for_surface(project, screen_id)
        ]
        c_req_dropped = req_id not in governing_after
        cov_after = graph.surface_coverage(project)
        uncovered_surface_screens_after = {
            (f.meta or {}).get("screen_id") for f in cov_after["uncoveredSurfaces"]
        }
        c_surface_uncovered = screen_id in uncovered_surface_screens_after
        c_ok = c_req_dropped and c_surface_uncovered

        ok = a_bound_only and b_ok and c_ok
        return CheckResult(
            name="surface_binding_governs_screen",
            passed=ok,
            evidence=(
                f"renders binding governs {screen_id!r}: bound req {req_id} governs and "
                f"the other is flagged uncovered; rejecting the bound req drops it and "
                f"re-uncovers the surface"
                if ok
                else f"surface binding/coverage gap "
                f"[a bound-only={a_bound_only}; b other-uncovered={b_other_uncovered}, "
                f"surface-covered={b_surface_covered}; c req-dropped={c_req_dropped}, "
                f"surface-re-uncovered={c_surface_uncovered}]"
            ),
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


def requirement_not_fragmented_by_distillation(
    ctx: EvalContext,
    *,
    requirement_text: str,
    requirement_id: str = "R1",
    acceptance_marker: str = "Acceptance",
    control_text: str | None = None,
) -> CheckResult:
    """A single multi-sentence requirement written via ``add_insight`` must stay ONE fact.

    The agent factory (``factory-plan``) admits each settled requirement via ``add_insight``
    with ``category="requirement"`` + ``meta={"requirement_id": ...}``, relying on a 1:1
    R-id<->fact mapping and on the requirement's binary **acceptance condition** living on the
    requirement fact. But ``add_insight`` is wired with ``llm=None`` (see ``serve/app.py``
    ``add_insight`` -> ``build_trio(graph, llm=None)``), so ``PromptIngestor.synthesis`` falls
    to ``segment_passthrough``, which splits the input into **one fact per sentence**. A
    three-sentence requirement therefore lands as THREE facts -- and the ``Acceptance: ...``
    clause is severed into its own fact (all three share the same ``requirement_id``).

    Observed live (2026-06-26, agent-factory planning run): one ``add_insight`` of R1 produced
    3 active facts; the single-sentence R2 stayed whole. Deterministic -- no LLM / model key --
    but it drives the REAL production path (``default_write_policy`` + the ``llm=None``
    ``PromptIngestor`` that ``add_insight`` builds) in a fresh isolated tenant, so it needs a
    Postgres DSN; the harness SKIPs without one.

    Failure points asserted (all must hold for GREEN):
      FP1  the requirement lands as exactly ONE active fact (no per-sentence fragmentation);
      FP2  that fact retains its acceptance condition (``acceptance_marker`` in its text) --
           i.e. the acceptance is NOT severed into a separate fact;
      FP3  that fact carries ``category="requirement"`` and ``meta.requirement_id``;
      FP4  control: a genuinely single-sentence requirement also stays one fact (guards against
           a trivial "everything collapses to one" pass).

    RED today (FP1 fails: 3 facts). GREEN once ``add_insight`` can ingest a pre-atomic
    requirement as a single fact (e.g. an ``atomic``/``distill=False`` mode that skips the
    sentence splitter while keeping dedup + contradiction surfacing).
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        default_write_policy,
    )
    from knowledge.wiring import build_trio
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    emb = _eval_embedder(_CachedAxis())
    user = "u1"
    org = "eval_fragment_" + uuid.uuid4().hex[:12]
    ctrl_org = "eval_fragment_ctrl_" + uuid.uuid4().hex[:12]

    def _ingest(text: str, org_id: str):
        # Mirror app.add_insight(on_conflict="surface") EXACTLY: the surface write policy plus
        # the llm=None PromptIngestor that build_trio wires for the insight path.
        graph = PostgresVectorGraph(conn, org_id, user, embedder=emb, policy=default_write_policy())
        _, ingestor, _ = build_trio(graph=graph, llm=None)
        ingestor.ingest(
            text,
            state="active",
            source="prd-team-app",
            category="requirement",
            meta={"requirement_id": requirement_id},
            # Mirror add_insight's shaped-fact lane: it now defaults atomic=True so a
            # pre-atomic insight stays one fact instead of fragmenting per-sentence.
            atomic=True,
        )
        return graph.all_facts(state="active")

    try:
        facts = _ingest(requirement_text, org)
        n = len(facts)
        fp1_one_fact = n == 1
        the_fact = facts[0] if n == 1 else None
        fp2_acceptance_kept = bool(
            the_fact and acceptance_marker.lower() in (the_fact.text or "").lower()
        )
        fp3_meta_kept = bool(
            the_fact
            and the_fact.category == "requirement"
            and (the_fact.meta or {}).get("requirement_id") == requirement_id
        )

        # FP4 control: a single-sentence requirement must stay one fact.
        fp4_control_ok = True
        ctrl_n = None
        if control_text is not None:
            ctrl_n = len(_ingest(control_text, ctrl_org))
            fp4_control_ok = ctrl_n == 1

        passed = fp1_one_fact and fp2_acceptance_kept and fp3_meta_kept and fp4_control_ok
        if passed:
            evidence = (
                f"requirement stayed atomic: 1 active fact retaining its acceptance condition "
                f"and requirement_id={requirement_id}"
                + ("; control stayed 1 fact" if control_text is not None else "")
            )
        else:
            severed = n > 1 and any(
                acceptance_marker.lower() in (f.text or "").lower() for f in facts
            )
            evidence = (
                f"requirement FRAGMENTED by the sentence-splitter: ONE add_insight produced "
                f"{n} active facts (expected 1) [FP1={fp1_one_fact}]; "
                f"acceptance-on-the-fact [FP2={fp2_acceptance_kept}] "
                f"(acceptance severed into its own fact: {severed}); "
                f"category+requirement_id on the fact [FP3={fp3_meta_kept}]"
                + (
                    f"; control single-sentence req -> {ctrl_n} fact(s) [FP4={fp4_control_ok}]"
                    if control_text is not None
                    else ""
                )
                + ". add_insight uses llm=None -> PromptIngestor.segment_passthrough splits per sentence."
            )
        return CheckResult(
            name="requirement_not_fragmented_by_distillation",
            passed=passed,
            evidence=evidence,
        )
    finally:
        for o in (org, ctrl_org):
            conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (o, user))
            conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (o, user))


def contradicting_requirement_not_merged(
    ctx: EvalContext,
    *,
    incumbent_text: str,
    contradicting_text: str,
    contradiction_marker: str,
) -> CheckResult:
    """A directly-contradicting same-subject requirement must SURFACE, never be merged.

    The agent factory admits each requirement via ``add_insight(on_conflict="surface")`` and
    depends on a contradiction being *flagged* (a pending pair in ``GET /contradictions``) so
    the human can adjudicate -- this is the planning loop's self-consistency mechanism. But
    under ``surface`` (= ``default_write_policy``), the Mem0-style **Augmenter** runs its
    additive merge BEFORE the ConflictFlagger fires, so a newcomer the Augmenter judges to be
    "about the same subject" as the incumbent is folded into it -- even when the two
    **contradict**. ``write()`` returns the incumbent's id, ``contradictionsSurfaced=0``, and
    the incumbent fact is mutated into a self-contradictory blend.

    Observed live (2026-06-26, agent-factory planning run): writing "daily completion REQUIRES
    >=1 checklist item" against the incumbent R1 ("the checklist does NOT affect completion")
    returned ``action="merged"`` with R1's id, 0 contradictions surfaced, and R1's text became
    self-contradictory (it now asserts both that a checklist item is required AND that the
    checklist never changes the result). Reproduced deterministically via ``graph.write`` under
    ``default_write_policy`` (req_id == chal_id).

    Drives the REAL production write policy in a fresh isolated tenant. Requires a Postgres DSN
    AND an OpenRouter key (the Augmenter + conflict judges are live LLM calls); the harness
    SKIPs without them.

    Failure points asserted (all must hold for GREEN):
      FP1  the contradicting write is NOT merged into the incumbent (a distinct fact id);
      FP2  the incumbent fact is NOT mutated to carry the contradicting clause
           (``contradiction_marker`` absent from the incumbent text) -- i.e. it is not blended
           into a self-contradictory fact.

    RED today (FP1 fails: chal_id == req_id; the incumbent absorbs the contradiction). GREEN
    once the Augmenter refuses to merge contradictory facts so ``surface`` can flag the pair.

    NB ``surface``/FR-005 may demote the distinct newcomer to ``proposed``, so this check does
    NOT assert an active-fact count -- only that the contradiction was kept as its own fact and
    the incumbent stayed clean.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        default_write_policy,
    )
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    org = "eval_contradmerge_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=_eval_embedder(_CachedAxis()), policy=default_write_policy()
    )
    try:
        req_id = graph.write(incumbent_text, state="active", category="requirement")
        chal_id = graph.write(contradicting_text, state="active", category="requirement")
        merged = chal_id is None or chal_id == req_id
        # On merge the Augmenter updates the incumbent row in place, so req_id stays active.
        inc = next((f for f in graph.all_facts(state="active") if f.id == req_id), None)
        inc_text = (getattr(inc, "text", "") or "") if inc else ""
        blended = contradiction_marker.lower() in inc_text.lower()
        passed = (not merged) and (not blended)
        if passed:
            evidence = (
                f"contradiction kept distinct (incumbent={req_id}, challenger={chal_id}); "
                "incumbent text not blended -- surface can flag the pair"
            )
        else:
            evidence = (
                f"contradicting requirement MERGED by the Augmenter instead of surfaced "
                f"[FP1 merged={merged}] (req_id={req_id}, chal_id={chal_id}); "
                f"incumbent blended into a self-contradictory fact "
                f"[FP2 marker '{contradiction_marker}' present={blended}]. "
                "The Augmenter ran its additive merge before the ConflictFlagger, so "
                "on_conflict='surface' never fired (contradictionsSurfaced=0)."
            )
        return CheckResult(
            name="contradicting_requirement_not_merged",
            passed=passed,
            evidence=evidence,
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))


def tabular_field_not_merged_into_incumbent(
    ctx: EvalContext,
    *,
    incumbent_text: str,
    table_text: str,
    overlap_marker: str,
) -> CheckResult:
    """A tabular-derived fact must NOT be Augmenter-merged into a pre-existing incumbent.

    When the factory ingests a data-model table, ``add_insight`` (llm=None ->
    ``PromptIngestor.synthesis`` -> ``linearize_table``) emits one ``tabular``-flagged fact
    per row. The Deduper's slot-guard keeps those rows distinct from EACH OTHER. But a row
    whose subject overlaps a PRE-EXISTING, separately-written, non-tabular incumbent is still
    additively merged into that incumbent by the Mem0-style Augmenter -- the slot-guard does
    not exempt a tabular write from being folded into an established non-tabular fact.

    Observed live (2026-06-26, agent-factory planning run): with R5 ("team day boundary ...
    resets at 3:00 AM ...") already active, ingesting a DailySubmission field table merged the
    ``team_day`` row INTO R5 (R5's text gained "for the team_day field, type is date, note is
    resolved via the 3AM boundary"), while the other three rows (user_id, completion_status,
    ratings_json) correctly landed as their own distinct facts. Reproduced deterministically.

    Distinct from ``augment_no_merge_distinct_rules`` (two prose rules seeded together): here a
    TABULAR fact merges into a SEPARATELY-WRITTEN incumbent despite the slot-guard. Same
    Augmenter-over-merge family, different mechanism.

    Drives the REAL write policy (``default_write_policy`` + the llm=None linearizing ingestor)
    in a fresh isolated tenant: writes ``incumbent_text`` active, then ingests ``table_text``.
    Needs a Postgres DSN AND an OpenRouter key (the Augmenter judge is a live LLM call); the
    harness SKIPs without them.

    Failure points asserted (all must hold for GREEN):
      FP1  the incumbent fact is NOT polluted with the overlapping row (``overlap_marker``
           absent from the incumbent text);
      FP2  the overlapping row exists as its OWN distinct active fact (carries
           ``overlap_marker``, separate id from the incumbent).

    RED today (FP1 fails: incumbent absorbs the row; FP2 fails: no standalone row fact). GREEN
    once the Augmenter stops folding a distinct tabular fact into an overlapping incumbent.
    """
    from knowledge.evals.run import _eval_embedder
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        default_write_policy,
    )
    from knowledge.wiring import build_trio
    from knowledge.serve import db

    class _CachedAxis:
        embedder = "cached"

    conn = db.connect()
    db.bootstrap()
    org = "eval_tabmerge_" + uuid.uuid4().hex[:12]
    user = "u1"
    graph = PostgresVectorGraph(
        conn, org, user, embedder=_eval_embedder(_CachedAxis()), policy=default_write_policy()
    )
    _, ingestor, _ = build_trio(graph=graph, llm=None)
    marker = overlap_marker.lower()
    try:
        inc_id = graph.write(incumbent_text, state="active", category="requirement")
        ingestor.ingest(table_text, state="active", source="prd-team-app", category="requirement")
        facts = graph.all_facts(state="active")
        inc = next((f for f in facts if f.id == inc_id), None)
        inc_text = (getattr(inc, "text", "") or "") if inc else ""
        fp1_incumbent_clean = marker not in inc_text.lower()
        fp2_own_fact = any(
            marker in (getattr(f, "text", "") or "").lower() and f.id != inc_id for f in facts
        )
        passed = fp1_incumbent_clean and fp2_own_fact
        if passed:
            evidence = (
                f"tabular row kept distinct: incumbent {inc_id} not polluted and "
                f"'{overlap_marker}' lives in its own fact ({len(facts)} active facts)"
            )
        else:
            evidence = (
                f"tabular row MERGED into the incumbent by the Augmenter "
                f"[FP1 incumbent-clean={fp1_incumbent_clean}] (incumbent {inc_id} "
                f"{'absorbed' if not fp1_incumbent_clean else 'kept'} '{overlap_marker}'); "
                f"standalone row fact present [FP2={fp2_own_fact}]. "
                f"{len(facts)} active facts -- the slot-guard did not exempt the tabular write "
                "from being folded into an overlapping pre-existing non-tabular fact."
            )
        return CheckResult(
            name="tabular_field_not_merged_into_incumbent",
            passed=passed,
            evidence=evidence,
        )
    finally:
        conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
        conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
