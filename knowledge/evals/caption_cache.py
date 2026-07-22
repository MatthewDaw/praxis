"""Regenerate the committed VLM caption cassette for ``caption_model`` eval cases.

Run locally with ``OPENROUTER_API_KEY`` set. It deletes each caption model's
cassette, then re-runs every ``caption_model`` case so the recording
``CaptionCassette`` captures exactly the captions those cases embed at runtime
(one per canonical image of each variant cluster) — then commit the files.
Starting from empty drops orphaned keys left by edited/removed assets.

    uv run python -m knowledge.evals.caption_cache --refresh
"""

from __future__ import annotations

import argparse

from knowledge.evals.run import (
    CAPTION_CACHE_DIR,
    _image_asset_dirs,
    _seed_image_assets,
    _slug,
    load_cases,
    load_env,
    missing_openrouter_key,
)


def refresh(cases=None) -> int:
    if missing_openrouter_key("set OPENROUTER_API_KEY to refresh the caption cassette"):
        return 1

    cases = load_cases() if cases is None else cases
    caption_cases = [
        c for c in cases if c.caption_model and _image_asset_dirs(c)
    ]
    if not caption_cases:
        print("no `caption_model` cases with image assets to record")
        return 0

    # Rebuild fresh so changed/removed assets don't orphan stale captions.
    for model in {c.caption_model for c in caption_cases}:
        (CAPTION_CACHE_DIR / f"{_slug(model)}.json").unlink(missing_ok=True)

    # A throwaway in-memory graph: captioning happens in ImageIngestor.synthesis
    # (which records to the cassette); we don't need the embeddings here.
    from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import InMemoryGraph

    for case in caption_cases:
        _seed_image_assets(case, InMemoryGraph.create())
        print(f"recorded {case.id} ({case.caption_model})")

    print(f"wrote caption cassette(s) -> {CAPTION_CACHE_DIR}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.evals.caption_cache")
    parser.add_argument(
        "--refresh", action="store_true", help="rebuild the committed caption cassette"
    )
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.print_help()
        return 0
    load_env()
    return refresh()


if __name__ == "__main__":
    main()
