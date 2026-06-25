"""Formal eval for the tax-rule ingestion pipeline (``ingest_dump``).

This drives the EXACT seeding path the ``agent_tax_harness`` "Seed tax rules ->
Praxis" button exercises: ``POST /ingest`` builds a dedup-only
``PostgresVectorGraph`` (``policy=[Redactor(), Deduper()]``) and calls
``ingest_dump(graph, OpenRouterLlm(), text, state, source)`` once per rule
document. We reproduce that here against a THROWAWAY local tenant and assert the
behaviors we kept hand-iterating on:

  1. self-contained distillation (no thin/subjectless fragments),
  2. no false-positive contradictions on the bracket/std-deduction tables
     (ZERO facts retired) when seeding empty,
  3. semantic dedup (a restated fact collapses to ONE active survivor),
  4. real conflict detection + resolution still works on a genuine correction,
  5. bounded LLM call volume — O(documents), not O(facts x neighbors).

Architecture note (current ``ingest_dump``)
-------------------------------------------
``ingest_dump`` owns dedup AND conflict resolution itself, via slot-granular
claims: it distills each fact with a granular ``(subject, attribute, value)``
claim whose SUBJECT carries the row's discriminating key (income range, filing
status, line/box number), collapses semantic duplicates, and resolves genuine
same-slot value clashes (loser retired + a ``contradicted_by`` edge). The write
policy ``/ingest`` ships is dedup-only (``[Redactor(), Deduper()]``) — conflict
detection is deliberately NOT delegated to it, so the coarse-slot claim extractor
(intentionally coarse for the structural-contradiction feature) can never
false-flag tax-table rows. Summary: ``{"facts", "merged", "conflicts", "rejected"}``.

  - behavior 2 (no false positives) is tested on the real dedup-only ``/ingest``
    path: once over the full rule set, and once on a focused adversarial bracket
    subset — no fact retired, no contradiction edge among rows. (The raw claim
    policy's deliberate coarsening is covered by the ``multiway_two_slots``
    component eval, not re-litigated here.)
  - behavior 4 (real conflict) drives a genuine value correction (MFJ deduction
    $31,500 -> $32,200) on that same path and asserts it is detected and resolved
    (stale value retired, ``contradicted_by`` edge). It passes today.

Determinism / safety design
----------------------------
Ingestion calls a real LLM + embedder, so phrasing varies run-to-run. To make
the eval DETERMINISTIC and runnable offline we cassette-back both seams (exactly
like the component eval ``multiway_two_slots``): every ``ingest_dump`` call runs
through a :class:`CassetteLlm` over committed responses, and every graph embeds
through a :class:`CachedEmbedder` over committed vectors. With
``OPENROUTER_API_KEY`` set we RECORD misses to those fixtures; without a key we
REPLAY them — so the eval produces the same result every run and needs no key in
CI. We still assert STRUCTURAL properties (no fact retired, dedup probe has one
active match, call-count <= 2 per doc) rather than exact phrasing.

DB SAFETY: we load the repo-root ``.env`` by the ``knowledge`` package location
(NOT a bare ``load_dotenv()`` which would search from this file's dir and could
silently fall back to PROD RDS), then HARD-ASSERT the DSN host is localhost
before any write. Every run uses a unique throwaway ``(org_id, user_id)`` and
tears it down in a ``finally``.

Requires a local Postgres (``PRAXIS_DB_URL`` -> localhost); skips cleanly without
one. The LLM + embeddings are cassette-backed, so it RUNS and PASSES on replay
WITHOUT ``OPENROUTER_API_KEY`` (a key is only needed to record fixtures).
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Environment: load the repo-root .env by package location, assert LOCAL DSN.
# --------------------------------------------------------------------------- #
def _load_repo_env() -> None:
    """Load the repo-root ``.env`` explicitly (never a dir-relative fallback)."""
    import knowledge

    repo_root = Path(knowledge.__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if env_path.exists():
        load_dotenv(env_path)


def _require_local_or_skip() -> str:
    _load_repo_env()
    url = os.getenv("PRAXIS_DB_URL", "")
    if not url:
        pytest.skip("PRAXIS_DB_URL unset (need a local Postgres for the tax-ingest eval)")
    host = re.sub(r"^.*@", "", url).split("/")[0].split(":")[0]
    if host not in ("localhost", "127.0.0.1"):
        pytest.skip(f"PRAXIS_DB_URL host is {host!r}, not local — refusing to run against remote")
    # NOTE: no OPENROUTER_API_KEY requirement — the LLM + embeddings are
    # cassette-backed (see _cassette_llm / _cached_embedder), so the eval RUNS and
    # PASSES on replay without a key. A key is only needed to RECORD the fixtures.
    return url


# --------------------------------------------------------------------------- #
# Cassette wiring: replay the LLM + embeddings from committed fixtures so the eval
# is deterministic and offline. With a key we record misses; without, we replay.
# --------------------------------------------------------------------------- #
_ALLOW_COMPUTE = bool(os.getenv("OPENROUTER_API_KEY"))
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LLM_CASSETTE = _FIXTURES / "tax_ingestion_llm.json"


def _embed_model() -> str:
    from knowledge.llm import openrouter_http

    return os.getenv("OPENROUTER_EMBED_MODEL", openrouter_http.DEFAULT_EMBED_MODEL)


def _embed_cache_path(model: str) -> Path:
    slug = model.replace("/", "_").replace(":", "_")
    return _FIXTURES / f"embeddings_{slug}.json"


def _cassette_llm():
    """A CassetteLlm over the real OpenRouter model: replay committed responses,
    record misses only when a key is present."""
    from knowledge.llm.llm_cassette import CassetteLlm
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

    return CassetteLlm(OpenRouterLlm(), _LLM_CASSETTE, allow_compute=_ALLOW_COMPUTE)


def _cached_embedder():
    """A CachedEmbedder over committed real vectors (records misses only with a key)."""
    from knowledge.llm.embedder_variants.cached_embedder import CachedEmbedder
    from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder

    model = _embed_model()
    inner = OpenRouterEmbedder(model=model) if _ALLOW_COMPUTE else None
    return CachedEmbedder(inner, _embed_cache_path(model), model_id=model, allow_compute=_ALLOW_COMPUTE)


# --------------------------------------------------------------------------- #
# Real tax rule documents — the exact text the seed button ingests.
# --------------------------------------------------------------------------- #
# The bracket + standard-deduction rows are the false-positive minefield (every
# row shares "TY2025 ... %/$..." structure); line-25a / withholding is restated
# across several docs (the semantic-dedup probe). A few prose docs round it out.
RULE_DOCUMENTS: list[dict[str, str]] = [
    {
        "source": "form_1040_ty2025:standard_deduction",
        "text": (
            "TY2025 Form 1040 standard deduction by filing status: Single $15,750; "
            "Married filing jointly $31,500; Married filing separately $15,750; "
            "Head of household $23,625. Enter on Form 1040 line 12."
        ),
    },
    {
        "source": "form_1040_ty2025:std_deduction_mfj",
        "text": "TY2025 standard deduction for Married filing jointly is $31,500 (Form 1040 line 12).",
    },
    {
        "source": "form_1040_ty2025:std_deduction_hoh",
        "text": "TY2025 standard deduction for Head of household is $23,625 (Form 1040 line 12).",
    },
    {
        "source": "form_1040_ty2025:brackets_single",
        "text": (
            "TY2025 ordinary income tax brackets, Single: 10% up to $11,925; "
            "12% $11,925-$48,475; 22% $48,475-$103,350; 24% $103,350-$197,300; "
            "32% $197,300-$250,525; 35% $250,525-$626,350; 37% above $626,350."
        ),
    },
    {
        "source": "form_1040_ty2025:brackets_mfj",
        "text": (
            "TY2025 ordinary income tax brackets, Married filing jointly: 10% up to "
            "$23,850; 12% $23,850-$96,950; 22% $96,950-$206,700; 24% "
            "$206,700-$394,600; 32% $394,600-$501,050; 35% $501,050-$751,600; "
            "37% above $751,600."
        ),
    },
    {
        "source": "form_1040_ty2025:brackets_hoh",
        "text": (
            "TY2025 ordinary income tax brackets, Head of household: 10% up to "
            "$17,000; 12% $17,000-$64,850; 22% $64,850-$103,350; 24% "
            "$103,350-$197,300; 32% $197,300-$250,500; 35% $250,500-$626,350; "
            "37% above $626,350."
        ),
    },
    {
        "source": "form_1040_ty2025:brackets_are_marginal",
        "text": (
            "Federal income tax brackets are marginal: each rate applies only to the "
            "portion of taxable income that falls within that bracket's range, not to "
            "the entire income. Sum the tax from each bracket to get total tax."
        ),
    },
    {
        "source": "form_1040_ty2025:w2_box2",
        "text": (
            "W-2 box 2 reports federal income tax already withheld from the employee's pay. "
            "It is entered on Form 1040 line 25a and counts as a payment toward the year's tax."
        ),
    },
    {
        "source": "form_1040_ty2025:payments_and_total_tax",
        "text": (
            "Form 1040 line 24 = total tax. Line 25a = federal income tax withheld from "
            "W-2 box 2. Line 33 = total payments (withholding plus any credits and estimated "
            "payments)."
        ),
    },
]

# Behavior 3 probe: "line 25a = federal income tax withheld from W-2 box 2"
# appears in BOTH w2_box2 and payments_and_total_tax, reworded. Exactly one
# active fact should survive for it after semantic dedup.
DEDUP_PROBE = "federal income tax withheld from W-2 box 2 is entered on Form 1040 line 25a"

# Behavior 4: a genuine value correction. MFJ's $31,500 is a unique amount that
# survives distillation as its own atomic fact (Single's $15,750 collapses with
# MFS's identical $15,750 under dedup, so it's a poor conflict probe).
CONFLICTING_CORRECTION = {
    "source": "form_1040_ty2025:correction_mfj_std_deduction",
    "text": (
        "The TY2025 standard deduction for Married filing jointly is $32,200 "
        "(Form 1040 line 12)."
    ),
}

# A bare procedural fragment with no subject — behavior 1 asserts NO distilled
# fact reduces to this kind of context-free imperative.
_FRAGMENT_RE = re.compile(
    r"^\s*(enter|see|use|add|subtract|multiply|report|attach|check|go to|skip)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Counting LLM shim — wraps the real model so behavior 5's call count is exact.
# --------------------------------------------------------------------------- #
class CountingLlm:
    """Delegates to an inner ``Llm`` and counts ``complete`` calls."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        return self.inner.complete(messages, **kwargs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _new_tenant():
    from knowledge.serve import db

    conn = db.connect()
    org = f"taxeval-{uuid.uuid4().hex[:8]}"
    user = f"u-{uuid.uuid4().hex[:8]}"
    return conn, org, user


def _purge(conn, org, user) -> None:
    for tbl in ("fact_edges", "claims", "facts"):
        try:
            conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s AND user_id = %s", (org, user))
        except Exception:
            pass


def _dedup_only_graph(conn, org, user):
    """The EXACT write policy POST /ingest uses for seeding."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor

    return PostgresVectorGraph(
        conn, org, user, embedder=_cached_embedder(), policy=[Redactor(), Deduper()]
    )


def _claim_policy_graph(conn, org, user, llm):
    """The production default: redact, dedup, then the structural claim-slot
    conflict path (ClaimExtractor + ClaimConflictDetector)."""
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
        default_write_policy,
    )

    return PostgresVectorGraph(conn, org, user, policy=default_write_policy(llm))


def _active_texts(graph) -> list[str]:
    return [f.text for f in graph.all_facts(state="active")]


# --------------------------------------------------------------------------- #
# Module fixture: seed the rule docs once through the dedup-only (/ingest) path.
# --------------------------------------------------------------------------- #
class _Seeded:
    def __init__(self, graph, llm, summaries, conn, org, user):
        self.graph = graph
        self.llm = llm
        self.summaries = summaries
        self.conn = conn
        self.org = org
        self.user = user
        # ingest_dump invocation count (seed docs + any test-added docs), so the
        # behavior-5 per-doc bound is order-independent.
        self.docs_ingested = len(summaries)


@pytest.fixture(scope="module")
def seeded():
    _require_local_or_skip()
    from knowledge.injestion.dump_ingest import ingest_dump

    conn, org, user = _new_tenant()
    graph = _dedup_only_graph(conn, org, user)
    llm = CountingLlm(_cassette_llm())

    summaries = []
    try:
        _purge(conn, org, user)  # defensive clean slate for the throwaway tenant
        for doc in RULE_DOCUMENTS:
            summaries.append(
                ingest_dump(graph, llm, doc["text"], state="active", source=doc["source"])
            )
        yield _Seeded(graph, llm, summaries, conn, org, user)
    finally:
        _purge(conn, org, user)


# --------------------------------------------------------------------------- #
# Behavior 1 — self-contained distillation (no thin/subjectless fragments).
# --------------------------------------------------------------------------- #
def test_b1_no_subjectless_fragments(seeded):
    texts = [f.text for f in seeded.graph.all_facts(state=None)]
    assert texts, "expected distilled facts to have been written"

    # No active fact is the literal bare imperative we kept seeing.
    assert "Enter on Form 1040 line 12." not in texts

    offenders = [t for t in texts if _FRAGMENT_RE.match(t)]
    assert not offenders, f"subjectless/procedural fragments leaked as facts: {offenders!r}"

    # Every fact carries its own subject: heuristically each should mention the tax
    # year, a filing status, a form/line reference, a deduction/bracket, or W-2 —
    # i.e. not a dangling clause.
    subjectful = re.compile(
        r"TY2025|deduction|bracket|filing|Single|Married|Head of household|"
        r"line \d|form 1040|W-2|withh|marginal|income|\btax\b",
        re.IGNORECASE,
    )
    contextless = [t for t in texts if not subjectful.search(t)]
    assert not contextless, f"facts lacking a self-contained subject: {contextless!r}"


# --------------------------------------------------------------------------- #
# Behavior 2 — no false-positive contradictions on the tax tables.
# --------------------------------------------------------------------------- #
def test_b2_no_false_positives_dedup_only(seeded):
    """The /ingest path (dedup-only) must retire nothing: no row rejects another."""
    rejected = seeded.graph.all_facts(state="rejected")
    assert not rejected, f"dedup-only seeding retired facts: {[f.text for f in rejected]!r}"
    assert not list(seeded.graph.all_edges("contradicted_by")), "no contradicted_by edges expected"
    assert not list(seeded.graph.all_edges("contradiction")), "no contradiction edges expected"

    # Every distinct rate row and per-status deduction must remain present.
    active = _active_texts(seeded.graph)
    for needle in ("31,500", "23,625", "37%", "10%"):
        assert any(needle in t for t in active), (
            f"expected a surviving active fact mentioning {needle!r}; a table row was wrongly dropped"
        )


def test_b2_no_false_positives_adversarial_brackets():
    """Focused adversarial probe of the real /ingest path (dedup-only graph +
    ingest_dump) on the worst case for false positives: the two FULL bracket
    tables plus the prose 'marginal' doc that historically triggered the storm.

    ingest_dump's slot-granular distillation makes each bracket row a distinct
    subject (its income range), so no row is compared to another — nothing is
    retired and no contradiction edge is fabricated. (We do NOT test the raw
    default_write_policy here: its ClaimExtractor deliberately coarsens subjects
    so paraphrased opinions collide for the structural-contradiction feature —
    great there, wrong for tables — which is precisely why /ingest delegates to
    ingest_dump instead. That tension is asserted by the multiway_two_slots eval,
    not re-litigated here.)"""
    _require_local_or_skip()
    from knowledge.injestion.dump_ingest import ingest_dump

    subset_sources = {
        "form_1040_ty2025:brackets_single",
        "form_1040_ty2025:brackets_mfj",
        "form_1040_ty2025:brackets_are_marginal",
    }
    docs = [d for d in RULE_DOCUMENTS if d["source"] in subset_sources]

    conn, org, user = _new_tenant()
    graph = _dedup_only_graph(conn, org, user)  # the EXACT policy POST /ingest uses
    try:
        _purge(conn, org, user)
        llm = _cassette_llm()
        for doc in docs:
            ingest_dump(graph, llm, doc["text"], state="active", source=doc["source"])
        assert not graph.all_facts(state="rejected"), (
            f"a bracket row was wrongly retired: {[f.text for f in graph.all_facts(state='rejected')]!r}"
        )
        for kind in ("contradiction", "contradicted_by"):
            assert not list(graph.all_edges(kind)), (
                f"different bracket rows were flagged as {kind}: {list(graph.all_edges(kind))!r}"
            )
        # The bracket rows survive: each table's distinct top rate is still active.
        active = _active_texts(graph)
        assert any("626,350" in t for t in active), "a Single bracket row was wrongly dropped"
        assert any("751,600" in t for t in active), "an MFJ bracket row was wrongly dropped"
    finally:
        _purge(conn, org, user)


# --------------------------------------------------------------------------- #
# Behavior 3 — semantic dedup: a restated fact collapses to ONE active survivor.
# --------------------------------------------------------------------------- #
def test_b3_semantic_dedup_collapses_restated_fact(seeded):
    hits = seeded.graph.search(DEDUP_PROBE, top_k=5, state="active")
    # The line-25a / withholding fact is restated across two docs. After semantic
    # dedup exactly one strong active match should survive (not two near-copies).
    strong = [h for h in hits if h.score >= 0.80]
    assert len(strong) == 1, (
        f"expected exactly one active fact for the line-25a/withholding probe, got "
        f"{len(strong)}: {[(round(h.score, 3), h.fact.text) for h in strong]!r}"
    )

    # Active fact count stays bounded: without dedup the restated overlaps inflate
    # it. Bound is generous (tolerates phrasing variance) but bites if dedup no-ops.
    active = _active_texts(seeded.graph)
    # Generous bound: tolerates LLM dedup/distillation phrasing variance while still
    # catching a real dedup failure (without dedup these 9 docs inflate to ~45+).
    assert len(active) <= 35, f"active fact count {len(active)} too high — dedup not collapsing overlaps"
    assert len(active) >= 6, f"active fact count {len(active)} too low — distillation under-produced"

    # At least one dedup merge must have happened across the overlapping docs.
    total_merged = sum(s.get("merged", 0) for s in seeded.summaries)
    assert total_merged >= 1, "expected at least one dedup merge across the overlapping rule docs"


# --------------------------------------------------------------------------- #
# Behavior 4 — real conflict detection + resolution via the actual /ingest path.
# --------------------------------------------------------------------------- #
def test_b4_real_conflict_is_detected_and_resolved():
    """A genuine value correction (MFJ standard deduction $31,500 -> $32,200) on
    the EXACT /ingest path (dedup-only graph + ingest_dump) is detected and
    resolved: a contradiction edge links the two, the new value is active, the
    stale value retired. ingest_dump owns this via slot-granular claims, so it no
    longer depends on the coarse write-policy claim path."""
    _require_local_or_skip()
    from knowledge.injestion.dump_ingest import ingest_dump

    conn, org, user = _new_tenant()
    graph = _dedup_only_graph(conn, org, user)
    llm = _cassette_llm()
    try:
        _purge(conn, org, user)
        # Establish the original MFJ deduction.
        ingest_dump(
            graph,
            llm,
            "TY2025 standard deduction for Married filing jointly is $31,500 (Form 1040 line 12).",
            state="active",
            source="form_1040_ty2025:std_deduction_mfj",
        )
        assert any("31,500" in t for t in _active_texts(graph)), (
            "precondition: the original $31,500 MFJ deduction should be active"
        )

        # Ingest the corrected amount.
        ingest_dump(
            graph,
            llm,
            CONFLICTING_CORRECTION["text"],
            state="active",
            source=CONFLICTING_CORRECTION["source"],
        )

        # The change is recognized and RESOLVED: a contradicted_by edge (the
        # resolved-conflict kind) links the two MFJ facts, the new value is active,
        # and the stale value is retired. ingest_dump auto-resolves, so the edge is
        # contradicted_by (resolved), not contradiction (pending review).
        assert list(graph.all_edges("contradicted_by")), (
            "the $31,500 -> $32,200 MFJ change should leave a contradicted_by edge"
        )
        active = _active_texts(graph)
        assert any("32,200" in t for t in active), "the corrected $32,200 amount should be active"
        stale_active = [t for t in active if "31,500" in t]
        assert not stale_active, f"the superseded $31,500 amount should be retired, still active: {stale_active!r}"
    finally:
        _purge(conn, org, user)


def test_b4b_real_conflict_surface_mode_keeps_both_pending():
    """The SAME $31,500 -> $32,200 MFJ clash on the /ingest path with
    ``on_conflict="surface"`` is NOT auto-resolved: both facts are kept (neither
    rejected), the edge is a *pending* ``contradiction`` (not ``contradicted_by``),
    and FR-005 holds (the incumbent stays active, the newcomer is demoted to
    proposed). A human/agent then settles it via ``FactsCandidates.resolve``.

    Reuses test_b4's recorded LLM fixtures: surface mode changes only the
    (non-LLM) resolution action, so the distill/dedup/slot prompts replay identically.
    """
    _require_local_or_skip()
    from knowledge.injestion.dump_ingest import ingest_dump
    from knowledge.serve.facts_candidates import FactsCandidates

    conn, org, user = _new_tenant()
    graph = _dedup_only_graph(conn, org, user)
    llm = _cassette_llm()
    try:
        _purge(conn, org, user)
        ingest_dump(
            graph,
            llm,
            "TY2025 standard deduction for Married filing jointly is $31,500 (Form 1040 line 12).",
            state="active",
            source="form_1040_ty2025:std_deduction_mfj",
        )
        summary = ingest_dump(
            graph,
            llm,
            CONFLICTING_CORRECTION["text"],
            state="active",
            source=CONFLICTING_CORRECTION["source"],
            on_conflict="surface",
        )

        # Surfaced, not auto-resolved: a PENDING contradiction edge, no contradicted_by.
        assert summary["surfaced"] == 1
        assert summary["conflicts"] == 0
        assert list(graph.all_edges("contradiction")), "a pending contradiction edge should exist"
        assert not list(graph.all_edges("contradicted_by")), "surface mode must not auto-resolve"

        # Both facts are kept (neither rejected); FR-005: incumbent active, newcomer proposed.
        all_texts = [f.text for f in graph.all_facts(state=None)]
        assert any("31,500" in t for t in all_texts) and any("32,200" in t for t in all_texts)
        rejected = [f.text for f in graph.all_facts(state="rejected")]
        assert not rejected, f"surface mode must not reject either side, rejected: {rejected!r}"
        active = _active_texts(graph)
        assert any("31,500" in t for t in active), "the incumbent should stay active"
        assert not any("32,200" in t for t in active), "the newcomer should be demoted to proposed"
        assert any(
            "32,200" in f.text for f in graph.all_facts(state="proposed")
        ), "the newcomer should be held as proposed for review"

        # The contradiction is surfaced for adjudication and then settles cleanly:
        # keep the corrected $32,200, retire the stale $31,500.
        facade = FactsCandidates(conn, org, user, embedder=_cached_embedder())
        clusters = facade.contradictions()
        assert clusters, "the pending contradiction should appear in the contradictions view"
        pair = clusters[0]["pairs"][0]
        keep = next(s["id"] for s in (pair["a"], pair["b"]) if "32,200" in s["content"])
        facade.resolve(pair["id"], keep)
        resolved_active = _active_texts(graph)
        assert any("32,200" in t for t in resolved_active)
        assert not any("31,500" in t for t in resolved_active)
    finally:
        _purge(conn, org, user)


# --------------------------------------------------------------------------- #
# Behavior 5 — bounded LLM call volume: O(documents), not O(facts x neighbors).
# --------------------------------------------------------------------------- #
def test_b5_bounded_llm_call_volume(seeded):
    # ingest_dump makes 1 distill call + at most 1 batched dedup call per document,
    # so <= 2 calls/doc regardless of how many facts each doc distils to. The
    # dedup-only write policy adds NO per-fact LLM calls. ``docs_ingested`` tracks
    # every ingest_dump invocation, so this bound is order-independent.
    n_docs = seeded.docs_ingested
    assert seeded.llm.calls <= 2 * n_docs, (
        f"LLM made {seeded.llm.calls} calls for {n_docs} documents — expected <= {2 * n_docs} "
        "(1 distill + <=1 batched dedup per doc). A per-fact-per-neighbour conflict judge "
        "would make hundreds; this asserts the volume scales with docs, not facts^2."
    )
    # And it must scale WITH docs (at least one distill per doc), not be a no-op.
    assert seeded.llm.calls >= n_docs, (
        f"LLM made only {seeded.llm.calls} calls for {n_docs} docs — distillation did not run per doc"
    )
