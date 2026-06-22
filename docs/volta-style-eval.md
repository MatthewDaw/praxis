# Volta style-reproduction eval — resume here

**Single source of truth.** Point a fresh session at this file. Written 2026-06-21.

## The goal
Prove the Praxis knowledge graph can drive **style reproduction**: seed knowledge about a reference YouTube explainer video (Alessandro Volta, by channel "TheAlchemist"), ingest the Wikipedia *Alessandro Volta* article as raw text, and have an agent produce an **HTML mockup that mimics the video's style** — graded by closeness.

## The plan (decided)
- **Phase 1 — DO THIS NEXT. Eval-lean, zero trio code changes.** Images + style seeded as plain text; the real PNGs ship as sandbox fixtures the HTML can `<img>`. Validates the eval design before any architecture work.
- **Phase 2 — later, only after Phase 1 is validated.** Build the real capability: `ImageAsset` + `StyleProfile` storage/retrieval, reader surfacing of assets as `<img>`, a vision ingestion seam, and a video→style extraction pipeline. Spec in the appendix below.

The graph's `read/write(str)` contract stays frozen in both phases — images and style are always text + file references, never binary in the store.

---

## NEXT STEP: build the Phase 1 eval

Create `knowledge/evals/cases/matt/volta_video/`. Copy the shape of the existing reference case
`knowledge/evals/cases/matt/applications/sekai/embedding_retrieval_two_tower/case.yaml`
(it's a full-pipeline, sandbox, `via_ingestor` + rubric + deterministic-checks case — the exact pattern needed).

### 1. `case.yaml`
- `id: matt_volta_video_mock`, `substrate: in_memory`, `target_commit: '0000000000000000000000000000000000000000'`.
- **`seed_prompt`** — instruct the agent to write a single self-contained `volta.html` that mimics the reference video's style (from the seeded style profile + asset cards) while drawing content from the seeded Volta facts + the ingested Wikipedia article; reference seeded assets by their given `assets/…` paths; write only `volta.html`.
- **`seeded_insight.direct_to_graph`** (list of strings):
  - (a) **Volta facts** from the video narration — atomic, terse (late talker; self-taught amateur; ~40 yrs at Pavia; Royal Society; Galvani frog-twitch rivalry; disproves "animal electricity"; invents the voltaic pile / first battery 1801; shows Napoleon; foundation of the electrical revolution).
  - (b) **Style profile** — distilled from `knowledge/assets/reference-videos/thealchemist/volta_decomposition.md`. Three planes (visual / voice / editing / structure), each fact tagged `channel-constant` or `episode-variable`; quantifiables as tokens (`bg.color=#7FFFE0`, `cut≈1 per 10s`), prose for tone; a couple of on-style vs off-style exemplar lines; the 7-beat arc.
  - (c) **Asset cards** — one text block per fixture image: `id | caption | role | palette-hex | path=assets/<file>.png`.
- **`seeded_insight.via_ingestor`** — `[ <full plain text of https://en.wikipedia.org/wiki/Alessandro_Volta> ]`. Fetch once, strip wiki markup, vendor inline so the case is hermetic. Mind YAML escaping (follow the sekai case's block-scalar style).
- **`deterministic_checks`:**
  - `knowledge.evals.deterministic_checks.builds:writes_file` `{path: volta.html}`
  - `knowledge.evals.deterministic_checks.text:regex_matches` `{pattern: '(?i)#7?fffe0'}` (match the cyan hex you used)
  - `knowledge.evals.deterministic_checks.text:requires_all_substrings` `{texts: [battery, "1801", Galvani, Pavia]}`
  - `knowledge.evals.deterministic_checks.builds:contains_text` `{text: assets/}`
- **`rubric`** (weighted items, judge scores each 0..1; pass threshold 0.5):
  - `style_fidelity` (weight 2.5) — cyan collage bg, mascot, hard-cut scene sections, anachronistic first-person voice, fake-ending + CTA beats.
  - `factual_grounding` (1.5) — claims trace to seeded facts / Wikipedia, no fabrication.
  - `narrative_arc` (1.0) — hook → origins → rise → fake-ending → Galvani → battery → legacy, in order.
  - `content_style_separation` (1.0) — Volta rendered in the channel's style, not a copy of the reference's exact wording.

### 2. `fixture/assets/` (singular `fixture/` — `load_case` looks for exactly that)
Crop 4–6 PNGs from `knowledge/assets/reference-videos/thealchemist/scenes_volta/` (use the `contact_1.jpg`/`contact_2.jpg` sheets to pick frames) with ffmpeg at
`C:\Users\mattd\.local\ffx\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe`:
the **mascot** (recurring blue pixel figure, green cap, "A"), a **cyan bg** swatch, the **Volta portrait**, a **Galvani frog-leg engraving**. Name them to match the `path=` in the asset cards (`mascot.png`, `bg_cyan.png`, `volta_portrait.png`, `galvani_frog.png`).
ffmpeg crop: `& $ff -i scenes_volta\s_0XX.jpg -vf "crop=W:H:X:Y" fixture\assets\mascot.png`

### 3. Validate
- `uv run python -m knowledge.evals.run --fake matt_volta_video_mock` — load/shape smoke test (SKIPs sandbox parts; no file produced offline).
- `uv run python -m knowledge.evals.run matt_volta_video_mock` — real Claude Code runner + OpenRouter judge (`OPENROUTER_API_KEY` in `.env`); this is the run that mounts fixtures, produces `volta.html`, and grades the rubric.
- **Success:** a faithful HTML scores ≥ pass; an empty/plain output fails (rubric discriminates style).

### Gotchas
- The case needs the **sandbox** runner (fixtures + file artifact) — only the real `claude` backend provides it; `--fake`/`--openrouter`/`--structured` will SKIP the sandbox bits.
- Tag style facts `channel-constant`/`episode-variable` now so the Phase-2 migration to a real `StyleProfile` is mechanical.

---

## Inventory (already in place)
Under `knowledge/assets/reference-videos/thealchemist/`:
- Volta + Faraday `.mp4` + `.en.vtt` + `.info.json` + `.webp` (Faraday = same template, second data point).
- `volta_decomposition.md` — **the style source** (image assets, voice, editing, 7-beat arc, production-spec table).
- `frames_volta/` (161 frames), `scenes_volta/` (33 scene-cut frames + 2 contact sheets) — crop fixtures from here.

Tooling: ffmpeg at `C:\Users\mattd\.local\ffx\…\ffmpeg.exe`; `yt-dlp` on PATH (`C:\Users\mattd\.local\bin`).

Conventions: new eval cases live under `knowledge/evals/cases/matt/`; KG access goes through `build_trio` ingestor/reader, never direct graph calls (relevant in Phase 2). Don't commit/push unless asked — new assets + this doc are untracked. Reference video © TheAlchemist; internal eval/research use only.

---

## Appendix — Phase 2 capability spec (build after Phase 1 is validated)

Frozen `read/write(str)` preserved; everything additive, behind the existing offline `Fake*` seams.

**2A — Image assets.** `ImageAsset` model: `asset_id` (= sha256 of bytes → free dedup), `caption`, `tags` (incl. `role`, `palette`), `uri` (content-addressed under `knowledge/assets/`, later S3/CloudFront); serializes to/from a canonical text asset-card so it rides `write(str)`. Add `write_image(asset)` / `read_images(context)` to the graph (retrieval v1 = caption/tag keyword match, CI-safe via `FakeEmbedder`; `modality` tag leaves a CLIP/Voyage upgrade path). `GraphReader.read()` appends an "Available image assets" section rendering each as `<img src="uri" alt="caption">`. Vision ingestion seam mirroring `FakeLLM`/`OpenRouterLLM`: `FakeVisionDescriber` (fixture→canned caption, offline) + OpenRouter vision. Add `SeededInsight.images: list[ImageAsset]`; migrate the Volta case from text cards → seeded `ImageAsset`s (same data, real path).

**2B — Style profile.** `StyleProfile`: three planes, every fact tagged `channel-constant` vs `episode-variable`; quantifiables as design tokens + prose for tone; golden + contrastive exemplars; an ordered beat-template. Visual plane declares **role-typed asset slots** (`mascot×{2-3}`, `portraits×5-7`, `engravings×2-3`) that resolve as retrieval queries against 2A's asset cards — this is where images and style join. Application: reader co-fires style + bound assets as an **imperative contract** ("background MUST be `#7FFFE0`"), optional token→CSS scaffold, two labeled channels (`STYLE` vs `CONTENT`), and a **precedence rule** ("retrieved style outranks your tasteful defaults") — necessary because LLMs regress to polished neutrals and fight a deliberately lo-fi look.

**2C — Extraction pipeline (raw video → profile + cards).** Hybrid, mostly deterministic/CI-safe: cut-rhythm histogram from ffmpeg scene-cut timestamps; palette + background-constancy from frames; perceptual-hash clustering to find recurring elements (the mascot); OCR (Tesseract) for burned-in text; WPM from the `.vtt`; optional librosa cut-to-beat alignment. Model-gated on a few keyframes only: structured VLM captions + narration tone labels (`FakeVisionDescriber`/`FakeLLM` stub offline). Output: a `StyleProfile` + `ImageAsset`s — i.e. the pipeline produces what Phase 1 hand-authored.

**Sequence:** 2A → 2B (binds to 2A) → 2C. **Explicitly out:** base64 bytes in the graph; ColPali multi-vector pages; caption-on-read; rendered/screenshot visual grading (text-source rubric only).
