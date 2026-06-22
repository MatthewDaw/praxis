"""Tests for the S3 asset cache (download-once behavior)."""

from __future__ import annotations

import sys
import types

import pytest

from knowledge.evals import assets


class _FakeS3:
    """Records download calls and writes a stub file, standing in for boto3's client."""

    def __init__(self) -> None:
        self.downloads: list[tuple[str, str, str]] = []

    def download_file(self, bucket: str, key: str, dest: str) -> None:
        self.downloads.append((bucket, key, dest))
        with open(dest, "wb") as f:
            f.write(b"stub-bytes")


@pytest.fixture
def fake_boto3(monkeypatch):
    """Install a fake ``boto3`` module whose s3 client is a single _FakeS3."""
    client = _FakeS3()
    module = types.SimpleNamespace(client=lambda service: client)
    monkeypatch.setitem(sys.modules, "boto3", module)
    monkeypatch.setenv("PRAXIS_ASSETS_BUCKET", "test-bucket")
    monkeypatch.delenv("PRAXIS_ASSETS_PREFIX", raising=False)
    return client


def test_downloads_once_then_caches(tmp_path, fake_boto3):
    key = "reference-videos/x/frame.jpg"

    first = assets.ensure_asset(key, cache_dir=tmp_path)
    assert first == tmp_path / key
    assert first.read_bytes() == b"stub-bytes"
    assert len(fake_boto3.downloads) == 1  # downloaded on the miss

    second = assets.ensure_asset(key, cache_dir=tmp_path)
    assert second == first
    assert len(fake_boto3.downloads) == 1  # cache hit: no second download


def test_prefix_is_applied_to_key(tmp_path, fake_boto3, monkeypatch):
    monkeypatch.setenv("PRAXIS_ASSETS_PREFIX", "praxis/assets")
    assets.ensure_asset("a/b.txt", cache_dir=tmp_path)
    bucket, s3_key, _dest = fake_boto3.downloads[0]
    assert bucket == "test-bucket"
    assert s3_key == "praxis/assets/a/b.txt"


def test_missing_bucket_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PRAXIS_ASSETS_BUCKET", raising=False)
    # Force a cache miss so the bucket is actually needed.
    with pytest.raises(RuntimeError, match="PRAXIS_ASSETS_BUCKET"):
        assets.ensure_asset("missing.bin", cache_dir=tmp_path)


def test_existing_nonempty_file_skips_download(tmp_path, monkeypatch):
    # No boto3 installed and no bucket set: a present, non-empty file must still
    # resolve with zero network setup.
    monkeypatch.delenv("PRAXIS_ASSETS_BUCKET", raising=False)
    key = "already/here.txt"
    local = tmp_path / key
    local.parent.mkdir(parents=True)
    local.write_text("present")
    assert assets.ensure_asset(key, cache_dir=tmp_path) == local
