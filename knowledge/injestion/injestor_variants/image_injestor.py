"""Ingest a folder of visual assets into the knowledge graph.

``ImageIngestor`` overrides only :meth:`synthesis` (the variant step): it treats
``raw_input`` as a *folder path*, walks it, normalizes each asset to canonical
PNG, and emits one derived text card per asset as an :class:`Insight`. The
concrete ``ingest`` loop, lifecycle state, and graph-write path are inherited
unchanged — images flow through the same embed + dedup pipeline as text.

Provenance rides on the Insight: ``source="asset:<sha256>:<relpath>"`` and
``category="asset"``. The relative path also appears in the card text (the
``assets/<file>.png`` convention) so retrieval surfaces a usable reference
without changing the frozen ``KnowledgeGraph.write`` contract.

Image adds are *explicit* knowledge, so ``ingest`` defaults to ``state="active"``
(unlike the passive, "proposed" text path).

Variant clustering (perceptual-hash collapse) and VLM captioning layer in via
the injected ``captioner`` and the reconcile/cluster step (see ``hashing``);
with neither, each file becomes its own deterministic card.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from knowledge.injestion.image.cards import build_card
from knowledge.injestion.image.normalize import NormalizedAsset, normalize
from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# Optional VLM caption hook: canonical PNG bytes -> caption text (or None on miss/failure).
Captioner = Callable[[bytes], str | None]


def _content_hash(png_bytes: bytes) -> str:
    return hashlib.sha256(png_bytes).hexdigest()


class WalkedAsset:
    """One normalized asset plus where it sat in the dump."""

    __slots__ = ("relpath", "folder", "asset", "content_hash")

    def __init__(self, relpath: str, folder: str, asset: NormalizedAsset) -> None:
        self.relpath = relpath
        self.folder = folder
        self.asset = asset
        self.content_hash = _content_hash(asset.png_bytes)


def walk_assets(folder: Path) -> list[WalkedAsset]:
    """Normalize every readable asset under ``folder`` (recursive, sorted, total)."""
    walked: list[WalkedAsset] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        asset = normalize(path)
        if asset is None:
            continue  # unsupported/corrupt — already logged by normalize
        relpath = path.relative_to(folder).as_posix()
        taxonomy = path.parent.relative_to(folder).as_posix() or "."
        walked.append(WalkedAsset(relpath, taxonomy, asset))
    return walked


class ImageIngestor(Ingestor):
    """Distill a folder of visual assets into derived text cards."""

    def __init__(self, graph: KnowledgeGraph, *, captioner: Captioner | None = None) -> None:
        super().__init__(graph)
        self.captioner = captioner

    def ingest(self, raw_input: str, *, state: str = "active") -> str:
        """Ingest the folder at ``raw_input``. Image adds are active by default."""
        return super().ingest(raw_input, state=state)

    def synthesis(self, raw_input: str) -> list[Insight]:
        folder = Path(raw_input)
        insights: list[Insight] = []
        for walked in walk_assets(folder):
            caption = self.captioner(walked.asset.png_bytes) if self.captioner else None
            card = build_card(
                asset_path=f"assets/{walked.relpath}",
                folder=walked.folder,
                dims=walked.asset.dims,
                layer_names=walked.asset.layer_names,
                caption=caption,
            )
            insights.append(
                Insight(
                    raw_text=card,
                    source=f"asset:{walked.content_hash}:{walked.relpath}",
                    category="asset",
                )
            )
        return insights
