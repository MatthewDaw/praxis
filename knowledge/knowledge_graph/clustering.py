"""Cluster facts into labeled topics: embed -> reduce -> HDBSCAN -> c-TF-IDF.

The pipeline tags each :class:`Fact` with ``cluster_id`` + ``cluster_label`` so the
dashboard can render collapsible topic super-nodes instead of a flat hairball.
Clustering always uses REAL embeddings (independent of the graph's own embedder),
reduces dimensionality (UMAP, falling back to PCA), runs HDBSCAN, then labels each
cluster with c-TF-IDF (BERTopic-style) top terms. Noise facts (HDBSCAN ``-1``) get
``cluster_id = None``. Cluster ids are NOT stable across runs — re-clustering is
free to reassign (a settled design decision).
"""

from __future__ import annotations

import os
from typing import Sequence

import numpy as np
from joblib import Parallel, delayed

from knowledge.knowledge_graph.knowledge_graph_def import Fact

MIN_CLUSTER_SIZE = 3
EMBED_BATCH = 48


def assign_clusters(facts: Sequence[Fact], *, min_cluster_size: int = MIN_CLUSTER_SIZE) -> int:
    """Tag each fact with ``cluster_id`` + ``cluster_label`` in place; return cluster count.

    Clears clusters (and returns 0) when there are too few facts or no embedding
    API key — so the load path degrades gracefully to an unclustered graph.
    """
    texts = [f.text for f in facts]
    if len(texts) < min_cluster_size or not os.getenv("OPENROUTER_API_KEY"):
        for fact in facts:
            fact.cluster_id = None
            fact.cluster_label = None
        return 0

    reduced = _reduce(_embed(texts))

    from sklearn.cluster import HDBSCAN

    labels = HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean").fit_predict(reduced)
    # LLM-named topics, with c-TF-IDF as the base/fallback so every cluster gets a
    # label even if a naming call fails.
    cluster_labels = _ctfidf_labels(texts, labels)
    try:
        cluster_labels.update(_llm_labels(texts, labels))
    except Exception:
        pass
    for fact, raw in zip(facts, labels):
        cid = int(raw)
        if cid == -1:
            fact.cluster_id = None
            fact.cluster_label = None
        else:
            fact.cluster_id = cid
            fact.cluster_label = cluster_labels.get(cid)
    return len(cluster_labels)


def _embed(texts: list[str]) -> np.ndarray:
    """Real OpenRouter embeddings, fetched in parallel batches, L2-normalized."""
    from knowledge.llm.embedder_variants.openrouter_embedder import OpenRouterEmbedder

    embedder = OpenRouterEmbedder()
    batches = [texts[i : i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
    results = Parallel(n_jobs=len(batches) or 1, prefer="threads")(
        delayed(embedder.embed)(batch) for batch in batches
    )
    vecs = np.array([v for batch in results for v in batch], dtype=np.float64)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.clip(norms, 1e-12, None)


def _reduce(vecs: np.ndarray) -> np.ndarray:
    """Dimensionality reduction so HDBSCAN works (UMAP preferred, PCA fallback)."""
    n = len(vecs)
    if n < 6:
        return vecs
    try:
        import umap  # umap-learn

        reducer = umap.UMAP(
            n_neighbors=min(15, n - 1),
            n_components=min(5, n - 2),
            metric="cosine",
            random_state=42,
        )
        return reducer.fit_transform(vecs)
    except Exception:
        from sklearn.decomposition import PCA

        return PCA(n_components=min(15, n - 1, vecs.shape[1])).fit_transform(vecs)


def _llm_labels(texts: list[str], labels: np.ndarray, sample: int = 8) -> dict[int, str]:
    """Name each cluster with a short LLM-generated topic label (parallel calls)."""
    from knowledge.llm.llm_def import ChatMessage
    from knowledge.llm.llm_variants.openrouter_llm import OpenRouterLlm

    cluster_ids = sorted({int(c) for c in labels if c != -1})
    if not cluster_ids:
        return {}
    members = {
        cid: [t for t, c in zip(texts, labels) if int(c) == cid][:sample] for cid in cluster_ids
    }
    llm = OpenRouterLlm(model="openai/gpt-4o-mini")

    def _label_one(cid: int) -> tuple[int, str | None]:
        bullets = "\n".join(f"- {t[:200]}" for t in members[cid])
        prompt = (
            "These notes were grouped together. Give a concise 2-4 word topic label "
            "naming what they have in common. Reply with ONLY the label — no quotes, "
            "no punctuation, no 'Topic:' prefix.\n\nNotes:\n" + bullets
        )
        try:
            out = llm.complete([ChatMessage(role="user", content=prompt)])
            return cid, " ".join(out.split())[:48] or None
        except Exception:
            return cid, None

    results = Parallel(n_jobs=min(8, len(cluster_ids)), prefer="threads")(
        delayed(_label_one)(cid) for cid in cluster_ids
    )
    return {cid: lab for cid, lab in results if lab}


def _ctfidf_labels(texts: list[str], labels: np.ndarray, top_n: int = 3) -> dict[int, str]:
    """c-TF-IDF: treat each cluster as one document; its top terms become the label."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    cluster_ids = sorted({int(c) for c in labels if c != -1})
    if not cluster_ids:
        return {}
    docs = [" ".join(t for t, c in zip(texts, labels) if int(c) == cid) for cid in cluster_ids]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1, max_features=4000)
    matrix = vec.fit_transform(docs)
    terms = np.array(vec.get_feature_names_out())
    out: dict[int, str] = {}
    for row, cid in enumerate(cluster_ids):
        weights = matrix[row].toarray().ravel()
        top = terms[weights.argsort()[::-1][:top_n]]
        out[cid] = " · ".join(top)
    return out
