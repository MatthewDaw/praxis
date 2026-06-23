"""Regenerate the committed ingestion cassette for ``ingest_model`` eval cases.

Run locally with ``OPENROUTER_API_KEY`` set. It deletes each model's cassette
file, then re-runs every ``ingest_model`` case so the recording
``IngestionCassette`` captures exactly the distilled text those cases produce at
runtime — then commit the file. Starting from empty drops orphaned keys left by
edited/removed seeded inputs.

This is **step 1** of the two-step refresh: fix the distilled text first, then run
``embed_cache --refresh`` to embed the now-stable strings (reversing the order
caches vectors for soon-to-be-stale text).

    uv run python -m knowledge.evals.ingestion_cache --refresh
"""

from __future__ import annotations

import argparse
import os
import sys

from knowledge.evals.run import (
    INGEST_CACHE_DIR,
    _build_trio_for,
    _slug,
    load_cases,
    load_env,
)


def refresh() -> int:
    if not os.getenv("OPENROUTER_API_KEY"):
        print("set OPENROUTER_API_KEY to refresh the ingestion cassette", file=sys.stderr)
        return 1

    cases = [c for c in load_cases() if c.ingest_model]
    if not cases:
        print("no `ingest_model` cases to record")
        return 0

    # Rebuild each model's cassette fresh so changed/removed inputs don't orphan.
    for model in sorted({c.ingest_model for c in cases}):
        (INGEST_CACHE_DIR / f"{_slug(model)}.json").unlink(missing_ok=True)

    for case in cases:
        # A key is set, so each case's IngestionCassette records misses and saves
        # to the shared fixture. Driving the ingestor distills each seeded input.
        _, ingestor, _ = _build_trio_for(case)
        for text in case.seeded_insight.via_ingestor:
            ingestor.ingest(text)
        print(f"recorded {case.id}")

    for model in sorted({c.ingest_model for c in cases}):
        print(f"wrote {INGEST_CACHE_DIR / f'{_slug(model)}.json'} ({model})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.evals.ingestion_cache")
    parser.add_argument(
        "--refresh", action="store_true", help="rebuild the committed ingestion cassette"
    )
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.print_help()
        return 0
    load_env()
    return refresh()


if __name__ == "__main__":
    sys.exit(main())  # contract: a missing key must exit non-zero, not silently no-op
