# Handoff: dedup & contradiction in the praxis write policy

> **Status: FOLDED IN — closed 2026-06-22.** The open agenda (§4) and findings here have been
> carried into two proposals:
> [dedup recall-gate + merge-judge](../2026-06-22-semantic-dedup-recall-gate-llm-judge.md) and
> [unified dedup + contradiction recall](../2026-06-22-unified-dedup-conflict-recall.md).
> Archived; retained only as the canonical home for the §1 cosine measurement table.

**Purpose:** carry the dedup/contradiction discussion into a fresh chat with clean
context. This is a working summary + open agenda, not a proposal. Date: 2026-06-22.

## 0. State of the world

- **Repo:** praxis knowledge graph. Insights are written through a **write policy** —
  `default_write_policy()` = `[Redactor, Deduper, ConflictFlagger]` (run in order on
  each `write`).
- **Store:** `VectorGraph` (in-memory MVP) — embeds each fact, brute-force cosine
  `search`/`most_similar`. Default embedder is `FakeEmbedder` (offline, hash-based, no
  semantics); real = `OpenRouterEmbedder` → `openai/text-embedding-3-small` (1536-dim).
- **Recent landed work (on `main`, PR #6):** `RetrievingReader` + a **committed embedding
  cassette** (record real vectors once, replay offline deterministically;
  `CachedEmbedder` + `embed_cache --refresh` + `fixtures/embeddings/`). This cassette
  pattern is reused below.
- **Two paraphrase-dedup eval cases are honest XFAILs today** (the system genuinely can't
  merge paraphrases): `ingestion_merge_near_dupes`, `skills_merge_dedup`.
- **Branch note:** the two proposals below + this handoff are **uncommitted** on branch
  `docs/reader-cutoff-policy` (a topic-mixing accident — sort branches before committing).
  `main` already has the retrieval work merged.

### Key files
- `knowledge/knowledge_graph/write_policy/write_step_variants/` — `deduper.py`,
  `conflict_flagger.py`, `conflict_overwriter.py`, `redactor.py`
- `knowledge/knowledge_graph/knowledge_graph_variants/vector_graph.py` (`search` /
  `most_similar` brute force; docstring names sqlite-vec/LanceDB as the swappable backend)
- `knowledge/llm/embedder_variants/cached_embedder.py`, `knowledge/evals/embed_cache.py`
- Proposals: `docs/proposals/2026-06-22-semantic-dedup-recall-gate-llm-judge.md`,
  `docs/proposals/2026-06-22-reader-cutoff-policy.md`

## 1. The core finding

**Cosine similarity is a *topicality funnel*, not a *relationship detector*.** It tells
you two notes are about the same thing; it cannot tell you whether they duplicate, agree,
or contradict. All the design follows from this.

Measured on `text-embedding-3-small` (all numbers below are from this model):

| relationship | example | cosine |
|---|---|--:|
| contradiction (tabs vs spaces) | "indent with tabs" / "indent with spaces" | **0.890** |
| agreement / paraphrase | "run the suite with uv run pytest" / "use uv run pytest to run the tests" | 0.828 |
| contradiction (direct negation) | "never commit secrets" / "fine to commit secrets" | **0.792** |
| contradiction (different framing) | "deploying Fridays is fine" / "do not deploy Fridays" | **0.771** |
| paraphrase dup (skills case) | shared "avoid generic AI-generated design" | 0.667 |
| paraphrase dup (near_dupes case) | shared "use uv run pytest" | 0.652 |
| contradiction (different vocab) | "prioritize raw performance" / "readability over micro-optimizations" | **0.454** |
| distinct ideas (skills case) | "color token system" vs "self-contained HTML" | 0.31 |
| unrelated | "indent with tabs" / "email via SES" | 0.097 |

Three consequences:
1. **Contradictions usually score HIGH** (0.77–0.89) — embeddings are negation-insensitive,
   so "X" and "not X" sit close. A contradiction can outscore an agreement.
2. **Cosine can't separate contradiction from agreement/dup** — the agree pair (0.828)
   sits *inside* the contradiction range. No threshold separates them. ⇒ a precision
   judge (LLM/cross-encoder) is mandatory; the cosine stage can only do recall.
3. **A fixed cosine threshold is brittle + model-dependent** — paraphrase-detection optimal
   thresholds span **0.33–0.87** across models (MDPI study). And the same floor must serve
   dups, paraphrases, contradictions, and merely-related facts, which all overlap in
   ~0.45–0.9.

## 2. Deduper today + the proposal

- **Today:** `Deduper(threshold=0.95)` → merge on exact match OR cosine ≥ 0.95. So it only
  ever catches near-identical text; real paraphrases (~0.65) never merge. 0.95 is both the
  *wrong task* (near-exact, not paraphrase) and a *model-dependent* magic number.
- **Proposal (`...-semantic-dedup-recall-gate-llm-judge.md`):** two-stage —
  - **recall gate:** loose cosine (~0.45) to surface merge *candidates* (high recall, model-
    drift forgiving; precision NOT its job);
  - **LLM merge-judge:** structured `{same_lesson: bool, keep_id}` — merge into the chosen
    **verbatim** survivor or keep both. (The eval cases require keeping verbatim text, not
    LLM-rewriting/distillation.)
  - **offline:** a **merge-verdict cassette** (same pattern as the embedding cache; committed,
    model-keyed, loud-miss), so the dedup evals run deterministically in CI; skip when no
    cassette + no key (like ConflictFlagger).
  - **effect:** `ingestion_merge_near_dupes` + `skills_merge_dedup` flip XFAIL→PASS *honestly*
    (system actually merges), and the cosine number becomes a loose recall gate, not the
    precision knob.

## 3. ConflictFlagger today (what we learned)

`ConflictFlagger(llm=None, similarity_floor=0.6)`:
```
apply(decision, store):
  if no llm / decision.dropped / decision.action == "update": return   # skip merged dups
  for hit in store.most_similar(decision.text, k=3):
      if hit.score < similarity_floor: break                            # recall gate (0.6)
      ans = llm.complete("Does NEW contradict EXISTING? yes/no")        # LLM judge
      if ans.startswith("yes"): decision.flags.append(f"contradiction:{hit.fact.id}")
```

- **It is already the recall-gate + LLM-judge architecture** the dedup proposal wants —
  `Deduper` is the laggard. (Strengthens the proposal: precedent in the same file.)
- **Complexity — it does NOT do N LLM calls per insert.** `most_similar(k=3)` funnels N
  facts → top 3; the 0.6 floor trims further; so **≤ 3 LLM calls per insert, usually 0**
  (zero when nothing is similar). Total LLM over N inserts = O(N·k) = **O(N)**.
- **The N² is in the cosine scan, not the LLM.** `VectorGraph.search` is brute-force O(N)
  per insert → O(N²) total — but cheap float math. Fix = **ANN index** (HNSW/IVF/sqlite-vec/
  LanceDB) → O(log N)/query → O(N log N). Already on `VectorGraph`'s roadmap (docstring).
- **Only flags, doesn't resolve** (`ConflictOverwriter` is the resolving variant). Surfaced
  for human review (dashboard "Contradictions" tab).
- **Blind spot:** the 0.6 floor **misses differently-worded contradictions** — the
  perf-vs-readability contradiction scored **0.454 < 0.6**, so the gate breaks before the
  LLM ever sees it. Lowering the floor catches more but costs more LLM calls.
- **Smells:** brittle `startswith("yes")` parsing (vs structured output); text re-embedded
  ~3×/write (Deduper.most_similar, ConflictFlagger.most_similar, _add).

## 4. Open agenda for the fresh chat

1. **Recall-floor calibration + the contradiction blind spot.** Set the floor purely for
   recall (accept false positives, LLM filters). But the 0.454 contradiction shows **some
   genuine contradictions don't cluster above *any* reasonable topical floor** — is there a
   recall mechanism for conflicts beyond cosine kNN? (This is the deepest open question.)
2. **Unify Deduper + ConflictFlagger.** Both are `most_similar → pair-judge`. One candidate-
   recall pass dispatching to a merge-judge + a conflict-judge, instead of two steps with
   inconsistent floors (0.95 vs 0.6). Preserve the ordering interaction (don't conflict-check
   a just-merged dup; `action=="update"` skip).
3. **Policy placement / cost.** Should per-write LLM calls (conflict + future merge) sit on
   the **synchronous write path** at all? Options: defer to an async/batched pass, or a
   background **compaction** job (cf. SemDeDup's batch k-means clustering). Plus the
   ANN-index swap for the N² search.
4. **Verify stage: LLM judge vs cross-encoder.** Cross-encoder = cheaper, more deterministic,
   but adds a model dep and can't pick the verbatim survivor or explain itself. LLM judge fits
   the existing stack (`ConflictFlagger` precedent) + the cassette. (Two-stage recall→verify
   accuracy: rerank ARI 93.7 vs bi-encoder 91.5 vs LSH 73.7.)
5. **Dedicated similarity embedder.** MPNet/sentence-transformers have more separable, better-
   documented thresholds than a general retrieval embedder; would tighten the recall gate and
   *might* help the contradiction blind spot. Relevant mostly if we ever go threshold-only;
   under two-stage it's a smaller lever. (Bigger decision: changes the whole embedding stack.)
6. **Harden + make conflict evals deterministic.** Structured output for the judge (kill
   `startswith("yes")`); a **verdict cassette** for the conflict cases too; embed-once-per-write.

### 4.1 Recall mechanisms beyond cosine kNN (the deepest open question)

The 0.454 perf-vs-readability pair is **not the same failure** as the 0.77–0.89 negation
contradictions, even though the §1 table lists them adjacently:

- **Negation cases (high cosine) fail the *precision* stage** — contradiction sits inside the
  agreement range, so no threshold separates "X" from "not X". Fix = the LLM/cross-encoder judge.
- **The 0.454 case fails the *recall* stage** — the pair is never surfaced as a candidate, so the
  judge never runs. And it fails for a specific reason: the two facts share a *latent* axis
  (the perf-vs-maintainability tradeoff) but almost no surface or topical proximity. **No
  topical-similarity metric — any embedder, any threshold — reliably clusters them, because they
  are not topically close.** A better vector signal does not fix this; recall has to move off
  pairwise topical similarity.

That split rules some popular-sounding options *out* for this case:

- **NLI cross-encoders** (`cross-encoder/nli-deberta-v3-large`, RoBERTa-MNLI) are **verify-only** —
  they need a pair as input and can't generate candidates without going O(N²). Great as the §4.4
  judge; useless for *surfacing* the 0.454 pair. (The trap: do not move them upstream to recall.)
- **Contradiction-aware embeddings** — **SparseCL** (arXiv 2406.10746, Jun 2024) is the closest-named
  prior art ("contradiction retrieval"), but its recall stage is *still* cosine ANN over top-K; the
  sparsity score only **reranks** what cosine already surfaced. If 0.454 is below the K cutoff,
  SparseCL never sees it either. **DiffCSE** is documented to fail on exactly low-overlap/negation pairs.
- **Instruction-tuned embedders** (Instructor, E5-instruct, Voyage, nomic) shift the space with a
  task prompt but show no evidence of bridging *cross-domain* gaps — still one topical signal.

On the recall side, one intuitive lever turns out **discredited by direct test** and one **holds up** —
the surviving fix is multi-key blocking (item 2), with entity-blocking as its proven key:

1. **HyDE-for-contradiction — intuitive but DIRECTLY TESTED AND FAILED; do not pursue.** The idea:
   at write time, one LLM call per fact (*"state the claim that would contradict this"*), embed that
   counter-claim as a second vector, and search with both — so "prioritize raw performance" generates
   "favor readability over optimization," which now shares vocabulary with the stored opposing fact.
   It *sounds* like the classic IR vocabulary-mismatch → query-expansion fix in LLM form. **But a 2026
   study tested almost exactly this** ("Negation is Not Semantic," arXiv 2603.17580): its Variant 4
   generated negation-heavy counter-claim queries, embedded them, and dense-retrieved → **catastrophic
   failure, MRR 0.023.** Failure mode = **"Semantic Collapse"**: the embedder maps by topic "regardless
   of epistemic polarity," so it confuses topical similarity with logical entailment *even when handed
   the generated negation* — i.e. **§1's "topicality funnel" biting again, not routed around.** Caveat:
   that study is high-overlap biomedical QA, not our low-overlap value-tradeoff case, so it's strong
   adjacent-domain evidence, not an identical refutation — but the failure mechanism is exactly the one
   we already documented, so treat the lever as discredited unless re-validated on our setting.
   *Partial salvage:* what won in that study was **negation-cue filtering, not the lexical backbone** —
   Dense+NegEx (weighted MRR 0.779) ≈ BM25+NegEx (0.790); the decisive ingredient is the NegEx filter
   (23 hand-written clinical negation patterns), and the catastrophic 0.023 was specifically V4's
   counter-claim *generation*, not dense retrieval per se. Caveats from a full read: it's a **preprint**
   (TREC 2025 BioGen competition track, not peer-reviewed), dev set is **188 biomedical claim-doc pairs**
   (SciFact), and NegEx **requires explicit surface negation markers** ("absence of", "no evidence of").
   Worth a blocker in the union (item 2) for *negation-marked* contradictions, but it **explicitly
   misses** the 0.454 case — the paper reports **no** method retrieving contradictions that lack *both*
   lexical overlap *and* a negation cue, and only **3/8** human-evaluated systems scored non-zero on
   contradiction at all. That is independent primary confirmation that the implicit-contradiction case
   is unsolved field-wide, not just here.
2. **Multi-key blocking / union of cheap recall generators (highest *benchmarked* leverage).** The
   entity-resolution gold rule: *blocking matters more than matching* — union several cheap,
   imprecise candidate generators and let the expensive judge filter. Add 2–3 doors alongside
   cosine-ANN: shared canonical **topic-tag** blocking (an LLM/controlled-vocab tag like
   `code-quality-tradeoff` — both facts collide regardless of wording), shared **entity/aspect**
   blocking, plus the existing cosine-ANN. Union the candidate sets; each door has independent
   blind spots, and the union closes the 0.454 gap without raising judge calls proportionally.
   (This is §4.2's unification, generalized: recall should be a *union of cheap blockers*, at least
   one of them semantic-canonical rather than topical — not a single similarity number.)

Research bet, not near-term: **aspect-stance canonicalization** — normalize each fact to
`(aspect, stance)`, e.g. `(perf-vs-readability, performance)` vs `(perf-vs-readability, readability)`,
and conflicts become an index lookup (same aspect, opposing stance). Cleanest in theory; CLAIMSPECT
(arXiv 2506.10728, ACL 2025) ships only a benchmark-tied research reference impl (`pkargupta/claimspect`,
not a library). **A real published instantiation of this shape exists** — **ASDC** (Anchor-Guided
Semantic Double-Clustering, ACIT 2025): "anchors from question–answer semantics" drive blocking, so
contradiction = *same anchor, conflicting answer* — essentially `(anchor, stance)`. Claims recall
92%→~99% (vs an LSH baseline) cutting 1.2M comparisons to 15.6k. **BUT unverified on our hard case:**
paywalled (IEEE/ResearchGate, no open preprint — could not read method or benchmark), its candidate
reduction still leans on "semantic vector similarity and lexical filtering," and beating a *lexical*
LSH baseline says little about low-*topical*-overlap implicit contradictions. So the shape is real and
deployed; whether it catches the 0.454 case is **unconfirmed**. **2026 update — the technique splits in two:** (a) the *upstream half* — LLM
claim-normalization → atomic facts + **entity extraction/linking** — is now **production-proven**
(mem0's 2026 memory does entity-collection + entity-match retrieval; TriMem, arXiv 2605.19952, keeps
raw/atomic-fact/profile granularities). This **de-risks the entity-match blocking key** in the
multi-key lever above — promote it from speculative to first-pick. (b) The *downstream half* —
opposing-stance contradiction via a canonical index — **remains unsolved even in production**: mem0
files conflict/"memory staleness" under *open problems*. And SemEval-2026's DimStance pushes stance
toward **continuous valence-arousal** rather than discrete labels, which erodes the clean "opposing
stance = exact index collision" formulation (it becomes a threshold in stance space). Takeaway: bank
the entity-blocking half now; treat the aspect-stance index-lookup as a research bet whose tidy form
may not be where the field lands.

**This applies symmetrically to dedup (§2), not just conflicts** — see below. A paraphrase duplicate
written in disjoint vocabulary can fall below the recall floor for the *same* reason. The HyDE lever
flips to generating *paraphrases* instead of counter-claims; multi-key blocking helps unchanged; and
aspect-stance canonicalization unifies both — *same aspect + same stance = duplicate, same aspect +
opposing stance = contradiction* — which is exactly the §4.2 merge-judge/conflict-judge dispatch.

## 5. Prior art (sources)

- Two-stage recall→verify is the high-precision standard:
  [Noise-Robust De-Duplication at Scale](https://arxiv.org/pdf/2210.04261),
  [Dual vs Cross-Encoder](https://dev.to/krunalkanojiya/dual-encoder-vs-cross-encoder-why-your-rag-pipeline-needs-both-4bd)
- Fixed cosine thresholds are model-dependent (0.33–0.87):
  [MDPI paraphrase study](https://www.mdpi.com/2073-431X/14/9/385)
- Clustering batch dedup (alternative shape, offline curation):
  [NVIDIA SemDeDup](https://docs.nvidia.com/nemo-framework/user-guide/24.09/datacuration/semdedup.html)
- Lexical MinHash/LSH (scale gold-standard, lexical-only — misses paraphrases):
  [BigCode dedup](https://huggingface.co/blog/dedup)
- Low-overlap recall (re: §4.1) — contradiction-retrieval reranker, *not* a recall fix:
  [SparseCL (arXiv 2406.10746)](https://arxiv.org/abs/2406.10746);
  negation-aware embedding baseline: [DiffCSE (arXiv 2204.10298)](https://arxiv.org/pdf/2204.10298)
- NLI cross-encoders are verify-only (can't generate candidates):
  [cross-encoder/nli-deberta-v3-large](https://huggingface.co/cross-encoder/nli-deberta-v3-large),
  [NLI cross-encoder use cases](https://huggingface.co/blog/dleemiller/nli-xenc-ways-to-use)
- HyDE / query-expansion as the vocabulary-gap bridge (the *intuition* behind HyDE-for-contradiction):
  [HyDE explainer](https://zilliz.com/learn/improve-rag-and-information-retrieval-with-hyde-hypothetical-document-embeddings)
- **Counter-claim *generation* FAILS for contradiction retrieval (V4, MRR 0.023, "Semantic Collapse");
  negation-cue filtering — NegEx — is the active ingredient, dense+NegEx 0.779 ≈ BM25+NegEx 0.790; no
  method retrieves contradictions lacking both lexical overlap AND a negation cue (preprint, 188-pair
  biomedical SciFact):** [Negation is Not Semantic (arXiv 2603.17580, 2026)](https://arxiv.org/html/2603.17580)
- Anchor/aspect→stance blocking for KB contradiction (real but paywalled, hard-case unverified):
  [ASDC, ACIT 2025 (IEEE 11185903)](https://ieeexplore.ieee.org/abstract/document/11185903/)
- Entity-resolution blocking is a mature, recall-measured field (grounds the multi-key lever):
  [Blocking & Filtering for ER: A Survey (ACM CSUR 2020)](https://dl.acm.org/doi/abs/10.1145/3377455)
- Proposition/aspect canonicalization:
  [Dense X Retrieval (arXiv 2312.06648)](https://arxiv.org/html/2312.06648v2),
  [CLAIMSPECT aspect-stance (arXiv 2506.10728)](https://arxiv.org/pdf/2506.10728),
  [reference impl (pkargupta/claimspect)](https://github.com/pkargupta/claimspect)
- 2026 production agent-memory (entity-blocking proven; conflict still open):
  [mem0 State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026),
  [TriMem (arXiv 2605.19952)](https://arxiv.org/abs/2605.19952),
  [SemEval-2026 DimStance (continuous stance)](https://groups.google.com/g/ML-news/c/6_QsHAPsZt4)

## 6. Reusable principles (saved to agent memory)

- **eval-assert-actual-behavior** — tests assert the system's real shipped behavior, never a
  per-case-tuned global constant; *masking vs isolation* distinction (a per-case override is
  fine only if production's real config would also pass).
- **The cassette pattern** (record real model outputs once, commit, replay offline,
  loud-miss on staleness) generalizes from embeddings → merge/conflict verdicts.
