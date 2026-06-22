"""Fetch large reference assets from S3, caching locally so each downloads once.

The reference data (videos, extracted frames, subtitles) is too large to commit
to git — it is ignored (see ``.gitignore``) and hosted in S3 instead. Eval
fixtures call :func:`ensure_asset` to get a local path: on the first run the file
is downloaded from S3 into the local cache; every run after that reuses the
cached copy with no network call. So the download cost is paid once per machine,
not once per eval run.

Cache location: ``knowledge/assets/`` (gitignored), mirroring the S3 key layout.

Configuration (environment):

    PRAXIS_ASSETS_BUCKET   S3 bucket holding the assets (required to download).
    PRAXIS_ASSETS_PREFIX   Optional key prefix within the bucket (default none).

Populate the bucket once from a machine that has the local files:

    uv run python -m knowledge.evals.assets --upload
"""

from __future__ import annotations

import os
from pathlib import Path

# knowledge/assets/ — the gitignored cache root. assets.py lives in knowledge/evals/,
# so parents[1] is knowledge/.
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"


def _bucket() -> str:
    bucket = os.getenv("PRAXIS_ASSETS_BUCKET")
    if not bucket:
        raise RuntimeError(
            "PRAXIS_ASSETS_BUCKET is not set, so reference assets cannot be "
            "downloaded. Set it to the S3 bucket name (and PRAXIS_ASSETS_PREFIX "
            "if the assets live under a key prefix), or drop the file into "
            f"{ASSETS_DIR} manually."
        )
    return bucket


def _s3_key(key: str) -> str:
    """Map a cache-relative ``key`` to its full S3 key (honoring the prefix)."""
    prefix = os.getenv("PRAXIS_ASSETS_PREFIX", "").strip("/")
    return f"{prefix}/{key}" if prefix else key


def ensure_asset(key: str, *, cache_dir: Path = ASSETS_DIR) -> Path:
    """Return a local path to the asset at ``key``, downloading from S3 once.

    ``key`` is the asset's path relative to the assets root (which mirrors the S3
    layout), e.g. ``reference-videos/thealchemist/frames_volta/f_001.jpg``.

    If a non-empty local copy already exists it is returned immediately — no
    network call, no credentials needed. On a cache miss the object is downloaded
    to a temp file and atomically renamed into place, so an interrupted download
    never leaves a partial file that later looks cached.
    """
    local = cache_dir / key
    if local.exists() and local.stat().st_size > 0:
        return local

    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_name(local.name + ".part")

    import boto3  # local import: only needed on a cache miss, keeps offline runs free

    boto3.client("s3").download_file(_bucket(), _s3_key(key), str(tmp))
    os.replace(tmp, local)  # atomic: a half-written .part is never mistaken for cached
    return local


def ensure_assets(keys: list[str], *, cache_dir: Path = ASSETS_DIR) -> list[Path]:
    """Cache-and-return several assets; convenience over :func:`ensure_asset`."""
    return [ensure_asset(k, cache_dir=cache_dir) for k in keys]


def _iter_local_files(cache_dir: Path):
    """Every cached file under ``cache_dir`` as (absolute_path, cache_relative_key)."""
    for path in sorted(cache_dir.rglob("*")):
        if path.is_file() and not path.name.endswith(".part"):
            yield path, path.relative_to(cache_dir).as_posix()


def upload_all(cache_dir: Path = ASSETS_DIR) -> int:
    """Upload every local cached file to S3 (one-time bucket population).

    Mirrors the local layout to S3 keys (under the configured prefix). Returns the
    number of files uploaded. Run from a machine that holds the local assets.
    """
    import boto3

    client = boto3.client("s3")
    bucket = _bucket()
    count = 0
    for path, key in _iter_local_files(cache_dir):
        client.upload_file(str(path), bucket, _s3_key(key))
        print(f"uploaded {key}")
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="knowledge.evals.assets")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload every local file under knowledge/assets/ to S3 (one-time)",
    )
    args = parser.parse_args(argv)

    if args.upload:
        n = upload_all()
        print(f"uploaded {n} file(s) to s3://{_bucket()}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    main()
