# Proposal: semantic dedup via a recall gate + LLM merge-judge

**Owner:** Dominic Antonelli — knowledge graph / write policy
**Status:** Proposed
**Date:** 2026-06-22
**Scope:** `knowledge/knowledge_graph/write_policy` (the `Deduper` step), the two paraphrase-dedup eval cases, and an offline merge-verdict cassette

> `Deduper(threshold=0.95)` can't merge paraphrases — measured paraphrase similarity is
> ~0.65 and the threshold is a *near-exact* cutoff. Worse, any fixed cosine threshold is
> **model-dependent** (optimal paraphrase thresholds span 0.33–0.87 across models), so
> tuning a single number is a treadmill. This proposes the production-standard fix:
> **two stages** — a *loose* embedding recall gate that only has to find candidate dups,
> and an **LLM merge-judge** that decides "same lesson? merge or keep both, keep which
> wording." Precision lives in the judge, not in a brittle threshold; offline runs replay
> a committed verdict cassette (the embedding-cache pattern again).

---

## 1. Problem

`VectorGraph`'s `Deduper` collapses a write into an existing fact when
`top.score >= threshold` (or exact match). With `threshold=0.95` that only ever fires
on near-identical text. Two structural problems:

1. **Wrong task.** 0.95 is a near-exact cutoff. Real paraphrases of the same lesson land
   far lower. Measured on `text-embedding-3-small`
   (`skills_merge_dedup` / `ingestion_merge_near_dupes`): shared idea **0.65–0.67**,
   distinct ideas **0.31**. So 0.95 cannot merge them; it isn't even close.
2. **Model-dependent.** A paraphrase-detection study across transformer models found the
   *optimal* threshold ranges **0.334–0.867** by model (MPNet best ≈ 0.671). There is no
   portable number; a pinned threshold is wrong the moment the embedder changes.

Consequence today: `ingestion_merge_near_dupes` and `skills_merge_dedup` are honest
**XFAIL**s — the system genuinely doesn't merge paraphrases. We
[rejected](2026-06-21-retrieving-reader-semantic-retrieval.md) a per-case threshold
override as a fake pass (production would still fail). The honest fix is to change how
dedup works, globally.

## 2. Why not just calibrate one threshold?

The obvious move — pick the cutoff empirically (ROC/F1 on labeled dup/non-dup insight
pairs) per model, pin it, document it — is legitimate and is what batch curation does
(SemDeDup uses an ε threshold inside k-means clusters). For *our* model+corpus it would
land ~0.5 (between 0.31 and 0.65). But it stays a **single global scalar** that:

- must be recalibrated on every embedder change (the treadmill),
- is brittle near the boundary (a 0.55 paraphrase and a 0.45 "related but distinct" pair
  straddle any line), and
- can't express "keep the *verbatim* survivor" — which the eval cases explicitly require
  (`skills_merge_dedup` warns that LLM *distillation* which rewrites text is **not** the
  green path).

So calibration is a band-aid. The standard high-precision answer is two-stage.

## 3. Design: recall gate → merge-judge

Split the decision into a cheap, forgiving recall stage and a precise verify stage —
the bi-encoder-then-cross-encoder pattern, with an LLM as the verifier.

### 3.1 Stage 1 — embedding recall gate (loose)

Keep the cosine search, but its only job is **don't miss a true dup** (high recall). Use
a *loose* gate (`RECALL_FLOOR`, ~0.4–0.5) to pull merge **candidates**:

```
candidates = [h for h in graph.most_similar(text, k=K) if h.score >= RECALL_FLOOR]
```

Because it's high-recall, the value is forgiving of model drift — it need not separate
dup from non-dup, only surface plausibles. On our data a 0.45 floor catches the 0.65
paraphrases with margin; false positives (the 0.31 distinct pairs) are fine — Stage 2
rejects them.

### 3.2 Stage 2 — LLM merge-judge (precise)

For each candidate, an LLM answers a tight question:

```
schema: { "same_lesson": bool, "keep_id": "<existing fact id> | null" }
prompt: "Do these two notes record the SAME lesson/rule, just phrased differently?
         If yes, which EXISTING note should survive verbatim? Do NOT rewrite either."
```

`same_lesson: true` → `decision.action = "update"`, `update_target_id = keep_id`
(merge into the survivor, bump observation count — the existing merge path). `false` →
add as a new fact. This is the precision arbiter; **no cosine threshold decides the
merge**. It also answers "which wording survives" (verbatim), which a threshold can't.

This reuses the existing pattern: the write policy already runs an LLM
(`ConflictFlagger(llm=OpenRouterLlm())`), and `most_similar` is already how it finds
candidates. The merge-judge is its sibling (see §8 — they likely share candidate-finding).

### 3.3 Offline determinism — a merge-verdict cassette

The judge is nondeterministic and keyed, exactly like embeddings. Reuse the cassette
pattern from the [embedding cache](2026-06-21-retrieving-reader-semantic-retrieval.md):
a committed JSON of `{ sha256(model + textA + textB) -> {same_lesson, keep_id-role} }`,
replayed offline, recorded with a key, **loud miss** on a stale fixture. So the dedup
evals run in CI deterministically against real verdicts, no live LLM.

Graceful degradation matches `ConflictFlagger`: no cassette and no key → skip semantic
merge (exact-dedup still works); the case SKIPs rather than mis-runs.

## 4. Why this fits praxis

- **Reuses the LLM-in-write-policy precedent** (`ConflictFlagger`) — same wiring, same
  offline-skip, same minimal-policy considerations.
- **Matches the eval contracts verbatim.** `skills_merge_dedup` needs one *verbatim*
  survivor kept and distinct ideas preserved; a judge that *selects* (not rewrites) does
  this, and the `distinct_ideas_survive` guard catches over-merge.
- **Model-robust.** An embedder swap only shifts the recall floor (forgiving); it doesn't
  silently corrupt precision.

## 5. Effect on the evals (honest)

| case | today | after |
|------|-------|-------|
| `ingestion_merge_near_dupes` | XFAIL (0.95 won't merge 0.65) | **PASS** — judge merges the two phrasings; the surviving "use uv run pytest" appears once |
| `skills_merge_dedup` | XFAIL (no paraphrase merge) | **PASS** — judge merges the shared idea, keeps both distinct ideas verbatim |
| `ingestion_dedup` | PASS (exact) | unchanged — exact path still short-circuits |

They flip because the **system actually merges now**, tested against real (cassetted)
verdicts — not because of a tuned threshold. The cosine threshold becomes a loose,
documented recall gate, not the precision knob.

## 6. Prior art

- **Two-stage recall→verify is the high-precision standard.** Bi-encoder proposes,
  cross-encoder/LLM confirms; measured ARI 93.7 (rerank) vs 91.5 (bi-encoder) vs 73.7
  (LSH) ([Noise-Robust De-Duplication at Scale](https://arxiv.org/pdf/2210.04261),
  [Dual vs Cross-Encoder](https://dev.to/krunalkanojiya/dual-encoder-vs-cross-encoder-why-your-rag-pipeline-needs-both-4bd)).
- **Fixed cosine thresholds are model-dependent** — optimal paraphrase thresholds span
  0.33–0.87 ([MDPI paraphrase study](https://www.mdpi.com/2073-431X/14/9/385)).
- **Threshold-in-cluster batch dedup** is the alternative shape (offline curation), not
  incremental write-time ([NVIDIA SemDeDup](https://docs.nvidia.com/nemo-framework/user-guide/24.09/datacuration/semdedup.html)).
- **Lexical MinHash/LSH** is the scale gold-standard but lexical-only — misses paraphrases
  ([BigCode dedup](https://huggingface.co/blog/dedup)).

## 7. Implementation sketch

1. `Deduper`: keep exact-match short-circuit; replace the `score >= threshold` branch with
   a `RECALL_FLOOR` candidate gate + a `MergeJudge` call. Rename `threshold` → `recall_floor`
   to reflect its new (loose) role.
2. `MergeJudge` (new write-policy helper): structured LLM call (`same_lesson`, `keep_id`),
   `None`/skip when the LLM is unavailable, verdict cassette for offline replay.
3. Cassette: `knowledge/evals/fixtures/merge_verdicts/<model-slug>.json`; a
   `--refresh`-style regenerator (mirrors `embed_cache`).
4. Cases: drop the `xfail` from `ingestion_merge_near_dupes` / `skills_merge_dedup`; they
   ride the new `Deduper`. Generate + commit the verdict cassette.
5. Tests: a stub-judge unit test (merge true/false, keep_id selection, distinct preserved);
   cassette replay/loud-miss/record; recall-gate-then-judge ordering.

## 8. Risks, alternatives, and the `ConflictFlagger` question

- **Cost/latency on writes** — only call the judge for candidates above the recall floor;
  batch; the cassette makes evals free.
- **Cross-encoder instead of an LLM judge** — a sentence-pair cross-encoder is cheaper and
  more deterministic than an LLM, but adds a model dependency and can't pick the verbatim
  survivor or explain itself. LLM-judge fits the existing stack; cross-encoder is a
  fallback if cost bites. (Open.)
- **Dedicated similarity embedder** (MPNet/sentence-transformers) would make even a
  threshold-only approach more separable; relevant only if we *don't* go two-stage. (Open.)
- **Relationship to `ConflictFlagger`.** Both steps are the same shape: `most_similar` →
  LLM question about a pair. Dedup asks "same lesson?"; conflict asks "do these
  contradict?". They likely should **share candidate-finding** and possibly a single
  pair-judge surface. The exact unification (one step vs two, ordering: merge before or
  after conflict-flag) is its own discussion — deferred, but this proposal should not
  cement a shape that blocks it.

## 9. Open questions

1. **`RECALL_FLOOR` value.** ~0.45 for `text-embedding-3-small`; it's a recall gate so err
   low. Still model-tied, but forgiving (unlike a precision threshold).
2. **Judge model + cassette key.** Which LLM, and does the cassette key on the judge model
   id (yes — same staleness logic as embeddings)?
3. **Merge vs conflict ordering** and whether `MergeJudge`/`ConflictFlagger` unify (§8).
4. **Cross-encoder vs LLM judge** for the verify stage (cost/determinism vs fit/verbatim).
