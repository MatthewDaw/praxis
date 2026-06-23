"""Prototype: cluster the matt/applications eval facts to eyeball quality.

Standalone exploration (no persistence, no UI). Loads the application cases'
seed docs, splits them into atomic facts, embeds them once (parallel batches),
then sweeps several HDBSCAN settings IN PARALLEL and prints a comparison with
c-TF-IDF labels per cluster — so we can pick good params before wiring any of
this into the pipeline.

    uv run python -m knowledge.serve.cluster_prototype
"""

from __future__ import annotations

import numpy as np
from dotenv import load_dotenv
from joblib import Parallel, delayed
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer

from knowledge.evals.run import CASES_DIR, load_cases
from knowledge.injestion.injestor_variants.prompt_injestor import PromptIngestor
from knowledge.knowledge_graph.knowledge_graph_variants.in_memory_graph import InMemoryGraph
from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder

MIN_CHARS = 25  # drop header/noise lines too short to be a real fact
EMBED_BATCH = 48
SWEEP = [3, 5, 8, 12]  # HDBSCAN min_cluster_size values to compare


def _facts() -> list[str]:
    """Atomic facts from the application cases' seed docs (passthrough split)."""
    splitter = PromptIngestor(InMemoryGraph())  # no llm => line split
    seen: set[str] = set()
    facts: list[str] = []
    for case in load_cases(CASES_DIR / "matt" / "applications"):
        for source in case.seeded_insight.via_ingestor:
            for insight in splitter.synthesis(source):
                t = " ".join(insight.raw_text.split())
                if len(t) >= MIN_CHARS and t not in seen:
                    seen.add(t)
                    facts.append(t)
    return facts


def _embed(texts: list[str]) -> np.ndarray:
    """Embed all texts via parallel batched OpenRouter calls; L2-normalize."""
    embedder = OpenRouterEmbedder()
    batches = [texts[i : i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
    results = Parallel(n_jobs=len(batches) or 1, prefer="threads")(
        delayed(embedder.embed)(batch) for batch in batches
    )
    vecs = np.array([v for batch in results for v in batch], dtype=np.float64)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.clip(norms, 1e-12, None)  # cosine == euclidean on unit vecs


def _ctfidf_labels(texts: list[str], labels: np.ndarray, top_n: int = 4) -> dict[int, str]:
    """c-TF-IDF (BERTopic-style): treat each cluster as one doc, top terms = label."""
    cluster_ids = sorted({int(c) for c in labels if c != -1})
    if not cluster_ids:
        return {}
    docs = [" ".join(t for t, c in zip(texts, labels) if c == cid) for cid in cluster_ids]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1, max_features=4000)
    matrix = vec.fit_transform(docs)
    terms = np.array(vec.get_feature_names_out())
    out: dict[int, str] = {}
    for row, cid in enumerate(cluster_ids):
        weights = matrix[row].toarray().ravel()
        top = terms[weights.argsort()[::-1][:top_n]]
        out[cid] = ", ".join(top)
    return out


def _run_one(vecs: np.ndarray, texts: list[str], min_cluster_size: int) -> dict:
    model = HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
    labels = model.fit_predict(vecs)
    n_clusters = len({int(c) for c in labels if c != -1})
    noise = int((labels == -1).sum())
    return {
        "min_cluster_size": min_cluster_size,
        "n_clusters": n_clusters,
        "noise": noise,
        "labels": labels,
        "cluster_labels": _ctfidf_labels(texts, labels),
    }


def main() -> None:
    load_dotenv()
    texts = _facts()
    print(f"facts: {len(texts)}  |  embedding (parallel batches)...")
    vecs = _embed(texts)
    print(f"embedded: {vecs.shape}  |  sweeping HDBSCAN {SWEEP} in parallel...\n")

    runs = Parallel(n_jobs=len(SWEEP), prefer="threads")(
        delayed(_run_one)(vecs, texts, mcs) for mcs in SWEEP
    )

    for r in runs:
        print(
            f"=== min_cluster_size={r['min_cluster_size']}  ->  "
            f"{r['n_clusters']} clusters, {r['noise']}/{len(texts)} noise ==="
        )
        sizes = {cid: int((r["labels"] == cid).sum()) for cid in r["cluster_labels"]}
        for cid in sorted(sizes, key=lambda c: -sizes[c]):
            print(f"  [{sizes[cid]:>3}] {r['cluster_labels'][cid]}")
        print()


if __name__ == "__main__":
    main()
