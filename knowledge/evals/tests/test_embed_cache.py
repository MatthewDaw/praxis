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
