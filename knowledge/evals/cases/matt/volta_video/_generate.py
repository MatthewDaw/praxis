"""Generate the Volta style-reproduction eval case (Phase 1).

See ``docs/volta-style-eval.md``. This is a FULL-PIPELINE, sandbox case:

  - ``seeded_insight.direct_to_graph`` carries hand-authored knowledge in three
    groups — Volta facts (from the video narration), a style profile (distilled
    from ``volta_decomposition.md``), and asset cards (one per fixture image).
  - ``seeded_insight.via_ingestor`` carries the vendored Wikipedia article text
    (``sources/wikipedia_volta.txt``) so the case is hermetic.
  - The agent is told to write a single self-contained ``volta.html`` that
    reproduces the channel's style while drawing content from the seeded facts +
    Wikipedia, referencing the mounted fixtures by their ``assets/<file>.png``
    paths.
  - Deterministic checks assert the artifact + key tokens; a rubric grades style
    fidelity, factual grounding, narrative arc, and content/style separation.

Style facts are tagged ``[channel-constant]`` / ``[episode-variable]`` inline so
the Phase-2 migration to a real ``StyleProfile`` is mechanical (see the appendix
in the doc).

Re-generate:  uv run python knowledge/evals/cases/matt/volta_video/_generate.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).parent
WIKIPEDIA = (HERE / "sources" / "wikipedia_volta.txt").read_text(encoding="utf-8")

SEED_PROMPT = (
    "You are producing a single self-contained HTML mockup, volta.html, that "
    "reproduces the visual and narrative STYLE of a specific YouTube explainer "
    "channel (TheAlchemist) applied to NEW content about Alessandro Volta.\n\n"
    "You have been given background knowledge: (1) atomic facts about Volta from "
    "the reference video, (2) a STYLE PROFILE describing the channel's look, "
    "voice, editing, and narrative arc, and (3) asset cards describing the image "
    "files mounted alongside you under assets/. You also have the full Wikipedia "
    "article on Volta for additional factual content.\n\n"
    "Write ONE file named volta.html and create no other files. Requirements:\n"
    "- Reproduce the channel STYLE from the style profile: the flat cyan "
    "background, the recurring pixel mascot as connective tissue, hard-cut "
    "collage sections, the fast first-person sarcastic anachronistic voice, the "
    "fake-ending beat, and the like/subscribe + next-episode CTA.\n"
    "- Render each narrative beat as its own section, in the order the style "
    "profile's arc specifies.\n"
    "- Embed the mounted images with <img> tags referencing their given "
    "assets/<file>.png paths (do not invent other asset paths).\n"
    "- Draw factual content from the seeded Volta facts and the Wikipedia "
    "article; do not fabricate. Render Volta IN the channel's style — do not copy "
    "the reference video's exact wording.\n"
    "- The cyan background color must be #7FFFE0."
)

# (a) Volta facts from the video narration — atomic, terse.
VOLTA_FACTS = [
    "Alessandro Volta is the namesake of the 'volt', the unit of electric potential.",
    "Volta was a late talker as a child and was not expected to be exceptional.",
    "Volta was largely a self-taught amateur who rejected a career in law for science.",
    "Volta published his first paper on electricity at age 22.",
    "Volta taught for roughly 40 years at the University of Pavia.",
    "Volta was inducted into the Royal Society of London.",
    "Luigi Galvani was Volta's rival; Galvani's twitching-frog-leg experiments suggested 'animal electricity'.",
    "Galvani's demos of making dead frog legs twitch were a viral dinner-party spectacle ('raising the dead').",
    "Volta disproved Galvani's 'animal electricity', showing the electricity came from the metals, not the living tissue.",
    "After about a decade of work Volta invented the voltaic pile — the first electric battery — in 1801.",
    "Volta demonstrated the battery to Napoleon, then Emperor.",
    "Volta's battery laid the foundation for the electrical revolution.",
]

# (b) Style profile — distilled from volta_decomposition.md. Each fact tagged
# [channel-constant] (the series template) or [episode-variable] (this episode).
STYLE_PROFILE = [
    "[channel-constant] visual: the whole piece is a collage on a flat cyan background; bg.color=#7FFFE0.",
    "[channel-constant] visual: assets are cut-out PNGs composited on the background, 2-4 elements on screen at once; almost no full-frame footage.",
    "[channel-constant] visual: a recurring pixel-art mascot ('Alchemist guy' — blue body, green cap, green 'A') is the narrator stand-in, present in nearly every scene as connective tissue.",
    "[channel-constant] editing: hard cuts only, no crossfades; a new visual roughly every 10 seconds (cut=1 per 10s), faster during joke runs.",
    "[channel-constant] editing: deliberately low-fi finish; a meme/reaction image is inserted as the punchline for each joke beat.",
    "[channel-constant] voice: single first-person narrator, fast, dry, sarcastic; one historical fact then one anachronistic joke; direct address to the viewer and fourth-wall asides.",
    "[channel-constant] voice: anachronism is the core comedic engine (modern slang over 18th-century content, e.g. 'went viral', 'academic big leagues').",
    "[channel-constant] structure: a fake 'happily ever after' ending head-fake, then yank back to the real conflict.",
    "[channel-constant] structure: outro CTA = like/subscribe + name the next scientist episode + vote in the comments.",
    "[channel-constant] arc (7 beats, in order): 1 cold-open hook (the unit named after them) -> 2 origins (late talker, rejects law) -> 3 rise (first paper, Pavia, Royal Society) -> 4 fake ending -> 5 rival/conflict (Galvani, frog legs) -> 6 triumph (disproves Galvani, invents the 1801 battery, shows Napoleon) -> 7 legacy + CTA.",
    "[channel-constant] audio: dry voice throughout; a music sting only over the outro.",
    "[episode-variable] subject: Alessandro Volta; cold-open unit gag = the 'volt'; rival = Galvani; invention = the voltaic pile (1801); cameo = Napoleon.",
    "on-style line (good): 'So most of you have probably met this little guy on a battery — the volt. Cute. Anyway, let me tell you about the nerd it's named after.'",
    "off-style line (bad, too formal): 'Alessandro Volta was an esteemed Italian physicist whose contributions to science remain influential to this day.'",
]

# (c) Asset cards are no longer hand-authored. They're generated by the real
# ImageIngestor from the mounted fixture (``fixture/assets/``: the named cues +
# the AlchemistAssets dump under Common/ and Photoshop Files/), captioned by a
# VLM and collapsed by perceptual-hash variant clustering. See
# ``via_image_ingestor`` below and docs/plans/2026-06-23-001-feat-image-asset-ingestion-plan.md.


def main() -> None:
    case = {
        "id": "matt_volta_video_mock",
        # Vector substrate + cached embedder so seeding exercises the real embed +
        # dedup path offline; caption_model captions canonical images (replayed
        # from the committed cassette in CI). Refresh both caches locally with a
        # key: `uv run python -m knowledge.evals.embed_cache --refresh` and
        # `uv run python -m knowledge.evals.caption_cache --refresh`.
        "substrate": "vector",
        "embedder": "cached",
        "caption_model": "google/gemini-2.5-flash-lite",
        # The seed prompt promises the Wikipedia article as retrievable factual
        # content (and the factual_grounding rubric item checks claims trace to
        # it), so the ingested article must land *active* — established
        # background, not a pending candidate. Without this, all 36 Wikipedia
        # facts land "proposed" and the active-gated reader hides them, leaving
        # the agent ungrounded (the same fix as the matt/applications/* suite;
        # see docs/proposals/completed/2026-06-23-active-fact-retrievability.md).
        "ingest_state": "active",
        "seed_prompt": SEED_PROMPT,
        "target_commit": "0" * 40,
        "needs": ["sandbox"],
        "output_file": "volta.html",
        "seeded_insight": {
            "direct_to_graph": VOLTA_FACTS + STYLE_PROFILE,
            "via_ingestor": [WIKIPEDIA],
            # The whole mounted assets/ tree (named cues + AlchemistAssets dump),
            # distilled to derived cards and written active.
            "via_image_ingestor": ["assets"],
        },
        "deterministic_checks": [
            {
                "name": "wrote_volta_html",
                "ref": "knowledge.evals.deterministic_checks.builds:writes_file",
                "params": {"path": "volta.html"},
            },
            {
                "name": "cyan_background",
                "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
                "params": {"pattern": "(?i)#7?fffe0"},
            },
            {
                "name": "core_facts_present",
                "ref": "knowledge.evals.deterministic_checks.text:requires_all_substrings",
                "params": {"texts": ["battery", "1801", "Galvani", "Pavia"]},
            },
            {
                "name": "references_assets",
                "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
                "params": {"pattern": "assets/"},
            },
            # Ingestion guardrails: assert the seed actually populated the graph,
            # so a no-op ingestor (zero active asset cards / zero retrievable
            # Wikipedia facts) FAILS the eval instead of slipping through on the
            # artifact-only checks above. These read ctx.injected_knowledge (the
            # active facts the reader surfaced to the agent).
            {
                "name": "active_asset_cards_present",
                "ref": "knowledge.evals.deterministic_checks.graph:min_active_asset_cards",
                "params": {"minimum": 1},
            },
            {
                "name": "active_wikipedia_facts_present",
                "ref": "knowledge.evals.deterministic_checks.graph:min_non_seed_facts",
                "params": {"minimum": 3, "seed_texts": VOLTA_FACTS + STYLE_PROFILE},
            },
        ],
        "rubric": {
            "id": "matt_volta_video_mock_v1",
            "items": [
                {
                    "id": "style_fidelity",
                    "criterion": (
                        "Reproduces the channel's style: flat cyan (#7FFFE0) collage "
                        "background, the recurring pixel mascot, hard-cut scene "
                        "sections, a fast first-person sarcastic anachronistic voice, "
                        "and the fake-ending + like/subscribe CTA beats."
                    ),
                    "weight": 2.5,
                },
                {
                    "id": "factual_grounding",
                    "criterion": (
                        "Claims trace to the seeded Volta facts or the Wikipedia "
                        "article (volt namesake, Pavia, Royal Society, Galvani rivalry, "
                        "voltaic pile in 1801, Napoleon). No fabricated history."
                    ),
                    "weight": 1.5,
                },
                {
                    "id": "narrative_arc",
                    "criterion": (
                        "Follows the 7-beat arc in order: hook -> origins -> rise -> "
                        "fake-ending -> Galvani conflict -> battery triumph -> legacy + CTA."
                    ),
                    "weight": 1.0,
                },
                {
                    "id": "content_style_separation",
                    "criterion": (
                        "Volta's content is rendered IN the channel's style, not a "
                        "copy of the reference video's exact wording — style and "
                        "subject are recombined, not transcribed."
                    ),
                    "weight": 1.0,
                },
            ],
        },
    }

    out = HERE / "case.yaml"
    header = (
        "# GENERATED by _generate.py — edit that script, not this file.\n"
        "# Volta style-reproduction eval (Phase 1). See docs/volta-style-eval.md.\n"
        "# Full-pipeline sandbox case: seed Volta facts + style profile (direct_to_graph),\n"
        "# the Wikipedia article (via_ingestor), and the mounted image assets\n"
        "# (via_image_ingestor -> ImageIngestor: deterministic + VLM-captioned cards);\n"
        "# the agent writes volta.html mimicking TheAlchemist's style; rubric grades it.\n"
    )
    out.write_text(
        header + yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=4096),
        encoding="utf-8",
    )
    print(f"wrote {out.relative_to(HERE.parents[4]) if False else out}")


if __name__ == "__main__":
    main()
