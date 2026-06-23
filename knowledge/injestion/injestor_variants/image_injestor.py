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

from pathlib import Path
from typing import Callable

from knowledge.injestion.image import hashing
from knowledge.injestion.image.cards import build_card
from knowledge.injestion.image.normalize import NormalizedAsset, normalize
from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# Optional VLM caption hook: canonical PNG bytes -> caption text (or None on miss/failure).
Captioner = Callable[[bytes], str | None]


class WalkedAsset:
    """One normalized asset plus where it sat in the dump."""

    __slots__ = ("relpath", "folder", "asset", "content_hash")

    def __init__(self, relpath: str, folder: str, asset: NormalizedAsset) -> None:
        self.relpath = relpath
        self.folder = folder
        self.asset = asset
        self.content_hash = hashing.content_hash(asset.png_bytes)


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

    def __init__(
        self,
        graph: KnowledgeGraph,
        *,
        captioner: Captioner | None = None,
        seen_hashes: set[str] | None = None,
        threshold: int = hashing.DEFAULT_THRESHOLD,
    ) -> None:
        super().__init__(graph)
        self.captioner = captioner
        # Content hashes already in the graph; assets matching these are skipped
        # (idempotent reconcile). The caller owns and may mutate this set.
        self.seen_hashes = seen_hashes if seen_hashes is not None else set()
        self.threshold = threshold

    def ingest(self, raw_input: str, *, state: str = "active") -> str:
        """Ingest the folder at ``raw_input``. Image adds are active by default."""
        return super().ingest(raw_input, state=state)

    def synthesis(self, raw_input: str) -> list[Insight]:
        folder = Path(raw_input)

        # Reconcile: drop assets whose exact content is already in the graph.
        fresh = [w for w in walk_assets(folder) if w.content_hash not in self.seen_hashes]
        if not fresh:
            return []

        # Exact-dedup: identical bytes collapse to one representative; the rest
        # become variant aliases. Preserves first-seen (sorted) order.
        reps: list[WalkedAsset] = []
        exact_aliases: dict[str, list[str]] = {}
        seen_exact: dict[str, WalkedAsset] = {}
        for w in fresh:
            if w.content_hash in seen_exact:
                exact_aliases[seen_exact[w.content_hash].relpath].append(w.relpath)
            else:
                seen_exact[w.content_hash] = w
                exact_aliases[w.relpath] = []
                reps.append(w)

        # Perceptual cluster the representatives (near-dups: PSD + exported PNG, @2x…).
        phashes = [hashing.perceptual_hash(w.asset.png_bytes) for w in reps]
        clusters = hashing.cluster(phashes, threshold=self.threshold)

        insights: list[Insight] = []
        for members in clusters:
            cluster_reps = [reps[i] for i in members]
            canonical = self._pick_canonical(cluster_reps)
            variants = self._variant_paths(canonical, cluster_reps, exact_aliases)
            caption = self.captioner(canonical.asset.png_bytes) if self.captioner else None
            card = build_card(
                asset_path=f"assets/{canonical.relpath}",
                folder=canonical.folder,
                dims=canonical.asset.dims,
                layer_names=canonical.asset.layer_names,
                caption=caption,
                variants=variants,
            )
            # Record every member's content hash (canonical + near-dup variants),
            # not just the canonical — else variants reappear as fresh on re-ingest
            # and re-cluster among themselves, breaking idempotency.
            for member in cluster_reps:
                self.seen_hashes.add(member.content_hash)
            insights.append(
                Insight(
                    raw_text=card,
                    source=f"asset:{canonical.content_hash}:{canonical.relpath}",
                    category="asset",
                )
            )
        return insights

    @staticmethod
    def _pick_canonical(members: list[WalkedAsset]) -> WalkedAsset:
        """Pick the cluster's canonical: richest signal wins, deterministically.

        Prefer an asset with layer names (a PSD describes itself), then larger
        pixel area, then the lexically-first relpath.
        """
        return min(
            members,
            key=lambda w: (
                not w.asset.layer_names,
                -(w.asset.dims[0] * w.asset.dims[1]),
                w.relpath,
            ),
        )

    @staticmethod
    def _variant_paths(
        canonical: WalkedAsset,
        members: list[WalkedAsset],
        exact_aliases: dict[str, list[str]],
    ) -> list[str]:
        """All non-canonical relpaths in the cluster (near-dup reps + exact aliases)."""
        paths: list[str] = []
        for w in members:
            if w.relpath != canonical.relpath:
                paths.append(w.relpath)
            paths.extend(exact_aliases.get(w.relpath, []))
        return sorted(paths)
