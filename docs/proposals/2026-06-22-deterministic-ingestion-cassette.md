# Proposal: deterministic ingestion via a replay cassette (and a unified keyed-replay surface)

**Owner:** Dominic Antonelli — eval harness / knowledge graph
**Status:** Implemented — see [`specs/002-deterministic-ingestion-cassette/`](../../specs/002-deterministic-ingestion-cassette/spec.md) (`IngestionCassette` + the `ingest_replay` gate + the `ingestion_cache --refresh` regenerator). The **unified keyed-replay surface** sketched in §5 remains deferred, and only the **first** of the two `model-robust-recall-policies` FR-030/SC-013 prerequisites lands here — the second, active-fact retrievability (`ingest_state: active`), is the explicit next follow-up.
**Date:** 2026-06-22
**Scope:** the eval `ingest_model` path (`PromptIngestor`'s injected LLM), a committed ingestion cassette, the application eval suite's embedder axis, and the relationship to the existing embedding cache + the proposed merge/conflict verdict cassettes
**Relates to:** [`2026-06-22-semantic-dedup-recall-gate-llm-judge.md`](2026-06-22-semantic-dedup-recall-gate-llm-judge.md) and [`2026-06-22-unified-dedup-conflict-recall.md`](2026-06-22-unified-dedup-conflict-recall.md) (which add merge/conflict verdict cassettes), and the `model-robust-recall-policies` spec (FR-015/SC-007, FR-030/SC-013)

> The application eval suite runs a real LLM ingestion splitter (`ingest_model: gpt-4o-mini`)
> whose output text **varies run-to-run even at temperature 0**. Because the embedding cache
> keys on `(model, text)`, nondeterministic ingested text can't be replayed — which is why
> those cases are forced onto `embedder: live` (a bare, *uncached* `OpenRouterEmbedder`). The
> result: the same text is embedded 2–3× per write, every embed is a live API call, and the
> knowledge-graph the agent sees is irreproducible. This proposes the production-standard fix —
> a **text→text replay cassette** over the ingestion LLM (the same committed/keyed/loud-miss
> pattern as the embedding cache) — and notes the opportunity to unify it with the embedding
> and verdict cassettes behind one keyed-replay surface.

---

## 1. Problem

The real-LLM-ingestion change (PR #14, `9478b69`) wired the application cases to
`substrate: vector`, `embedder: live`, `ingest_model: gpt-4o-mini`. Two coupled facts make
those runs nondeterministic and expensive:

1. **The ingestion splitter is nondeterministic.** `OpenRouterLlm.complete` already defaults to
   `temperature=0.0`, yet gpt-4o-mini over OpenRouter is not byte-stable across calls (backend
   nondeterminism, routing). So `PromptIngestor.synthesis` emits a *different set of atomic
   insight strings* on each run.
2. **The embedding cache can only replay deterministic text.** `CachedEmbedder` keys on
   `sha256(model + "\n" + text)` and treats a miss-without-recording as a **loud error**. With
   the embedded strings changing every run, a committed embedding fixture would miss constantly.
   So application cases use `embedder: live` — a bare `OpenRouterEmbedder` with **no cache
   wrapper at all** (only `embedder: cached` wraps live in `CachedEmbedder`).

Consequences:

- **Cost.** The write path embeds the incoming text 2–3× per write (Deduper search +
  ConflictFlagger search + the store embed). Under `live`, every one of those is a real API
  call, multiplied across many insights per case.
- **Irreproducibility — the suite can't measure anything.** The knowledge graph the agent reads
  differs run-to-run, adding variance on top of the already-nondeterministic agent + judge. This is
  not merely noisy — it makes the application suite unusable as a measurement instrument. A
  2026-06-22 A/B probe (whole-graph reader vs `reader: retrieving` on the three failing application
  cases) found that facts a check asserts (`billions`, `bentoml/mlops`) were in the graph one run and
  *gone* the next, purely from re-distillation — so a pass/fail swing can't be attributed to the
  policy under test. **This makes the cassette a gating prerequisite** for the
  `model-robust-recall-policies` spec's application-suite validation (its FR-030/SC-013), not just a
  cost optimization: until ingestion is deterministic, reader/dedup/conflict effects on
  `matt/applications/*` are unattributable. (The same probe did confirm the reader mechanically works
  — context shrank ~11k→~1k chars, no rubric loss, one case flipped FAIL→PASS — a signal only
  reproducible once this lands.)
- **No deterministic component cases on real distilled text.** We can't build offline
  dedup/conflict eval cases from *real* LLM-distilled atomic insights, because the distillation
  wobbles. Today such cases must seed verbatim strings (`via_ingestor`, no `ingest_model`).

## 2. What this is NOT

- **Not a recall-policy change.** Orthogonal to the reader cutoff / dedup recall gate / conflict
  recall work — this is eval-infrastructure determinism.
- **Not full application-case determinism.** Application cases still run a live Claude Code agent
  (sandbox) and a judge, both nondeterministic. This proposal makes the *ingestion → embedding →
  graph-construction* layer deterministic and cheap; it does not make the whole case offline or
  reproducible end-to-end. The headline win is cost + reproducible graph state + unlocking
  deterministic component-level cases, not "the application suite runs in CI."
- **Not a temperature fix.** Temp 0 is already the default and is insufficient on its own.

## 3. Design: an ingestion replay cassette

Mirror the embedding cache exactly, one layer up.

### 3.1 The cassette

A committed JSON keyed `sha256(ingest_model + "\n" + raw_input) -> output_text`, replayed
offline, recorded on a miss only when a key + `allow_compute` is present, **loud miss** on a
stale fixture (a seeded input or the ingest model changed without a refresh). This is the
`CachedEmbedder` contract applied to a `str -> str` LLM call.

```
key   = sha256(f"{ingest_model}\n{raw_input}")
hit   -> replay committed output_text (deterministic, offline)
miss  -> if allow_compute: call the live ingest LLM, record, save; else: loud error
```

### 3.2 Wiring

`run._ingest_llm_for` currently returns a bare OpenRouter-backed `str -> str` lambda when
`ingest_model` is set. Wrap that lambda in the cassette (same shape as how `_eval_embedder`
wraps `live` in `CachedEmbedder` for `cached`). No change to `PromptIngestor` — it still receives
a `str -> str` callable.

### 3.3 Regeneration

A `--refresh` regenerator (mirror `knowledge/evals/embed_cache.py`): with a key set, delete the
cassette, re-run every `ingest_model` case so the recorder captures exactly the inputs those
cases distill, then commit. Cassette path e.g. `knowledge/evals/fixtures/ingestion/<model-slug>.json`.

### 3.4 Unlocking `cached` embeddings for the application suite

Once ingestion replays deterministically, the embedded strings become stable, so application
cases can flip `embedder: live -> cached`. Ordering for a refresh: **record the ingestion
cassette first** (so the text is fixed), **then** refresh the embedding cache (so it records the
now-stable strings). Both need a live key locally; CI then replays both offline for the
graph-construction layer.

## 4. Why this fits praxis

- **Reuses the established cassette pattern** (`CachedEmbedder`) — committed, model-keyed,
  loud-miss, record-with-key, skip/loud when unavailable.
- **Rhymes with the in-flight verdict cassettes.** The dedup/conflict proposals add merge-verdict
  and conflict-verdict cassettes — the *same* keyed-replay shape over an LLM call. Ingestion is a
  fourth surface (embeddings, ingestion, merge verdicts, conflict verdicts).
- **Composes with FR-015.** "Embed once per write" cuts the per-write embed count regardless of
  caching; this proposal makes the remaining embeds free on replay and the inputs stable.

## 5. The unification opportunity (optional, call it out — don't over-build)

Four surfaces now want the identical contract: `sha256(model + payload) -> result`, committed,
loud-miss, record-with-key, skip-when-unavailable. There is a real case for **one keyed-replay
cassette abstraction** parameterized by (a) the key inputs and (b) the value codec
(float32-packed vectors for embeddings; raw text for ingestion; small structured verdicts for
merge/conflict). Recommendation: **don't build the abstraction first.** Land the ingestion
cassette as a near-copy of `CachedEmbedder`, let the merge/conflict verdict cassettes land from
their own proposals, then extract the common surface once three concrete instances exist and the
shared shape is proven — not speculatively.

## 6. Effect on the evals (honest)

| surface | today | after |
|---|---|---|
| application cases (`matt/applications/*`) | `embedder: live`, uncached; nondeterministic graph; live embeds 2–3×/write | `ingest_model` replays from cassette; eligible for `embedder: cached`; graph-construction layer deterministic + offline; replay embeds free. **Agent + judge still nondeterministic** |
| component dedup/conflict on real distilled text | not possible (distillation wobbles) | possible — build offline cases from cassetted real-LLM insights |
| seeded cases (`direct_to_graph` / `via_ingestor`, no `ingest_model`) | deterministic already | unchanged |

## 7. Implementation sketch

1. `IngestionCassette` (new): near-copy of `CachedEmbedder` with a `str -> str` value codec;
   `sha256(model + input)` key; load/save; loud-miss; `allow_compute` gate.
2. Wrap the ingest LLM in `run._ingest_llm_for` with the cassette (parallel to `_eval_embedder`'s
   `cached` branch).
3. Regenerator `knowledge/evals/ingestion_cache.py --refresh` (mirror `embed_cache.py`); cassette
   under `knowledge/evals/fixtures/ingestion/<model-slug>.json`.
4. Flip application cases `embedder: live -> cached`; record the ingestion cassette, then refresh
   + commit the embedding cache for the now-stable strings.
5. Tests: cassette replay / record / loud-miss; ordering (ingest cassette feeds embedding cache);
   a stub-LLM unit test for record-then-replay.

## 8. Risks & open questions

1. **Fixture size.** Committing distilled outputs + their embeddings for the whole application
   suite could be sizable. Mitigate with the packed codec (embeddings already do this) and
   scoping which cases flip to `cached`.
2. **Staleness ergonomics.** Editing a seeded application input or bumping `ingest_model` is now a
   two-step refresh (ingestion, then embeddings). The loud-miss makes this safe but it must be
   documented.
3. **Partial determinism, clearly communicated.** Because agent + judge stay nondeterministic, do
   not oversell this as "application suite in CI." Its scope is the graph-construction layer.
4. **Unify now or later?** §5 — recommend later, after three concrete cassettes exist.
5. **Does any non-eval/serve path want the same determinism?** Out of scope; this proposal is
   eval-harness only.
