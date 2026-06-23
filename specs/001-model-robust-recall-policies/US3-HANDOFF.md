# US3 Implementation Handoff

Working note for resuming **User Story 3** (unify write path + structured conflict + Tier B). The design lives in `spec.md` / `plan.md` / `tasks.md` (T028–T040) / `research.md` (R7–R9) / `data-model.md` / `contracts/`. This file captures **current state, what's already built to reuse, the core refactor's design, and session-learned gotchas** — the things not in those docs.

## Status (branch `001-model-robust-recall-policies`)

- **US1 (reader cutoff) ✅** committed: `dd8afaf`, `dfff28e`, `dd28538`. Reader = floor(0.30) → relative(0.60) → cap(8). Reader cluster reconciled & green offline.
- **US2 (semantic dedup) ✅** committed: `c7cebc9`, `9453650`, `9227a96` (structured merge judge). `ingestion_merge_near_dupes` + `skills_merge_dedup` flip XFAIL→PASS via real merge, replayed from a committed cassette.
- **main merged in** (`7fbff17`) + gating-integration fix (`5925355`) + spec note (`65e0e5f`).
- **Verification baseline:** unit suite **301 passed, 1 failed**. The 1 failure (`knowledge/serve/tests/test_server.py::test_insight_then_context_round_trips`, `set OPENROUTER_API_KEY`) is **pre-existing & unrelated** — confirmed by stashing. Treat it as the known-good baseline.
- **US3 Tier A ✅ done & committed** (`75c4d57`, `43794f0`, `9abf477`): embed-once / one shared recall pass (T028–T032), structured `ConflictJudge` + cassette (T030, T033), conflict eval wiring + offline `conflict_should_flag` component case (T034–T035). Offline-green: 313 unit pass + the 1 pre-existing serve failure; merge + conflict eval cases replay from committed cassettes.
- **US3 Tier B = not started (paused by owner).** Next session resumes at T036. See "Tier B" below; the gate (T039→T040) is the owner's keep/kill call (FR-022) — kill is the documented zero-cost default.
- **Tier-A landed shape (for Tier B to build on):** `WriteStep.apply(decision)` (no `store`); steps read `decision.candidates`; `consumes_candidates` flag triggers the one `_recall` pass (embed once, `recall_floor` default 0.45, all-states). `ConflictFlagger(judge=ConflictJudge(...))`. `EvalCase.conflict_model` axis + `_conflict_judge_for` + `conflict_verdicts` capability; `knowledge_graph` producer surfaces `graph.contradictions()`. `Fact.tags` still NOT added (T036).

## CRITICAL post-merge facts (main's active-fact gating, PR #19)

- `SearchableGraph.search(query, *, top_k, filters, scope, state="active")` — retrieval is **active-only by default**. `read()` is also active-only.
- `VectorGraph.most_similar(text, k)` → `search(text, top_k=k, state=None)` — **sees ALL states**. This is exactly what the write-policy dedup/conflict recall needs; **keep using `most_similar` so US3's conflict recall sees `proposed` facts.**
- Eval component producers already adapted (`knowledge/evals/run.py`): `_produce_graph_reader` seeds `direct_to_graph` as `active`; `_produce_knowledge_graph`/`_produce_ingestion` inspect all states via `_all_facts_text(graph)` (= `graph.facts`).

## The core US3 refactor — embed once / one shared recall pass (FR-015, FR-016, FR-017; T031–T032)

**Today (the smell):** each `WriteStep.apply(decision, store)` calls `store.most_similar(decision.text)` itself, and `VectorGraph._add` re-embeds `decision.text`. So one `write()` embeds the same text **2–3×** (Deduper search + ConflictFlagger search + store).

**Target:** `VectorGraph.write` embeds the incoming text **once**, does **one** `most_similar(state=None)` pass against one **shared recall floor**, and feeds that candidate set + the vector to both judges and to persistence.

**Recommended design** (matches `contracts/write-policy-recall.md`):
1. `WriteDecision` (in `knowledge/knowledge_graph/write_policy/write_policy_def.py`) gains `embedding: list[float] | None = None` (T032). Optionally also carry the shared candidate list (e.g. `candidates: list[SearchHit]`).
2. `VectorGraph.write`: compute `decision.embedding` once; compute the shared candidate set once (`most_similar(text, k)` filtered by the shared `recall_floor`); thread both to the steps; `_add` **reuses `decision.embedding`** (no re-embed).
3. `Deduper` and `ConflictFlagger` consume the **shared candidates** from the decision instead of each calling `store.most_similar`. (This changes the `WriteStep`/`StoreView` interaction — update `Deduper`, `ConflictFlagger`, `ConflictOverwriter`, the `_StoreView` test stub in `knowledge/tests/test_write_policy.py`, and `test_postgres_vector_graph.py`.)
4. **Ordering:** merge (Deduper) before conflict; if `decision.action == "update"` (merged dup), **skip** the conflict check (ConflictFlagger already early-returns on `action=="update"` — preserve that).
5. **Shared recall floor (FR-016):** replace Deduper's `recall_floor=0.45` and ConflictFlagger's `similarity_floor=0.6` with one value. ~**0.45** (high-recall) catches both paraphrase dups (~0.65) and negation contradictions (~0.79–0.89). The implicit-contradiction case (~0.454) is below any usable floor by design — that's Tier B's job, not the floor's.

**SC-007 metric:** exactly one embedding of the incoming text per write (recall + merge + conflict + persist share it). Empty-graph first write embeds at most once and issues no candidate search it knows is empty.

## What's already built to REUSE (don't rebuild)

- **`VerdictCassette`** (`knowledge/llm/verdict_cassette.py`): keyed-replay (`sha256(model+payload)→verdict`), replay/record/loud-miss, merge-on-save lock. **Use it for the conflict cassette too** (`kind="conflict"`). Fixtures live at `knowledge/evals/fixtures/verdicts/<kind>/<model-slug>.json`.
- **`MergeJudge`** (`write_step_variants/merge_judge.py`): the **pattern to mirror** for the conflict judge — structured output + cassette + skip-when-no-source. Copy its shape.
- **Structured-output seam (already wired):** `Llm.complete(..., response_format=None)` → `openrouter_http.chat_complete` passes `response_format` through; `OpenRouterLlm` + `FakeLlm` accept it. Use a `json_schema` object root (a bare boolean is invalid). Conflict schema: `{"contradicts": boolean, "target_id": string|null}` (FR-018) — but `target_id` is a runtime fact id, so **cassette-store only the method-agnostic part** (e.g. `{"contradicts": bool}`) and resolve `target_id` to the candidate at call time, exactly like `MergeJudge` stores `{same_lesson}` and resolves `keep_id` at runtime.
- **`run.py` wiring to mirror:** `_merge_judge_for(case)` + `merge_model` EvalCase axis + `merge_verdicts` capability (`harness_capabilities`/`case_needs`) + `VERDICT_CACHE_DIR`. For conflict, add a `conflict_model` axis + `_conflict_judge_for` + a `conflict_verdicts` capability the same way; inject into `ConflictFlagger` in `_build_trio_for`.
- **`verdict_cache.py`** regenerator: extend to also drive conflict cases (it already re-runs `merge_model` cases; add `conflict_model`).
- **Calibrated constants (text-embedding-3-small):** reader floor 0.30 / ratio 0.60 / top_k 8; dedup recall 0.45. Grounding scores in `research.md` R4 + the reader docstring.

## Conflict cases (T035)

Existing negation-contradiction cases: `contradiction_should_flag`, `scoped_conflict` (and `ConflictOverwriter` is exercised by `test_postgres_vector_graph.py`). Harden them to assert structured output + replay from a committed **conflict** cassette (offline-deterministic). They currently rely on `ConflictFlagger(llm=...)` / `FakeLlm`.

## Tier B — gated experiment (T036–T040)

- `Fact` (`knowledge_graph_def.py`) needs a **`tags: list[str]`** field (not present yet) — T036.
- `AspectTagger` (new write step): write-time controlled-vocab tags; **conflict candidates = cosine-kNN ∪ same-tag** (bounded). Conflict path only.
- Implicit-contradiction eval set: the 0.454 pair ("prioritize raw performance" / "readability over micro-optimizations") + ~5–10 disjoint-vocab/no-negation siblings; marked **provisional**.
- **Gate = OWNER decision (FR-022, per the 2026-06-22 clarification):** no pinned threshold. Report tag-co-assignment recall + end-to-end flag rate and **surface to the user for an explicit keep/kill call** (T039). Kill → leave implicit cases as documented XFAIL, drop the tag key (FR-023). Tier C (batch backstop) = documented only, NOT built.

## Eval/cassette workflow & validation commands (session-proven)

- **Record (live, needs `.env` `OPENROUTER_API_KEY`)** — keyed `--fake` runs record vectors/verdicts incrementally (cheaper than full `--refresh`):
  `PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.run --fake <case_ids>`
  (Then commit `knowledge/evals/fixtures/embeddings/*` and `.../verdicts/*`.)
- **Validate offline (replay-only; loud-miss if a fixture is missing):**
  `OPENROUTER_API_KEY= PHOENIX_COLLECTOR_ENDPOINT= uv run python -m knowledge.evals.run --fake <case_ids>`
- **`PHOENIX_COLLECTOR_ENDPOINT=`** disables tracing — the Phoenix collector stalls/timeouts otherwise (observed). Tracing is on only when that env var is set.
- **Inspect scores** for calibration: build the trio via `_build_trio_for`, seed, `graph.search(query, top_k=30)`, print `h.score` — see the reader-calibration probe pattern (how rel_ratio 0.60 was chosen).
- Unit tests: `uv run pytest knowledge/ -q` (expect 301 pass + the 1 pre-existing serve failure).
- TDD: write the red tests first (FR-027). US3 test files: `knowledge/knowledge_graph/tests/test_vector_graph.py` (embed-once, shared recall — note main also edited this file), `write_policy/tests/test_conflict_flagger.py`.

## Out of scope / deferred (do NOT pull into US3)

- **Deterministic-ingestion cassette** + **`ingest_state: active`** axis — the two prerequisites for FR-030/SC-013 application-suite validation (Known gaps in `spec.md`). Gating author / follow-up.
- Wiring `RetrievingReader` into the production serve path.
- Tier C batch compaction.
- Broader main-suite `via_ingestor`-gratuitous cleanup (full-pipeline agent cases whose seeded knowledge is now gated out) — gating author's call.

## Gotchas

- `.py` "not covered by lint job" hook warnings are **false positives** (not the first `.py`; repo is on GitHub migration, not GitLab CI). Ignore.
- LF→CRLF git warnings on Windows are benign.
- `git add -A` is safe here (tree is kept clean between commits); the working tree had no stray changes.
- The retry + `--workers` infra is on `main` (merged via PR #18); embedder/HTTP calls now retry transient failures.
