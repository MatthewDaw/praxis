"""U7: integration — ImageIngestor over the real volta_video fixture dump.

Exercises the full chain (normalize -> hash -> cluster -> caption -> card) on the
actual mounted assets (named cues + the AlchemistAssets dump incl. 2 PSDs), with
a stubbed captioner so no network is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.injestion.injestor_variants.image_injestor import ImageIngestor

FIXTURE_ASSETS = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "cases"
    / "matt"
    / "volta_video"
    / "fixture"
    / "assets"
)


class SpyGraph:
    def __init__(self):
        self.writes = []

    def write(self, content, *, state="proposed"):
        self.writes.append((content, state))

    def read(self, context=None):
        return ""


@pytest.mark.skipif(not FIXTURE_ASSETS.is_dir(), reason="volta fixture assets not present")
def test_ingests_real_dump_with_captions_and_psd_layers():
    calls = {"n": 0}

    def stub_caption(png_bytes):
        calls["n"] += 1
        return f"stub caption {calls['n']}"

    graph = SpyGraph()
    insights = ImageIngestor(graph, captioner=stub_caption).synthesis(str(FIXTURE_ASSETS))

    assert insights, "expected cards from the real dump"
    # every card carries provenance + a usable path reference
    for ins in insights:
        assert ins.category == "asset"
        assert ins.source.startswith("asset:")
        assert "path=assets/" in ins.raw_text
    # exactly one caption per canonical (per cluster)
    assert calls["n"] == len(insights)
    # the two PSDs contribute layer names to at least one card
    assert any("layers:" in ins.raw_text for ins in insights)
    # a PSD path is referenced (Photoshop Files/ mounted under assets/)
    assert any("Photoshop Files/" in ins.raw_text for ins in insights)


@pytest.mark.skipif(not FIXTURE_ASSETS.is_dir(), reason="volta fixture assets not present")
def test_reingest_is_idempotent_on_real_dump():
    seen: set[str] = set()
    first = ImageIngestor(SpyGraph(), seen_hashes=seen).synthesis(str(FIXTURE_ASSETS))
    assert first
    again = ImageIngestor(SpyGraph(), seen_hashes=seen).synthesis(str(FIXTURE_ASSETS))
    assert again == []  # nothing new on a second pass
