"""Regression: the embedding-cache refresh records image-asset card vectors.

Image-asset (``via_image_ingestor``) cards land "active" through the same
``CachedEmbedder`` as the seeded text, so the committed cache must contain their
card-text vectors — else seeding a ``cached`` image case is a loud miss at
runtime (the cards never reach the graph). This guards the wiring that drives
``_seed_image_assets`` during a refresh.
"""

from __future__ import annotations

from knowledge.evals import embed_cache
from knowledge.evals.eval_def import DeterministicCheckRef, EvalCase, SeededInsight
from knowledge.llm.parent_embedder import Embedder


def _fake_build_factory(recorded: list[str]):
    """A patched _build_trio_for whose embedder records every embedded text (no network)."""

    class RecordingEmbedder(Embedder):
        def embed(self, texts):
            recorded.extend(texts)
            return [[0.0, 0.0] for _ in texts]

    def _patched_build(case, llm=None):
        from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
        from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
        from knowledge.wiring import build_trio

        graph = VectorGraph(embedder=RecordingEmbedder(), policy=[Redactor(), Deduper()])
        return build_trio(substrate="vector", graph=graph, embedder=RecordingEmbedder())

    return _patched_build


def _cached_case(cid: str, fact: str) -> EvalCase:
    return EvalCase.model_validate(
        dict(
            id=cid, component="graph_reader", substrate="vector", embedder="cached",
            reader="retrieving", seed_prompt=f"query {cid}",
            seeded_insight=SeededInsight(direct_to_graph=[fact]),
            deterministic_checks=[DeterministicCheckRef(name="x", ref="mod:fn")],
        )
    )


def test_add_is_incremental_and_scoped(monkeypatch, tmp_path):
    # Two cached cases; --add must record ONLY the named one and must NOT wipe the cache.
    case_a = _cached_case("case_a", "alpha fact for case_a")
    case_b = _cached_case("case_b", "bravo fact for case_b")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    cache_dir = tmp_path / "embeddings"
    cache_dir.mkdir()
    monkeypatch.setattr(embed_cache, "EMBED_CACHE_DIR", cache_dir)
    monkeypatch.setattr(embed_cache, "load_cases", lambda: [case_a, case_b])

    recorded: list[str] = []
    monkeypatch.setattr(embed_cache, "_build_trio_for", _fake_build_factory(recorded))

    # A pre-existing cache file — incremental add must leave it in place (refresh would unlink).
    cache_file = cache_dir / f"{embed_cache._slug(embed_cache._embed_model())}.json"
    cache_file.write_text('{"sentinel": "x"}', encoding="utf-8")

    # Unknown / non-cached id -> error, nothing recorded.
    assert embed_cache.add(["nope"]) == 1
    assert recorded == []

    # Named case -> recorded, scoped to that case only, cache file untouched (not unlinked).
    assert embed_cache.add(["case_b"]) == 0
    assert cache_file.exists()  # incremental: not deleted
    assert any("bravo" in t for t in recorded)
    assert not any("alpha" in t for t in recorded)


def _png(path, color=(10, 20, 30), size=(8, 8)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_refresh_records_image_asset_card_vectors(monkeypatch, tmp_path):
    case_dir = tmp_path / "case"
    _png(case_dir / "fixture" / "assets" / "mascot.png")
    _png(case_dir / "fixture" / "assets" / "bg.png", color=(200, 50, 50))
    case = EvalCase.model_validate(
        dict(
            id="img_cached",
            component="ingestion",
            substrate="vector",
            embedder="cached",
            source_dir=str(case_dir),
            seed_prompt="",
            seeded_insight=SeededInsight(via_image_ingestor=["assets"]),
            deterministic_checks=[DeterministicCheckRef(name="x", ref="mod:fn")],
        )
    )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(embed_cache, "EMBED_CACHE_DIR", tmp_path / "embeddings")
    monkeypatch.setattr(embed_cache, "load_cases", lambda: [case])

    recorded: list[str] = []

    from knowledge.llm.parent_embedder import Embedder

    class RecordingEmbedder(Embedder):
        def embed(self, texts):
            recorded.extend(texts)
            return [[0.0, 0.0] for _ in texts]

    # Drive the real refresh wiring with a fake live embedder so we observe exactly
    # which texts get embedded (recorded) without touching the network.
    def _patched_build(case, llm=None):
        from knowledge.knowledge_graph.knowledge_graph_variants.vector_graph import VectorGraph
        from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
        from knowledge.wiring import build_trio

        graph = VectorGraph(embedder=RecordingEmbedder(), policy=[Redactor(), Deduper()])
        return build_trio(substrate="vector", graph=graph, embedder=RecordingEmbedder())

    monkeypatch.setattr(embed_cache, "_build_trio_for", _patched_build)

    rc = embed_cache.refresh()
    assert rc == 0
    assert any("path=assets/" in t for t in recorded), (
        "refresh must embed image-asset card texts so cached image cases replay offline"
    )
