# Proposal: relevance-ranking retrieval (`RetrievingReader` + real-embedder wiring)

**Owner:** Dominic Antonelli — eval harness
**Status:** Implemented — reader pair live & promoted (PASS/XFAIL in CI from the committed cache); end-to-end `live` demo and the paraphrase-dedup flips remain optional follow-ups
**Date:** 2026-06-21
**Scope:** `knowledge/wiring.py`, a new `graph_reader` variant, `VectorGraph` embedder injection, `knowledge/evals` (capability gating + the two `lost_in_middle*` cases)

> The `lost_in_middle` pair has carried the same xfail reason for its whole life:
> *"no relevance-ranking reader."* That reason is literally accurate — the only
> reader we ship (`WholeFileReader`) returns the entire graph unranked, so the
> buried `TODO(MD)` needle is always in (end-to-end) or the irrelevant facts are
> never dropped (component). This proposal builds the reader that flips both to a
> real green assertion: a **`RetrievingReader`** that calls `VectorGraph.search()`
> and returns only the top-k relevant facts, backed by a **real embedder** so the
> ranking is semantic rather than hash-noise.

---

## 1. Problem

Switching a case to `substrate: vector` changes where facts are *stored*; it does
nothing to how they're *retrieved*. Retrieval is entirely the reader's job, and
the only reader wired is `WholeFileReader`, whose `read()` calls
`graph.read(query)` — which on `VectorGraph` returns **all** fact texts
concatenated, query ignored ("context ignored; reader filters",
[vector_graph.py](../../../knowledge/knowledge_graph/knowledge_graph_variants/vector_graph.py)).
So:

- **`lost_in_middle_reader`** (component `graph_reader`, `substrate: vector`) asserts
  the reader keeps the relevant `TODO(MD)` rule and **drops** the irrelevant facts
  (X-Ray / SES / CloudFront). With `WholeFileReader` the whole graph comes back, so
  the `excludes_*` checks fail → XFAIL.
- **`lost_in_middle`** (end-to-end agent) buries the same needle in ~400 unranked
  policies; the reader dumps all of them into the agent's context, so the rule is
  lost-in-the-middle → XFAIL.

The ranking capability already exists on the substrate — `VectorGraph.search()`
does cosine top-k and satisfies `SearchableGraph` — but **nothing calls it**. And
even if something did, `VectorGraph` defaults to `FakeEmbedder` (hash-to-vector,
"no real semantics"), so cosine similarity would be ~noise and couldn't reliably
rank the relevant rule above the distractors.

So the gap is two pieces, both missing: a reader that *ranks*, and an embedder
that makes ranking *mean* something.

## 2. The constraints that shape the design

1. **The reader can't be swapped globally.** Several component cases assert
   `WholeFileReader`'s return-everything contract — `reader_returns_all`,
   `reader_concatenation_order`, `reader_retrieval`, `scattered_multifact`
   (multi-fact assembly needs *all* facts). A top-k retrieving reader would break
   them. Reader choice must therefore be **per-case**, defaulting to whole-file.
2. **Retrieving requires a `SearchableGraph`.** `InMemoryGraph` has no `search()`;
   only `VectorGraph`/`PostgresVectorGraph` do. So `reader: retrieving` implies a
   vector substrate — a validation constraint, not a free combination.
3. **Real embeddings are keyed + costly, but deterministic.** A real embedding
   endpoint returns the *same* vector for the same `(model, text)`. The suite's
   discipline is "offline by default" (FakeEmbedder, no network in CI), and we keep
   it: we **record real vectors once locally and commit them** as a fixture, so CI
   replays them — offline, deterministic, *and* semantically real (§4.3). FakeEmbedder
   would XPASS-or-FAIL by hash luck; a committed real-vector cache makes CI exercise
   ranking for real. The only thing that must be keyed/online is *refreshing* the
   cache when a seeded text or the embedding model changes.
4. **Capability gating today keys off the *runner*.** `partition_by_capability`
   compares `case_needs` against `runner.provides`. But the embedder/reader is a
   **harness wiring** choice, independent of which backend runs the agent — and
   component cases ignore the runner entirely. So the existing gating axis doesn't
   express "a real embedder is available."

## 3. Goals / non-goals

**Goals**
- A `RetrievingReader` that turns context into a `search()` call and returns only
  the top-k relevant facts' text.
- Wire it per-case (opt-in) without disturbing the cases that depend on
  `WholeFileReader`.
- Inject a real embedder (`OpenRouterEmbedder`) into `VectorGraph`, with a per-case
  `embedder` axis (`fake` / `cached` / `live`): `cached` records vectors into a
  **committed fixture** so CI replays them offline and deterministically; `live` runs
  online and SKIPs in CI.
- Assert the thesis as **before/after pairs**: a CI-verified reader pair (deterministic,
  `cached`) that *proves* retrieval is the fix, and an online end-to-end demo pair.
- Promote the CI reader after (remove `xfail`); flip the paraphrase-dedup xfails for
  free via `embedder: cached`.

**Non-goals**
- No change to `WholeFileReader` or the cases that rely on it.
- No new persistence backend; `VectorGraph`'s in-process store is fine for the
  per-case eval lifecycle.
- No reranking model / hybrid BM25 / query rewriting — straight cosine top-k is the
  MVP; fancier retrieval is a later pass behind the same reader seam.
- No attempt to make semantic retrieval deterministic; it's gated as a
  network-capable tier, like the LLM judge.

## 4. Design

### 4.1 New reader variant

```python
# knowledge/graph_reader/grapher_reader_variants/retrieving_reader.py
class RetrievingReader(GraphReader):
    """Relevance-ranked retrieval: search the graph, return only the top-k hits.

    Requires a SearchableGraph (VectorGraph / PostgresVectorGraph). Unlike
    WholeFileReader it honors `context` — it's the query that ranks the facts.
    """

    def __init__(self, graph: SearchableGraph, *, top_k: int = 8,
                 min_score: float = 0.0) -> None:
        super().__init__(graph)
        self.top_k = top_k
        self.min_score = min_score   # drop hits below this cosine score

    def synthesis(self, context: str | None = None) -> list[ReadRequest]:
        return [ReadRequest(query=context or "", top_k=self.top_k)]

    # read() filters search hits to score >= min_score, then takes top_k.
```

**Relevance cutoff, not just top-k.** `VectorGraph.search()` sorts and slices to
`top_k` — it returns *K facts regardless of score*. For the `excludes_*` assertions
that's fragile: whether X-Ray/SES/CloudFront drop out depends only on them ranking
below position K. So `RetrievingReader` applies a **`min_score` threshold first** (a
real ranking reader returns the *relevant* facts, not always K of them), then top-k as
an upper bound. The threshold is what makes "drop the irrelevant" robust; top-k just
caps volume. `SearchHit.score` already carries the cosine value, so this is a filter,
not a new search.

`GraphReader.read` is concrete and currently calls `self.graph.read(req.query)`.
`VectorGraph.read` ignores the query, so the base `read` can't rank. Two clean
options:

- **(a)** Override `read` in `RetrievingReader` to call `self.graph.search(req.query,
  top_k=req.top_k, filters=req.filters, scope=req.scope)` and join the hits' `.text`.
- **(b)** Teach the base `read` to prefer `search` when the graph is a
  `SearchableGraph` and the request is bounded.

**Recommendation: (a)** — keep the base `read` untouched (it's "concrete and final
for the MVP" and `WholeFileReader` depends on it), and let the retrieving variant
own its retrieval. `ReadRequest` already carries `top_k`/`filters`/`scope`, so the
request model needs no change.

### 4.2 Per-case reader selection in `build_trio`

```python
# knowledge/evals/eval_def.py
class EvalCase(BaseModel):
    ...
    reader: Literal["whole_file", "retrieving"] = "whole_file"
    embedder: Literal["fake", "cached", "live"] = "fake"
```

Two orthogonal per-case axes. `reader` picks retrieval (dump vs rank); `embedder`
picks where vectors come from. `reader: retrieving` requires a real embedder
(`cached` or `live`) — validated, since FakeEmbedder can't rank meaningfully. The
`embedder` axis stands alone too: a `whole_file` dedup case can ask for `cached`
real vectors to exercise *paraphrase* merge. Defaults (`whole_file` + `fake`) leave
every existing case byte-for-byte unchanged.

```python
# knowledge/wiring.py
def build_trio(substrate="in_memory", graph=None, llm=None,
               reader: str = "whole_file", embedder=None):
    graph = graph or _graph_for(substrate, embedder=embedder)
    ingestor = PromptIngestor(graph, llm=llm)
    if reader == "retrieving":
        if not isinstance(graph, SearchableGraph):
            raise ValueError("reader='retrieving' requires a searchable substrate "
                             "(vector/postgres), got " + type(graph).__name__)
        reader_obj = RetrievingReader(graph)
    else:
        reader_obj = WholeFileReader(graph)
    return graph, ingestor, reader_obj
```

`_seed_knowledge` and `run_component` pass `reader=case.reader` (and the resolved
embedder, §4.3) through to `build_trio`. Default `whole_file` ⇒ every existing case
is byte-for-byte unchanged.

### 4.3 Embedder wiring: a committed real-vector cache

`_graph_for("vector")` constructs `VectorGraph()` with the default `FakeEmbedder`.
We make the embedder injectable and wrap a **`CachedEmbedder`** around it so real
vectors are recorded once and replayed everywhere — the record/replay ("cassette")
pattern, sound here because embeddings are deterministic for a fixed `(model, text)`.

```python
# knowledge/llm/embedder_variants/cached_embedder.py
class CachedEmbedder(Embedder):
    """Replay real vectors from a committed cache; record misses when allowed.

    key = sha256(f"{model_id}\n{text}").  Cache is JSON on disk, keyed so a model
    swap is a clean miss (not silent staleness).
    """
    def __init__(self, inner: Embedder | None, cache_path: Path, model_id: str,
                 allow_compute: bool) -> None: ...

    def embed(self, texts: list[str]) -> list[Vector]:
        hits, misses = self._partition(texts)
        if misses:
            if not (self.allow_compute and self.inner):
                raise RuntimeError(
                    f"embedding cache miss for {len(misses)} text(s) under model "
                    f"{self.model_id!r} (e.g. {misses[0][:60]!r}...). A seeded fact "
                    "or the model changed — refresh locally with OPENROUTER_EMBED_MODEL "
                    "set (`python -m knowledge.evals.embed_cache --refresh`) and commit "
                    f"{self.cache_path.name}."
                )
            for text, vec in zip(misses, self.inner.embed(misses)):
                self._store(text, vec)            # marks dirty
        return [self._cached(t) for t in texts]   # save() flushes if dirty
```

```python
# knowledge/wiring.py
def _graph_for(substrate, embedder=None):
    if substrate == "vector":
        return VectorGraph(embedder=embedder)   # None => VectorGraph's FakeEmbedder default
    ...

# knowledge/evals/run.py — resolve per case, from case.embedder
def _eval_embedder(case):
    if case.embedder == "fake":
        return None                              # VectorGraph's FakeEmbedder default
    model = os.getenv("OPENROUTER_EMBED_MODEL", openrouter_http.DEFAULT_EMBED_MODEL)
    has_key = bool(os.getenv("OPENROUTER_API_KEY"))
    live = OpenRouterEmbedder(model=model) if has_key else None
    if case.embedder == "live":
        return live                              # online only; None => case SKIPs (§4.4)
    # cached: replay the committed fixture; record misses only when a key is present.
    cache = EMBED_CACHE_DIR / f"{_slug(model)}.json"
    return CachedEmbedder(live, cache, model_id=model, allow_compute=has_key)
```

The model is the codebase's existing default — `openrouter_http.DEFAULT_EMBED_MODEL`
(`openai/text-embedding-3-small`), already used by `OpenRouterEmbedder` and overridable
via `OPENROUTER_EMBED_MODEL`. We reuse it as-is (no new constant, no `dimensions`
param — the existing `embed()` doesn't truncate, and we don't change that).

So `embedder` resolves to three behaviors:

| `embedder` | Source | CI (no key) | Local (+ key) |
|------------|--------|-------------|---------------|
| `fake` (default) | FakeEmbedder | offline, deterministic | offline, deterministic |
| `cached` | committed fixture | replay real vectors; **miss → loud error** | replay; record + commit new vectors |
| `live` | OpenRouter, uncached | **SKIP** (no key) | online, real vectors every run |

The cache lives at `knowledge/evals/fixtures/embeddings/<model-slug>.json`, vectors
base64-packed float32 with sorted keys. Only `cached` cases write it and those are all
small (§5) — a few dozen full-dim vectors, tens of KB — so the fixture stays tiny and
churn-free with no dimension reduction needed; the big 400-fact end-to-end case is
`live` (uncached). An `embed_cache --refresh` entry point re-embeds every seeded text +
seed-prompt across the `cached` cases.

> **External-API discipline:** smoke-test OpenRouter's embeddings surface against
> `OPENROUTER_EMBED_MODEL` (one `embed(["x"])` round-trip) before wiring, per the
> project rule on not assuming external-API shape. `openrouter_http.embed` already
> exists and is the path the live/refresh modes use.

### 4.4 Capability gating: `real_embeddings` (cache or key) vs `live_embeddings` (key)

The `embedder` axis gives two requirements, gated harness-side (not a runner
property — component cases ignore the runner — so union them into the provided set):

```python
def harness_capabilities() -> set[str]:
    caps = set()
    model = os.getenv("OPENROUTER_EMBED_MODEL", _DEFAULT_EMBED_MODEL)
    if (EMBED_CACHE_DIR / f"{_slug(model)}.json").exists():
        caps.add("real_embeddings")             # committed fixture satisfies `cached`
    if os.getenv("OPENROUTER_API_KEY"):
        caps |= {"real_embeddings", "live_embeddings"}   # a key satisfies both
    return caps

# unmet_needs: provided = runner.provides | harness_capabilities()
```

`case_needs` auto-derives from the `embedder` axis:

```python
if case.embedder == "cached":
    needs.add("real_embeddings")    # committed cache OR a live key
elif case.embedder == "live":
    needs.add("live_embeddings")    # a key only — never cached
```

This is exactly the CI-vs-online split:

| | CI (cache committed, no key) | Local (+ key) |
|---|---|---|
| `cached` cases (reader pair, dedup) | **run** — replay; miss → loud error | run; record misses |
| `live` cases (end-to-end demo after) | **SKIP** (`needs live_embeddings`) | run online |

A **`cached` miss inside a run is a hard error, not a SKIP** — a seeded text or the
model changed without a refresh, and that must fail loudly so a stale fixture can't
pass silently. Distinguishing it from a `live` case's clean SKIP is precisely why the
two are separate capabilities. (FakeEmbedder cases need neither; they run everywhere.)

## 5. Worked examples — two before/after pairs

The lost-in-the-middle thesis is "a ranking reader rescues knowledge a dumb reader
buries." We assert it as **before/after pairs** — the before (dump everything) is the
control that proves the after (rank) is what fixes it. Two pairs at two layers:

### 5.1 Reader pair — deterministic, in CI (the regression guard)

Same ~18-fact graph, same `excludes_*` checks, the **reader is the only variable**:

| case | reader | embedder | verdict | runs in |
|------|--------|----------|---------|---------|
| `lost_in_middle_reader_before` | `whole_file` | `fake` | **XFAIL** — whole graph returned, X-Ray/SES/CloudFront not dropped | CI (offline) |
| `lost_in_middle_reader` (after) | `retrieving` | `cached` | **XPASS** — threshold keeps `TODO(MD)` + caching, drops the rest | CI (cached vectors) |

Both arms run in CI every time, so the before→after delta is *verified*, not assumed.
The before is `fake`/in-memory (no vectors needed); the after replays ~19 committed
vectors. Model-free, deterministic, tiny fixture. This pair is the promotable contract.

### 5.2 End-to-end pair — agent over the 400-fact haystack, online demo

Same buried needle, but an agent runs against the output of each reader. Following the
`_before` convention (base name = after), the current `lost_in_middle` is **renamed to
`lost_in_middle_before`** and the base name becomes the after:

| case | reader | embedder | verdict | runs in |
|------|--------|----------|---------|---------|
| `lost_in_middle_before` (renamed from today's `lost_in_middle`) | `whole_file` | `fake` | **XFAIL** — needle lost in ~400 unranked policies | CI (offline) |
| `lost_in_middle` (after) | `retrieving` | `live` | **XPASS** — agent gets only relevant facts, applies `TODO(MD)` | **local only** (SKIP in CI) |

The after is `live`/uncached, so it SKIPs in CI and runs online with a key — the
dramatic "ranking rescues the needle end-to-end" demo, without committing 400 vectors.
The before keeps the deep haystack (what makes the result non-trivial) and stays the
CI-visible XFAIL.

> **Bonus from the `embedder` axis (no reader change):** the paraphrase-dedup xfails
> `ingestion_merge_near_dupes` and `skills_merge_dedup` set `embedder: cached` and flip
> XFAIL→XPASS — their xfail reason is literally "needs a real embedder." Tiny graphs,
> tiny added vectors. Folded into the `cached` fixture for free.

Per the XPASS convention, the **reader pair** promotes (drop `xfail` on the after) once
green on the committed cache; the end-to-end after stays a SKIP-in-CI demo.

## 6. Calibration: `min_score` (primary) + `top_k` (cap)

Two knobs make `excludes_*` meaningful, and the **threshold is the load-bearing one**:

- **`min_score`** drops facts below a cosine relevance — this is what guarantees the
  irrelevant trio (X-Ray/SES/CloudFront) falls out, *independent* of how many facts
  the graph holds. Without it, top-k alone returns K facts whether or not they're
  relevant, so exclusion would hinge on rank position — fragile.
- **`top_k`** is just an upper bound on volume once the threshold has filtered.

For `lost_in_middle_reader` (18 facts, ~2 relevant) the threshold should sit above the
distractors' similarity to "add a caching TODO" and below the two relevant facts'
similarity — an empirical value on the actual embedding model, like the haystack depth
was calibrated. `top_k=8` is a safe default cap. Both are reader params with per-case
overrides; document the calibrated `min_score` in the case comment. (A real embedding
model's cosine scores are model-specific, so the threshold is pinned alongside the
model id — another reason Q1's "don't change the model" matters.)

## 7. Implementation plan

1. `retrieving_reader.py`: `RetrievingReader` (search → top-k text), option (a).
2. `eval_def.py`: add `EvalCase.reader` and `EvalCase.embedder` (both `Literal`,
   defaults `"whole_file"` / `"fake"`); validate `retrieving ⇒ embedder != fake`.
3. `wiring.py`: `build_trio(reader=, embedder=)` + `_graph_for(embedder=)`; the
   searchable-substrate guard.
4. `cached_embedder.py`: `CachedEmbedder` (base64-float32 store, model-keyed,
   loud miss) + an `embed_cache --refresh` entry point.
5. Smoke-test OpenRouter embeddings against the chosen model + `dimensions` (§4.3).
6. `run.py`: per-case `_eval_embedder(case)` + `harness_capabilities()`; thread
   `reader=`/embedder through `_seed_knowledge` / `run_component`; auto-derive
   `real_embeddings` / `live_embeddings`; union harness caps into `unmet_needs`.
7. `.env.example`: `OPENROUTER_EMBED_MODEL` (commented; "unset ⇒ replay committed
   cache; set + key ⇒ refresh `cached` cases / run `live` cases online").
8. Cases — the two pairs (§5):
   - add `lost_in_middle_reader_before` (whole_file control); set the existing
     `lost_in_middle_reader` to `reader: retrieving, embedder: cached`;
   - **rename** `lost_in_middle` → `lost_in_middle_before` (whole_file control); add a
     new `lost_in_middle` (`reader: retrieving, embedder: live`) as the after;
   - flip `ingestion_merge_near_dupes` + `skills_merge_dedup` to `embedder: cached`.
9. Generate + **commit** the `cached` fixture (reader after + 2 dedup cases — all
   small); calibrate `min_score` + `top_k`; confirm the reader pair runs in CI (before
   XFAIL, after XPASS) and the `live` after SKIPs offline; verify a deliberately edited
   `cached` fact triggers the loud miss; then promote the reader after (drop `xfail`).
10. Tests (offline — see §8).

## 8. Testing strategy

Offline, no network — the seam is injectable end to end:

- **`RetrievingReader` ranks + thresholds**: construct a `VectorGraph` with a tiny
  **stub embedder** that maps a fixed vocabulary to orthogonal vectors (so
  "caching"/"todo" rank above "xray"); assert `read(query)` keeps the relevant facts
  and excludes the below-`min_score` ones (and that `top_k` caps volume). Deterministic,
  no real model.
- **Searchable-substrate guard**: `build_trio(substrate="in_memory",
  reader="retrieving")` raises `ValueError`.
- **Default unchanged**: `build_trio()` still returns `WholeFileReader`; the
  existing reader cases are untouched.
- **CachedEmbedder replay**: a temp cache pre-seeded with known vectors; `embed`
  returns them with **no** inner embedder and **no** network. Round-trip through
  base64-float32 is exact.
- **CachedEmbedder miss is loud**: read-only cache + unseen text + `allow_compute=False`
  → `RuntimeError` naming the missing text and the refresh command (asserts CI fails
  closed on a stale fixture).
- **CachedEmbedder records**: `allow_compute=True` with an injected fake inner
  embedder fills misses and marks dirty; `save()` writes sorted, stable JSON.
- **Capability partition**: an `embedder: cached` case is **runnable** when the cache
  fixture exists (replay), and a `embedder: live` case is **skipped** with reason
  `live_embeddings` when no key — both via monkeypatched `harness_capabilities`. With
  a key, both run.
- **Validation**: a case with `reader: retrieving, embedder: fake` is rejected at load.
- **`OpenRouterEmbedder.embed`**: already injectable via `post`; unit-test the HTTP
  body shape against the embeddings endpoint (mock `post`).

## 9. Risks & alternatives

- **Global reader swap (rejected).** Flipping `build_trio` to `RetrievingReader`
  whenever a real embedder is present would silently break the
  return-everything reader cases (§2.1). Per-case opt-in is the safe seam.
- **FakeEmbedder ranking is meaningless (the trap).** If we shipped the reader but
  left the default embedder, `lost_in_middle_reader` would pass/fail by hash luck.
  The committed real-vector cache (§4.3) is precisely what lets CI run *real* ranking
  offline; FakeEmbedder is only the bootstrap fallback, which SKIPs.
- **Cache size — a non-issue by design.** Only `cached` cases write the fixture, and
  by §5 those are all small: the reader pair (~19 vectors) plus the two dedup cases
  (a handful each) — tens of KB total at full dimension. The one multi-MB driver, the
  400-fact `lost_in_middle`, is `live`/uncached, so it never touches the committed file.
  Store vectors base64-packed float32 with sorted keys to keep diffs clean; the only
  real cost is git **history churn** on regeneration, which the small size makes cheap.
- **Cache staleness.** A committed vector is only valid for the exact `(model, text)`.
  Model id is in the key (clean miss on swap), and any seeded-text edit misses →
  the loud error forces a refresh-and-commit. The risk is a contributor without a key
  who edits a seeded fact: they get a clear error telling them what to run, and the
  PR can't land a green-but-stale fixture.
- **Embedding cost / latency.** `cached` cases re-embed only in refresh mode (small,
  batched); CI pays nothing (pure replay). The `live` end-to-end demo re-embeds its
  ~400 facts every online run — fine, it's a manual demo, not a CI path.
- **ConflictFlagger network call at seed time.** `default_write_policy` includes a
  `ConflictFlagger(llm=OpenRouterLlm())`; switching these cases to `substrate: vector`
  routes the ~400 seed writes through it. It "skips silently if the LLM is
  unavailable", so CI (no key) is fine, but to keep refresh runs cheap and the test
  about *retrieval* (not conflict detection), seed these cases with a minimal policy
  (`[Redactor(), Deduper()]`) — pass it via `_graph_for`/`VectorGraph(policy=…)`.
- **Alternative — teach base `read` to use `search` (option (b)).** Cleaner in the
  abstract but mutates a contract `WholeFileReader` depends on; more blast radius
  for no extra capability. Rejected in favor of the variant owning its retrieval.
- **`PostgresVectorGraph`.** Also a `SearchableGraph`, so `reader: retrieving` works
  there too once tenancy is supplied — out of scope here but free.

## 10. Decisions (resolved)

1. **Embedding model — keep the existing one.** Use the codebase's current default,
   `openrouter_http.DEFAULT_EMBED_MODEL` (`openai/text-embedding-3-small`), via
   `OPENROUTER_EMBED_MODEL`. No new model, and no `dimensions` change — the existing
   `embed()` is used as-is. The model id is the cache key, so it's pinned once the
   fixture lands.
2. **Relevance cutoff, not just top-k.** `RetrievingReader` filters to `min_score`
   first, then caps at `top_k` (§4.1/§6). The threshold is what makes the `excludes_*`
   assertions robust; without it, top-k alone returns K facts regardless of relevance.
3. **Case naming — keep the `_before` convention (base = after).** Both pairs: rename
   today's `lost_in_middle` → `lost_in_middle_before` (control) and make the new
   retrieving/`live` after the base `lost_in_middle`; `lost_in_middle_reader` stays the
   after with a new `lost_in_middle_reader_before` control. No base-named case carries
   before-semantics.
4. **Promote the reader-after only.** The CI reader pair promotes (drop `xfail` once
   green on the committed fixture); the end-to-end `live` after stays a SKIP-in-CI demo
   and is not promoted (it can't be green in CI).
5. **Cache storage.** base64-float32, sorted keys, one file per model under
   `knowledge/evals/fixtures/embeddings/<model-slug>.json`. Only small `cached` cases
   write it, so no special encoding needed.
6. **No `--reader` CLI override for now.** The committed `lost_in_middle` (after) gives
   the end-to-end demo a home; an ad-hoc override is unnecessary.

**Pending the implementation run (empirical, not design):** confirm OpenRouter serves
the embedding model (one-call smoke test, §4.3), then calibrate `min_score` + `top_k`
on `lost_in_middle_reader` against that model's actual cosine scores.
