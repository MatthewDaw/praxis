---
date: 2026-06-23
topic: image-asset-ingestion
status: requirements
upstream_ideation: docs/ideation/2026-06-23-image-dump-ingestion-ideation.md
scope: deep-feature
---

# Image / Asset-Dump Ingestion into the Knowledge Graph

## Problem & Context

Praxis cannot ingest images today. Visual assets enter the graph only as **hand-authored text "asset cards"** typed into `knowledge/evals/cases/matt/volta_video/_generate.py` (`ASSET_CARDS`) — e.g. `"asset: galvani_frog | caption: antique engraving of the frog-leg experiment | path=assets/galvani_frog.png"`. These rot silently against the real files and require manual labor.

We have a real asset dump to ingest (`AlchemistAssets`: `Common/` PNGs + `Photoshop Files/` PSDs) and want a refactor that ingests folders like this **without the naive slow path** (one VLM call per file, every run). The ingestion pipeline (`knowledge/injestion/parent_injestor.py` `Ingestor.ingest`, `injestor_variants/prompt_injestor.py` `PromptIngestor.synthesis`, `knowledge/wiring.py` `build_trio`) is `str`-in/`str`-out, synchronous, with no dedup or caching.

## Key Framing Decision (grounding)

The MMKG research describes explicit cross-modal entity-linking (`imageOf`/`sameAs` edges) — but that pattern belongs to **classical entity-graphs that retrieve by edge traversal**. Praxis retrieves text by **vector similarity over insight nodes** with no explicit entity-linking layer. Therefore:

- **No new graph mechanisms for images.** Images reuse what text already does.
- An image becomes an **asset node** (provenance) plus one or more **derived insight cards** (text). The cards go through the **existing text embedder + dedup/merge** path. "Linking" to existing concepts (a Volta portrait → Volta facts) happens **implicitly via vector similarity**, identical to how two text insights about Volta relate today.
- **Pixel embeddings (CLIP/SigLIP) are deferred** — they require separate infra and a separate vector space (the genuinely "weird" path), and nothing in v1 needs literal image-content search.
- The richer-signal lever is a **VLM "reverse prompt" caption**, which produces *text* and thus flows through the existing pipeline with no new modality.

## Goals / Success Criteria

- The `AlchemistAssets` dump (PNGs + PSDs) ingests into the graph through `build_trio` → an image-aware ingestor.
- The volta_video eval's asset cards are **auto-generated from the folder**, replacing hand-authored `ASSET_CARDS`, at quality ≥ the hand-authored versions for flat PNGs.
- Re-ingesting an unchanged dump does **near-zero work** (idempotent).
- The eval runs the **real ingestion code path** but is **deterministic and offline in CI** (no live VLM/API calls).
- Near-duplicate assets (PSD + its exported PNG, variants) collapse to a single canonical card and produce a `cluster_id`.

## Scope

### In scope

1. **Asset-node + derived-insight-card model (N-MMKG split).** Asset node carries provenance: source path, sha256, folder taxonomy, dimensions, format. Derived insight card(s) are text, linked to the asset node by provenance. Cards flow through the existing embedder + dedup/merge path.
2. **Card content generation.** Deterministic signals (filename, folder taxonomy e.g. `Common/` vs `Photoshop Files/`, PSD layer names, dimensions) **plus an eager VLM caption** for the canonical image of each cluster.
3. **VLM captions, content-hash cached.** Each unique image (by content hash) is captioned at most once, ever; re-runs and perceptual-hash variants reuse the cached caption. The throughput layer (dedup + cache + batching) is what makes eager captioning affordable.
4. **PSD handling.** Extract layer-name tree + embedded thumbnail via `psd-tools`; never rasterize/flatten full pixels.
5. **Perceptual-hash variant grouping.** Build a near-duplicate similarity graph, run connected-components (the near-dup relation is non-transitive), assign a `cluster_id`, emit one canonical card per cluster with variants attached. Feeds the write-time `cluster_id`/`label` clustering roadmap.
6. **Idempotent content-addressed reconcile.** Walk the dump into a manifest (path, size, sha256); diff against the graph; process only new/changed files. Re-dropping an unchanged folder is a near-instant no-op.
7. **`ImageIngestor(Ingestor)` seam.** A subclass overriding the `synthesis` step, taking a folder manifest (one folder = one pass), embedding/enriching in batches concurrently, inheriting the existing `ingest()` loop, state lifecycle, and injected `Embedder`. Routed through `build_trio`.
8. **Eval integration.** volta_video runs the real `ImageIngestor` live; VLM captions + embeddings are read from a **committed content-hash cache** (extending `knowledge/evals/embed_cache.py`). Auto-generated cards replace hand-authored `ASSET_CARDS`. Cache regenerated when assets change.
9. **Canonical-PNG normalization.** All supported image inputs are transformed to a single canonical type (PNG) at intake, then handled by one uniform PNG path downstream — no per-format branches. PSDs are special only in the *extraction* step (layer names + embedded thumbnail via `psd-tools`); their visual representation is rendered/extracted to PNG and joins the same path. Non-image files in the dump are skipped with a logged note (never error the whole run).
10. **Generated cards land active.** Image assets are always inserted as active/approved knowledge — they are never passively/proposed-inserted the way code-derived insights can be. This holds for both eval seeds and production ingestion: all explicit asset adds go straight to active. (Consistent with the "all explicit adds → approved active" rule and the graph-vs-review separation.)

### Resolved decisions (multi-tenancy, caching, retrieval, failure)

- **Dedup vs tenancy.** Asset *nodes* are deduped **per-tenant** (scoped within `(org_id, user_id)`, never shared across tenants — preserves isolation). The caption/embedding **cache** is **global** (captions/embeddings of an image are not tenant-sensitive, so caption once for everyone).
- **Cache key.** `hash(canonical_png_bytes) + model_id + prompt_version` — so swapping the VLM model or changing the caption prompt correctly invalidates stale captions rather than serving them forever.
- **Retrieval surface.** An asset's derived card embeds the relative asset path (as today's `assets/<file>.png` convention) and the asset node carries the file reference; retrieval surfaces both the card text and a usable path, so a sandboxed agent can place the image in `<img>` tags. Mounting paths must line up with what the card claims.
- **Caption failure degrades gracefully.** If a VLM caption call fails (rate limit, API error, unsupported image), the asset still lands with its deterministic card (filename + folder + layer names); the caption is retried lazily later. One bad call never fails a whole dump.

### Deferred for later

- **CLIP/SigLIP pixel embeddings + cross-modal pixel search.** Adds a separate model, separate vector space, and a second index. Revisit only if literal "find me an image that looks like X" is ever required. Not needed for the volta build.
- **Lazy caption-on-retrieval.** Eager-at-ingest + hash cache was chosen for card quality on day one; lazy generation remains a future cost optimization if dumps grow large with low retrieval rates.

### Outside scope (rejected)

- **Explicit cross-modal entity-linking edges** (`imageOf`/`sameAs`). No parity with how text is stored/retrieved in Praxis; would add an image-only mechanism for no retrieval benefit.
- **Graph-as-pointer-index / object-store byte storage** as a coupled decision — the bytes-storage location is noted as an open question, not a rejected idea, but its resolution is independent of this feature.

## Success looks like (acceptance)

- Running ingestion on `AlchemistAssets` produces asset nodes + derived cards in the graph via `build_trio`.
- A second run with no file changes performs no captioning/embedding work and writes nothing new.
- A PSD and its exported PNG share one `cluster_id` and one canonical captioned card.
- The volta_video eval passes with auto-generated cards and runs offline/deterministically in CI.

## Dependencies / Assumptions

- **Assumption:** the existing text embedder + dedup/merge path produces useful proximity between an image's text card and related text insights (the implicit-linking bet). If proximity proves too weak in practice, explicit linking returns as a follow-up brainstorm.
- **Assumption:** `psd-tools` reliably yields layer names + embedded thumbnails for the dump's PSDs (smart objects unsupported — flatten/skip those layers).
- **Dependency:** a VLM with image input for captioning, via OpenRouter. **Decided: `google/gemini-flash-1.5-8b`** (vision-capable, ~$0.04/M input tokens — the cheapest capable tier). Step-up fallback if caption quality on flat PNGs is weak: `google/gemini-2.0-flash-001`. A `FakeEmbedder`-style stand-in / committed cache keeps CI offline.
- **Dependency:** a perceptual-hash library (e.g. `imagehash`/`imagededup`).

## Outstanding Questions (for planning)

- The caption prompt (what an asset card should contain, format). VLM model is decided (`google/gemini-flash-1.5-8b` via OpenRouter).
- Perceptual-hash algorithm + near-duplicate threshold.
- Concrete asset-node representation in the pgvector-backed graph schema (new node type vs tagged insight) and provenance-edge shape.
- Where asset bytes live (in-graph reference vs object store) and how fixtures are mounted vs ingested. (Note: retrieval-surface decision above assumes a resolvable file reference, whichever storage wins.)
- Which raster library performs canonical-PNG normalization, and the PSD→PNG render path.
- Batch size / concurrency limits for the enrichment pass.

## Upstream

Full ideation (10 ranked ideas across modeling + throughput layers, with sources): `docs/ideation/2026-06-23-image-dump-ingestion-ideation.md`.
