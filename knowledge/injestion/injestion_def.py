"""Pydantic data models for the ingestion stage.

Centralizes the typed objects ingestion produces, mirroring ``eval_def.py`` for
the harness. The ``Ingestor`` contract lives in ``parent_injestor.py``.
"""

from __future__ import annotations

from pydantic import BaseModel


class Insight(BaseModel):
    """A single distilled unit of knowledge.

    One field for now; expand as the distillation gets richer (source,
    confidence, tags, ...).
    """

    raw_text: str
