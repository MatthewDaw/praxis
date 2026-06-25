"""Regenerate the committed embedding cache for ``embedder: cached`` eval cases.

Run locally with ``OPENROUTER_API_KEY`` (+ optional ``OPENROUTER_EMBED_MODEL``)
set. It deletes the model's cache file, then re-runs every ``cached`` case so the
recording ``CachedEmbedder`` captures exactly the vectors those cases embed at
runtime (post-redaction, post-dedup, plus the reader's query) â€” then commit the
file. Starting from empty drops orphaned keys left by edited/removed texts.

    uv run python -m knowledge.evals.embed_cache --refresh

To add one or a few new cases without re-embedding the whole corpus, use ``--add``,
which merges the named case(s)' vectors into the existing cache (no delete-and-rebuild):

    uv run python -m knowledge.evals.embed_cache --add <case_id> [<case_id> ...]

For image-asset (``caption_model``) cases, refresh the caption cassette *first*
(``knowledge.evals.caption_cache --refresh``) so the card texts embedded here
replay the same captions the runtime does; otherwise the recorded vectors key off
captions that won't match at replay time and seeding is a loud miss.
"""

from __future__ import annotations

import argparse
import os
import sys

from knowledge.evals.run import (
    EMBED_CACHE_DIR,
    _build_trio_for,
    _embed_model,
    _seed_image_assets,
    _slug,
    load_cases,
    load_env,
)


def _no_key() -> bool:
    if not os.getenv("OPENROUTER_API_KEY"):
        print(
            "set OPENROUTER_API_KEY (+ OPENROUTER_EMBED_MODEL) to record the cache",
            file=sys.stderr,
        )
        return True
    return False


def _record_case(case) -> None:
    """Embed exactly the vectors ``case`` needs at runtime (writes + the reader query).

    A key is set, so the case's ``CachedEmbedder`` records misses and merges them into
    the shared fixture. Seed exactly as the runtime producers do (see
    ``run._seed_knowledge`` / ``_produce_graph_reader``): ``direct_to_graph`` lands
    "active", ``via_ingestor`` "proposed". Retrieval is gated to active facts and
    ``search`` returns early on no active candidates *before* embedding the query, so a
    proposed-only seed would never record the reader query — replaying it offline would
    then be a loud cache miss. Image-asset cards land "active" through the same embedder.
    """
    graph, ingestor, reader = _build_trio_for(case)
    for text in case.seeded_insight.direct_to_graph:
        graph.write(text, state="active")
    for text in case.seeded_insight.via_ingestor:
        ingestor.ingest(text)
    _seed_image_assets(case, graph)
    if case.seed_prompt:
        reader.read(case.seed_prompt)


def refresh() -> int:
    """Rebuild the whole cache from scratch — drops orphaned keys from edited/removed texts."""
    if _no_key():
        return 1

    model = _embed_model()
    cache = EMBED_CACHE_DIR / f"{_slug(model)}.json"
    cache.unlink(missing_ok=True)  # rebuild fresh so changed/removed texts don't orphan

    cached = [c for c in load_cases() if c.embedder == "cached"]
    if not cached:
        print("no `embedder: cached` cases to record")
        return 0

    for case in cached:
        _record_case(case)
        print(f"recorded {case.id}")

    print(f"wrote {cache} ({model})")
    return 0


def add(case_ids: list[str]) -> int:
    """Incrementally record only the named cached case(s), MERGING into the existing cache.

    Unlike ``refresh`` this does NOT delete the cache first — the ``CachedEmbedder`` re-reads
    the on-disk vectors (hits, no re-embed) and records only the genuinely-new texts (e.g. a
    new case's reader query). Use when adding a case without paying to re-embed the whole
    corpus. Orphaned keys are not pruned (that is ``refresh``'s job).
    """
    if _no_key():
        return 1

    cached = {c.id: c for c in load_cases() if c.embedder == "cached"}
    unknown = [cid for cid in case_ids if cid not in cached]
    if unknown:
        print(
            f"not an `embedder: cached` case (nothing to embed): {unknown}. "
            f"Pass an id from: {sorted(cached)}",
            file=sys.stderr,
        )
        return 1

    model = _embed_model()
    cache = EMBED_CACHE_DIR / f"{_slug(model)}.json"
    for cid in case_ids:  # no unlink: existing vectors stay, only new texts record
        _record_case(cached[cid])
        print(f"recorded {cid}")

    print(f"merged {len(case_ids)} case(s) into {cache} ({model})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.evals.embed_cache")
    parser.add_argument(
        "--refresh", action="store_true", help="rebuild the whole committed embedding cache"
    )
    parser.add_argument(
        "--add", nargs="+", metavar="CASE_ID",
        help="incrementally embed only the named cached case(s), merging into the existing "
             "cache (no full rebuild)",
    )
    args = parser.parse_args(argv)
    if args.refresh and args.add:
        parser.error("pass --refresh or --add, not both")
    if args.refresh:
        load_env()
        return refresh()
    if args.add:
        load_env()
        return add(args.add)
    parser.print_help()
    return 0


if __name__ == "__main__":
    main()
