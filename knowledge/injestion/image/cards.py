"""Deterministic asset-card text — the derived knowledge for one visual asset.

A card is a single pipe-delimited line built entirely from cheap, deterministic
signals: the asset's name, its folder taxonomy in the dump, dimensions, any PSD
layer names, and (optionally, added later by the captioner) a VLM caption. The
trailing ``path=`` token mirrors the convention the agent already consumes
(``assets/<file>.png``) so retrieval surfaces a usable reference.

Kept pure and string-only so it flows through the existing text-embed + dedup
path with no new graph mechanism.
"""

from __future__ import annotations

from pathlib import PurePosixPath


def build_card(
    *,
    asset_path: str,
    folder: str | None = None,
    dims: tuple[int, int] | None = None,
    layer_names: list[str] | None = None,
    caption: str | None = None,
    variants: list[str] | None = None,
) -> str:
    """Compose the card line for one asset (or one variant cluster).

    ``asset_path`` is the reference the agent uses (e.g. ``assets/mascot.png``).
    Empty/absent fields are omitted so cards stay tight and contradiction-free.
    """
    parts = [f"asset: {PurePosixPath(asset_path).stem}"]
    if folder and folder != ".":
        parts.append(f"folder: {folder}")
    if dims is not None:
        parts.append(f"dims: {dims[0]}x{dims[1]}")
    if layer_names:
        parts.append(f"layers: {', '.join(layer_names)}")
    if caption:
        parts.append(f"caption: {caption}")
    if variants:
        parts.append(f"variants: {', '.join(variants)}")
    parts.append(f"path={asset_path}")
    return " | ".join(parts)
