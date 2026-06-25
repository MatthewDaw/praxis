"""Regression guard for topic-cluster segmentation.

Pins the fix for the under-segmentation bug where UMAP's ``n_neighbors`` favored
global structure so hard that a heterogeneous corpus collapsed into one
mega-cluster — e.g. the 114-fact ``matt_hightouch_agentic_systems`` corpus
(resume + LinkedIn + ACME math curriculum + Gauntlet program) melted into just
two blobs ([94, 20]), so the dashboard's "Topic" label described 94 unrelated
facts at once.

The fixture ``cluster_corpus.npy`` is the frozen, real OpenRouter embedding
matrix (114 x 1536) for that corpus. The test runs the *production* reduce step
(:func:`clustering._reduce`, which carries the ``n_neighbors`` knob) plus HDBSCAN
exactly as :func:`clustering.assign_clusters` does — offline, no network, no
labeling LLM. Both assertions fail at the old ``n_neighbors=15`` (which yields 2
clusters, largest 94/114) and pass at the current 10 (12 clusters, largest 20).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import HDBSCAN

from knowledge.knowledge_graph.clustering import MIN_CLUSTER_SIZE, _reduce

FIXTURE = Path(__file__).parent / "fixtures" / "cluster_corpus.npy"


def _cluster_sizes() -> list[int]:
    """Run the production reduce + HDBSCAN over the frozen corpus; return the
    size of each (non-noise) cluster."""
    vecs = np.load(FIXTURE)
    reduced = _reduce(vecs)
    labels = HDBSCAN(min_cluster_size=MIN_CLUSTER_SIZE, metric="euclidean").fit_predict(reduced)
    return list(Counter(int(c) for c in labels if c != -1).values())


def test_heterogeneous_corpus_does_not_collapse_to_a_blob() -> None:
    sizes = _cluster_sizes()
    n_facts = len(np.load(FIXTURE))
    # More than the two-blob collapse, and no single cluster owns the corpus.
    assert len(sizes) > 2, f"under-segmented into {len(sizes)} clusters: {sorted(sizes, reverse=True)}"
    assert max(sizes) < n_facts / 2, (
        f"largest cluster holds {max(sizes)}/{n_facts} facts — a mega-blob, not a topic"
    )
