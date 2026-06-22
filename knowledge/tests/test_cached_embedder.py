"""Tests for CachedEmbedder — committed-vector replay, loud miss, recording."""

import pytest

from knowledge.llm.embedder_variants.cached_embedder import CachedEmbedder
from knowledge.llm.parent_embedder import Embedder


class _CountingEmbedder(Embedder):
    """Returns a stable vector per text and counts how many it embedded."""

    def __init__(self):
        self.embedded = 0

    def embed(self, texts):
        self.embedded += len(texts)
        return [[float(len(t)), 0.5, -0.25] for t in texts]


def test_records_then_replays_without_inner(tmp_path):
    cache = tmp_path / "m.json"
    inner = _CountingEmbedder()
    rec = CachedEmbedder(inner, cache, model_id="m", allow_compute=True)
    first = rec.embed(["alpha", "beta"])
    assert inner.embedded == 2
    assert cache.exists()  # saved on record

    # Replay: no inner, no recording — must serve from the committed file.
    replay = CachedEmbedder(None, cache, model_id="m", allow_compute=False)
    out = replay.embed(["alpha", "beta"])
    for o, f in zip(out, first):  # base64-float32 round-trips (within f32 precision)
        assert o == pytest.approx(f)


def test_miss_with_recording_disabled_is_loud(tmp_path):
    emb = CachedEmbedder(None, tmp_path / "empty.json", model_id="m", allow_compute=False)
    with pytest.raises(RuntimeError) as ei:
        emb.embed(["never-seen"])
    msg = str(ei.value)
    assert "cache miss" in msg and "embed_cache --refresh" in msg  # tells you how to fix


def test_model_id_is_part_of_the_key(tmp_path):
    cache = tmp_path / "m.json"
    CachedEmbedder(_CountingEmbedder(), cache, model_id="model-a", allow_compute=True).embed(["x"])
    # Same text, different model -> a miss (not a silent stale reuse).
    other = CachedEmbedder(None, cache, model_id="model-b", allow_compute=False)
    with pytest.raises(RuntimeError):
        other.embed(["x"])


def test_no_recompute_when_all_hits(tmp_path):
    cache = tmp_path / "m.json"
    CachedEmbedder(_CountingEmbedder(), cache, model_id="m", allow_compute=True).embed(["a", "b"])
    inner = _CountingEmbedder()
    warm = CachedEmbedder(inner, cache, model_id="m", allow_compute=True)
    warm.embed(["a", "b"])  # all cached
    assert inner.embedded == 0  # never touched the inner embedder
