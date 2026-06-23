# Proposal: unified dedup + contradiction recall (and the implicit-contradiction limit)

**Owner:** Dominic Antonelli — knowledge graph / write policy
**Status:** Implemented — `specs/001-model-robust-recall-policies` US3. Tier A: embed-once / one shared recall pass, structured `ConflictJudge` + cassette, merge-before-conflict. Tier B (gated experiment, owner KEPT 2026-06-23): `AspectTagger` + same-tag conflict recall, 7/8 below-floor implicit pairs rescued. Tier C (batch backstop) remains documented-only. Deterministic-ingestion follow-on: see [the cassette proposal](2026-06-22-deterministic-ingestion-cassette.md).
**Date:** 2026-06-22
**Scope:** the `Deduper` + `ConflictFlagger` write-policy steps, their shared candidate-recall
pass, a contradiction-specific recall key (experiment), and a documented residual + batch backstop

> Companion to [`2026-06-22-semantic-dedup-recall-gate-llm-judge.md`](2026-06-22-semantic-dedup-recall-gate-llm-judge.md)
> (the dedup recall-gate + merge-judge) and successor to the open agenda (§4) in
> [`2026-06-22-dedup-conflict-handoff.md`](../archive/2026-06-22-dedup-conflict-handoff.md). The dedup
> proposal is **not** superseded — it is endorsed and reused. This doc (1) unifies
> `ConflictFlagger` onto the same two-stage machinery, (2) adds the one recall key with a
> plausible shot at *implicit* contradictions, and (3) names — honestly — the case the field
> has not solved, with a batch backstop instead of a write-time silver bullet.

---

## 0. Relationship to the other two docs (read this first)

- **Recall→verify is the validated spine for BOTH steps.** Research confirms the two-stage
  pattern (loose embedding recall gate → precise LLM/cross-encoder judge) is the
  high-precision standard. The dedup proposal already builds it; this doc reuses its gate,
  judge, and cassette machinery rather than reinventing them.
- **Dedup needs nothing more.** Paraphrase dups (~0.65) are **high-topical / low-lexical** —
  exactly where a cosine recall gate (~0.45) and semantic blocking are empirically strong. Ship
  the dedup proposal as written; do **not** add a second recall key to it.
- **Contradiction is where the cosine gate breaks**, on one specific sub-case (§1). That is the
  whole reason this companion exists.

So: shared *machinery* from the dedup proposal; this doc adds the *unification* + the
*conflict-side recall* + the *honest limit*.

## 1. The problem this doc owns: implicit contradiction recall

`ConflictFlagger(similarity_floor=0.6)` is already recall-gate + LLM-judge — the right shape.
Its blind spot is **recall**, on one sub-case measured on `text-embedding-3-small`:

| relationship | example | cosine |
|---|---|--:|
| contradiction (negation) | "never commit secrets" / "fine to commit secrets" | 0.79–0.89 |
| **contradiction (implicit, different vocab)** | **"prioritize raw performance" / "readability over micro-optimizations"** | **0.454** |
| distinct ideas | "color token system" / "self-contained HTML" | 0.31 |

The negation contradictions score **high** — they pass recall and fail *precision* (the LLM
judge handles them). The implicit one scores **0.454**, below any usable floor, so it is never
surfaced as a candidate — a **recall** failure. And it sits barely above "distinct ideas"
(0.31), which is the trap: **you cannot fix this by lowering the cosine floor.** A floor low
enough to admit 0.454 admits nearly everything, and every admitted pair costs an LLM call. The
fix has to be a *different* recall signal, not a looser threshold.

### What the research ruled out (so we don't relitigate it)

- **HyDE / counter-claim generation — dead.** Generating each fact's negation and embedding it
  to retrieve contradictions was directly tested and **failed catastrophically** (MRR 0.023,
  "Semantic Collapse": embedders map by topic regardless of polarity). [arXiv 2603.17580](https://arxiv.org/html/2603.17580).
- **Negation-cue filtering (NegEx) — narrow.** What actually worked in that study was a negation
  *filter* (dense+NegEx 0.779 ≈ BM25+NegEx 0.790 — the filter is the active ingredient, not the
  backbone). But it **requires explicit negation markers** ("absence of", "no evidence of"),
  which the 0.454 case lacks. Useful only for the *negation-marked* subset.
- **No method solves the implicit case.** Same study: **no** approach retrieved contradictions
  lacking *both* lexical overlap *and* a negation cue; only **3 of 8** human-evaluated systems
  scored non-zero on contradiction at all. This is field-wide, not a praxis gap.
- **A better embedder won't save it.** The 0.454 pair has genuinely low *topical* proximity; no
  topical-similarity metric reliably clusters it. (This is §1 of the handoff — "cosine is a
  topicality funnel" — restated.)

## 2. Design

### Tier A — ship now (research-backed, low risk)

1. **Land the dedup proposal unchanged** (recall gate + `MergeJudge` + verdict cassette).
2. **Unify `Deduper` + `ConflictFlagger` onto one candidate-recall pass.** One
   `most_similar(text, k=K)` per write → dispatch each candidate to the merge-judge
   ("same lesson?") and the conflict-judge ("contradict?"). This kills the two ailments the
   handoff flagged: the **inconsistent floors** (0.95 vs 0.6 → one recall floor) and the
   **embed-3×-per-write** smell (embed once, reuse). Preserve the ordering interaction: merge
   first; a just-merged dup (`decision.action == "update"`) skips the conflict check.
3. **Harden the judges:** structured output (`{contradicts: bool, target_id}`) to kill
   `startswith("yes")`; a **conflict verdict cassette** (same committed/model-keyed/loud-miss
   pattern as embeddings and merge verdicts) so the conflict evals run deterministically in CI;
   skip-when-no-key graceful degradation, matching today's `ConflictFlagger`.
4. **Verify stage stays the LLM judge.** An NLI cross-encoder (e.g. `nli-deberta-v3-large`) is
   robust to vocabulary but is **verify-only**, can't pick the verbatim survivor, and adds a
   model dependency. Keep the LLM judge (fits the stack + cassette + is explainable); treat the
   cross-encoder as a cost-driven fallback only. *(Resolves the dedup proposal's open Q4.)*

### Tier B — one gated experiment (the 0.454 case; plausible, unvalidated)

5. **Add a second recall key for the *conflict* path only: canonical aspect/topic tags.** At
   write time, the policy LLM tags each fact with a small set from a growing controlled
   vocabulary (`code-quality-tradeoff`, `deploy-policy`, `secrets-handling`, …). Conflict
   candidates become `cosine-kNN ∪ same-tag`. This is the only key with a mechanism for 0.454:
   both "prioritize raw performance" and "readability over micro-optimizations" map to
   `code-quality-tradeoff` despite zero shared vocabulary. It is the multi-key-blocking principle
   from entity resolution (union of cheap recall generators; recall is the blocking stage's job,
   precision is the judge's). The published *shape* exists — ASDC's "anchor = QA-semantic unit,
   contradiction = same anchor / conflicting answer" is essentially `(aspect, stance)`.

   **This is the unproven part, stated as such.** No source we found shows tag/aspect blocking
   actually catches the *implicit* case; it lives or dies on whether the LLM reliably assigns the
   **same tag to both disjoint-vocab facts**. So it ships **with a kill criterion**:

   - Build a small eval set of implicit-contradiction pairs (the 0.454 pair + ~5–10 siblings:
     opposite value judgments, disjoint vocab, no negation cue).
   - Measure **tag co-assignment recall** — of known implicit-contradiction pairs, how many get a
     shared tag — and end-to-end flag rate through the judge.
   - **Kill if** tag co-assignment recall is poor (the LLM doesn't converge on shared tags) or the
     controlled vocabulary requires constant hand-curation to work. Cheaply learned either way.

### Tier C — accept and document (don't oversell)

6. **Name the residual.** Even with tags, some implicit contradictions will be missed (field-wide
   unsolved, per §1). Do **not** chase a write-time silver bullet — the research says there isn't
   one. The honest backstop is the **offline/async compaction** shape (handoff §4.3): a periodic
   batch job that clusters facts and does wider within-cluster comparison than the synchronous
   write path can afford. Contradiction specifically earns this because (a) it is the
   recall-starved case that needs a wider net, and (b) flagged conflicts surface to a review
   dashboard, so they tolerate batch latency. **Dedup does not need it** — its recall is already
   solved on the write path. Optionally, a NegEx-style negation-cue key can be added as a third
   recall door for the *negation-marked* subset, but it is marginal and does not touch 0.454.

## 3. Effect on the evals (honest)

| case | today | after |
|------|-------|-------|
| `ingestion_merge_near_dupes`, `skills_merge_dedup` | XFAIL | **PASS** via the dedup proposal (unchanged here) |
| existing negation-contradiction cases | rely on `ConflictFlagger` | unchanged behavior, hardened (structured output, cassette, single recall pass) |
| **new** implicit-contradiction cases (0.454 + siblings) | n/a | **provisional** — added by Tier B; PASS only if the experiment clears its gate, otherwise documented XFAIL (an honest "system can't recall this yet", not a tuned fake) |

Per [[eval-assert-actual-behavior]]: the implicit-contradiction cases are **provisional** until
Tier B's gate decides them — we assert real shipped behavior, not a per-case-tuned constant.

## 4. Prior art (this doc's additions)

- Counter-claim generation fails; negation-cue filtering is the active ingredient; implicit/no-cue
  contradictions unsolved: [Negation is Not Semantic (arXiv 2603.17580, 2026)](https://arxiv.org/html/2603.17580).
- Aspect/anchor→stance blocking for KB contradiction — real but paywalled, hard-case unverified:
  [ASDC, ACIT 2025 (IEEE 11185903)](https://ieeexplore.ieee.org/abstract/document/11185903/);
  [CLAIMSPECT (arXiv 2506.10728)](https://arxiv.org/pdf/2506.10728).
- Multi-key blocking is a mature, recall-measured discipline:
  "Blocking and Filtering Techniques for Entity Resolution: A Survey" (ACM CSUR 2020, doi:10.1145/3377455 — link blocks automated checks).
- 2026 production memory does atomic-fact + entity normalization (validates the upstream half of
  canonicalization; conflict still open): [mem0 State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026).
- (Two-stage recall→verify standard, model-dependent thresholds, SemDeDup batch shape — see the
  dedup proposal's prior-art list.)

## 5. Risks & open questions

1. **Tag vocabulary governance.** Controlled vocab that grows per write risks fragmentation
   (two tags for one aspect → no collision). Needs a merge/normalize step or a fixed seed
   taxonomy. This is the likeliest reason Tier B fails its gate.
2. **Write-path cost.** Tier A keeps judge calls bounded (candidates above the floor; usually 0).
   Tier B's tag key widens candidates for conflicts — bound it (cap same-tag candidates per
   write; or move conflict judging to the Tier C batch pass entirely).
3. **Where conflict judging runs.** Synchronous (flag on write) vs the §6/Tier C batch pass.
   Leaning: dedup synchronous (it changes what gets stored), conflict can be async (it only
   flags for review). Decides whether Tier B's tag key sits on the hot path at all.
4. **Cross-encoder vs LLM judge** — unchanged from the dedup proposal: LLM default, cross-encoder
   only if cost bites.

## 6. Implementation sketch

1. Build the dedup proposal's `MergeJudge` + recall gate first (its §7).
2. Refactor `Deduper` + `ConflictFlagger` to share one `most_similar` candidate pass + one
   recall floor; embed-once-per-write; structured-output both judges; conflict verdict cassette.
3. Preserve ordering (merge → skip-conflict-on-update).
4. *(Tier B, gated)* Add LLM tag assignment at write time + a same-tag candidate source unioned
   into the conflict recall; build the implicit-contradiction eval set; measure the gate; keep or
   drop.
5. *(Tier C, when needed)* Sketch the batch compaction pass; do not build until Tier B's result
   is known.
