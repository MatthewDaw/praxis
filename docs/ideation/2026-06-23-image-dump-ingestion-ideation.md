---
date: 2026-06-23
topic: image-dump-ingestion
focus: see how smarter people deal with this problem (ingesting visual asset dumps into a knowledge graph, fast)
mode: repo-grounded
---

# Ideation: Fast, smart ingestion of image/asset dumps into the knowledge graph

## Grounding Context (Codebase)

- **Current state:** Praxis ingestion does NOT process images. Images are hand-authored "asset cards" — text strings like `"asset: mascot | caption: ... | path=assets/mascot.png"` seeded directly to the graph in `knowledge/evals/cases/matt/volta_video/_generate.py` (`ASSET_CARDS`). The real task is ADDING image-dump ingestion; the naive approach (one vision/LLM call per file) is what would be "too slow."
- **Pipeline:** `knowledge/injestion/parent_injestor.py` (`Ingestor.ingest`, synchronous loop), `knowledge/injestion/injestor_variants/prompt_injestor.py` (`PromptIngestor.synthesis` = single LLM call splitting text into insights), `knowledge/wiring.py` (`build_trio`). Pipeline is `str`-in/`str`-out. Embeddings computed at graph-write/dedup. No batching, concurrency, content-hash dedup, or caching. Embedder is injectable via `build_trio`. `embed_cache.py` already records per-model keyed caches.
- **Constraints/memory:** Always go through `build_trio` → `ingestor.ingest`/`reader.read`, never direct graph calls. KG store is RDS Postgres + pgvector, multi-tenant by (org_id, user_id). Roadmap includes write-time persisted `cluster_id`/`label` clustering. The AlchemistAssets dump to ingest contains `Common/` PNGs and `Photoshop Files/` PSDs.

## Topic Axes

- Intake — reading folder dumps, PSD/PNG parsing, thumbnails, hashing
- Dedup & caching — exact + perceptual hash, idempotent re-runs
- Visual understanding strategy — cheap multimodal embeddings vs LLM captioning; KG modeling of images
- Concurrency & throughput — batching, async workers
- Deferred/lazy enrichment — fast pass now, expensive captioning later/on-demand

## Ranked Ideas

Two layers emerged: a **graph-modeling layer** (how an image becomes graph structure — the "ingest to a knowledge graph" answer) and a **throughput layer** (how to do it fast).

### 1. Asset-node + derived-insight-node split (A-MMKG vs N-MMKG decision) — ANCHOR
**Description:** Adopt node-based (N-MMKG / MAHA) modeling: the raw asset is its own node (provenance: path, sha256, embedding property); extracted facts/captions/entities become separate `Insight` nodes linked via `HAS-IMAGE`/`derivedFrom` provenance edges. Maps onto Praxis's existing insight model — asset node = upload, derived tier = Insight.
**Axis:** Visual understanding
**Basis:** external: MMKG survey arXiv:2202.05786 (A-MMKG vs N-MMKG); MAHA arXiv:2510.14592 asset-vs-derived split.
**Rationale:** This decision dictates whether an image can carry its own edges and whether retrieval can return "the image" vs only a fact about it — load-bearing for #2, #9, #10.
**Downsides:** New node type + provenance edges in the graph model.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Explored

### 2. Visual entity linking to existing concept nodes
**Description:** Resolve entities derived from an image (Volta portrait) to the existing text concept node ("Volta") via cross-modal entity resolution — extend the trio's dedup/merge canonicalization cross-modal.
**Axis:** Visual understanding
**Basis:** external: Visual Entity Linking (MIT Press Data Intelligence), VNEL arXiv:2211.04872; direct: aligns with "reads/writes through trio, dedup/merge."
**Rationale:** Without it, image nodes are islands; with it, assets enrich the Volta knowledge already in the graph.
**Downsides:** Cross-modal resolution is the hardest piece.
**Confidence:** 75% · **Complexity:** High · **Status:** Unexplored

### 3. Cluster node + `variantOf` edges for visual dedup
**Description:** Near-duplicate is non-transitive: build a perceptual-hash similarity graph, run connected-components to assign `cluster_id`, create a canonical cluster node ("Work"), attach members via `variantOf` edges ("Manifestations" — FRBR). PSD + exported PNG = two manifestations of one work.
**Axis:** Dedup & caching
**Basis:** external: near-dup non-transitivity arXiv:1907.02821, Richpedia `ImageSimilarity` edges; direct: produces the write-time `cluster_id`/`label` on the roadmap.
**Rationale:** Cleanest bridge between image work and committed clustering direction — dedup output is the cluster seed.
**Downsides:** Threshold tuning for "same vs meaningful variant."
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored

### 4. Shared embedding space for cross-modal pgvector retrieval
**Description:** Use a CLIP/SigLIP-class model so image-node embeddings share the text-node vector space — a text query retrieves image nodes from one pgvector index. Fallback: dual per-modality indexes fused at query (MAHA/LlamaIndex).
**Axis:** Visual understanding / throughput
**Basis:** external: IKRL (images into entity embedding space), LlamaIndex+Neo4j dual-index, MAHA parallel retrieval.
**Rationale:** Determines whether cross-modal retrieval is one query or a fusion step, and whether a separate image index is needed in pgvector.
**Downsides:** Shared-space models may underperform specialized text embeddings on text-only retrieval.
**Confidence:** 75% · **Complexity:** Medium · **Status:** Unexplored

### 5. Idempotent content-addressed reconcile
**Description:** sha256 = asset identity. Walk dump into a manifest (path, size, hash), diff against graph, process only new/changed files (git/docker/rsync model). Re-dropping the same folder is a near-instant no-op.
**Axis:** Intake / Dedup & caching
**Basis:** external: Git/Docker CAS + content-hash embedding cache; direct: `ingest()` "runs every time," no hashing today.
**Rationale:** The felt pain is the Nth re-run during iteration; cheapest 10x and the foundation other ideas key off.
**Downsides:** Needs hash→artifact store + model-version cache invalidation.
**Confidence:** 90% · **Complexity:** Medium · **Status:** Unexplored

### 6. Embed-all cheap, caption lazily on retrieval (Deferred Visual Ingestion)
**Description:** Zero LLM captioning at ingest; cheap multimodal embedding per asset for immediate searchability; expensive LLM caption only on first retrieval, cached by hash forever.
**Axis:** Visual understanding / Deferred
**Basis:** external: arXiv Deferred Visual Ingestion; cheap multimodal embeddings vs per-image captioning.
**Rationale:** Captioning, not understanding, is the cost wall; pay caption cost only for assets that earn retrieval.
**Downsides:** Two-state assets (indexed vs enriched); retrieval must trigger/cache enrichment.
**Confidence:** 85% · **Complexity:** Medium-High · **Status:** Unexplored

### 7. PSD: layer names + embedded thumbnail, never rasterize
**Description:** Use psd-tools to pull the embedded preview thumbnail and layer-name tree for `Photoshop Files/`; designer layer names are free human-authored captions.
**Axis:** Intake / Visual understanding
**Basis:** external: psd-tools fast thumbnail + layer extraction; reasoned: designers already labeled everything.
**Rationale:** PSDs are slowest under the naive path yet carry richest free metadata; removes them from the critical path.
**Downsides:** Smart objects unsupported; layer-name quality varies.
**Confidence:** 80% · **Complexity:** Low-Medium · **Status:** Unexplored

### 8. Typed `ImageIngestor(Ingestor)` + manifest-as-input, batched async
**Description:** Add an `ImageIngestor` subclass overriding only `synthesis()`, taking a folder manifest (one folder = one pass), embedding in async batches of 64–128, inheriting the `ingest()` loop, state lifecycle, and injected `Embedder` seam.
**Axis:** Intake / Concurrency
**Basis:** direct: `parent_injestor.py` isolates the variant step as `synthesis`; `wiring.py` injects `Embedder`.
**Rationale:** Integration backbone — images flow through the same dedup/embed/clustering write path; serial loop becomes batched concurrency.
**Downsides:** Widening the intake contract touches the core ABC.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Unexplored

### 9. Auto-generate ASSET_CARDS from the folder
**Description:** Point the new intake at the dump and emit the eval seed mechanically (folder + filename + layer names + dims + embedding), so `_generate.py`'s hand-typed `ASSET_CARDS` become generated output.
**Axis:** Intake
**Basis:** direct: ASSET_CARDS are hand-authored with `path=` strings that drift from the real folder.
**Rationale:** Makes the eval reflect what the real pipeline produces; closes fidelity gap and maintenance burden.
**Downsides:** Generated cards thinner than hand-curated until enrichment lands.
**Confidence:** 85% · **Complexity:** Low-Medium · **Status:** Unexplored

### 10. Pre-baked committed graph/embedding snapshot for the eval
**Description:** Ingest the dump once offline, commit resulting nodes + embeddings as a fixture (extend `embed_cache.py` record pattern); eval loads in ms with zero vision API calls.
**Axis:** Caching / Deferred
**Basis:** direct: `embed_cache.py` records per-model keyed caches; external: content-addressed fixtures.
**Rationale:** Answers "run pipeline too slow" for the eval loop specifically; keeps CI deterministic/offline.
**Downsides:** Snapshot regenerated when dump or model changes (mitigated by #5 hash keying).
**Confidence:** 80% · **Complexity:** Low-Medium · **Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Contact-sheet / sprite-atlas batching (tile N thumbnails into one vision call) | Lazy-on-retrieval (#6) makes per-file call count a non-issue; premature |
| 2 | Bazel-style action graph keyed on input+tool-version | Stronger version folded into #5 hash-keyed cache; over-engineered for current scale |
| 3 | Graph-as-pointer-index, bytes in object storage | Real, but an infra decision adjacent to the speed question — its own brainstorm |
| 4 | Streaming folder-watcher / continuous reconcile | Degenerate case of #5; dump is one-shot today |
| 5 | Embeddings-only, never store a text card | Too aggressive; overlaps #6 which keeps lazy human-readable captions |
| - | axis: Concurrency & throughput | No standalone survivor — batched async folded into #8 (deliberate, not a gap) |
