"""Topic clustering for the knowledge graph.

Reduces fact embeddings with UMAP, then clusters the reduced vectors with HDBSCAN
to assign each fact a topic. The dashboard renders one "Topic" label per cluster.
"""

from __future__ import annotations

import numpy as np
from sklearn.cluster import HDBSCAN

MIN_CLUSTER_SIZE = 3


def _reduce(vecs: np.ndarray) -> np.ndarray:
    """Reduce high-dimensional embeddings before clustering."""
    n = len(vecs)
    if n < 4:
        return vecs
    import umap  # umap-learn

    reducer = umap.UMAP(
        n_neighbors=min(15, n - 1),
        n_components=min(5, n - 2),
        metric="cosine",
        random_state=42,
    )
    return reducer.fit_transform(vecs)


def assign_clusters(vecs: np.ndarray) -> list[int]:
    """Assign each fact a cluster label (-1 = noise)."""
    reduced = _reduce(vecs)
    labels = HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, metric="euclidean").fit_predict(reduced)
    return [int(c) for c in labels]
