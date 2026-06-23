"""Regenerate the committed embedding cache for ``embedder: cached`` eval cases.

Run locally with ``OPENROUTER_API_KEY`` (+ optional ``OPENROUTER_EMBED_MODEL``)
set. It deletes the model's cache file, then re-runs every ``cached`` case so the
recording ``CachedEmbedder`` captures exactly the vectors those cases embed at
runtime (post-redaction, post-dedup, plus the reader's query) â€” then commit the
file. Starting from empty drops orphaned keys left by edited/removed texts.

    uv run python -m knowledge.evals.embed_cache --refresh
"""

from __future__ import annotations

import argparse
import os
import sys

from knowledge.evals.run import (
    EMBED_CACHE_DIR,
    _build_trio_for,
    _embed_model,
    _slug,
    load_cases,
    load_env,
)


def refresh() -> int:
    if not os.getenv("OPENROUTER_API_KEY"):
        print(
            "set OPENROUTER_API_KEY (+ OPENROUTER_EMBED_MODEL) to refresh the cache",
            file=sys.stderr,
        )
        return 1

    model = _embed_model()
    cache = EMBED_CACHE_DIR / f"{_slug(model)}.json"
    cache.unlink(missing_ok=True)  # rebuild fresh so changed/removed texts don't orphan

    cached = [c for c in load_cases() if c.embedder == "cached"]
    if not cached:
        print("no `embedder: cached` cases to record")
        return 0

    for case in cached:
        # A key is set, so each case's CachedEmbedder records misses and saves to
        # the shared fixture. Driving seed + read embeds writes and the query.
        graph, ingestor, reader = _build_trio_for(case)
        # Seed exactly as the runtime producers do (see run._seed_knowledge /
        # _produce_graph_reader): direct_to_graph lands "active", via_ingestor
        # "proposed". Retrieval is gated to active facts and ``search`` returns
        # early on no active candidates *before* embedding the query, so a
        # proposed-only seed would never record the reader query — replaying it
        # offline would then be a loud cache miss.
        for text in case.seeded_insight.direct_to_graph:
            graph.write(text, state="active")
        for text in case.seeded_insight.via_ingestor:
            ingestor.ingest(text)
        if case.seed_prompt:
            reader.read(case.seed_prompt)
        print(f"recorded {case.id}")

    print(f"wrote {cache} ({model})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.evals.embed_cache")
    parser.add_argument(
        "--refresh", action="store_true", help="rebuild the committed embedding cache"
    )
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.print_help()
        return 0
    load_env()
    return refresh()


if __name__ == "__main__":
    main()
