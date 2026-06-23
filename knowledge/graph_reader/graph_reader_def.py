"""Pydantic data models for the graph-reader stage.

Centralizes the typed objects retrieval uses, mirroring ``eval_def.py`` for the
harness. The ``GraphReader`` contract lives in ``parent_graph_reader.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReadRequest(BaseModel):
    """A structured retrieval request against the graph.

    ``query`` is the only field the whole-file reader needs; the rest let a
    retrieving reader bound and filter results. Additive with defaults so the
    MVP reader and existing cases are unaffected.
    """

    query: str = ""
    top_k: int | None = 10  # max results to retrieve; None => unbounded (no cap)
    filters: dict = Field(default_factory=dict)  # e.g. {"category": "constraint"}
    scope: str | None = None  # restrict to a scope (service / directory / global)
