"""Pydantic data models for the graph-reader stage.

Centralizes the typed objects retrieval uses, mirroring ``eval_def.py`` for the
harness. The ``GraphReader`` contract lives in ``parent_graph_reader.py``.
"""

from __future__ import annotations

from pydantic import BaseModel


class ReadRequest(BaseModel):
    """A structured retrieval request against the graph.

    One field for now; a Phase-2 retrieving reader can add filters, top-k,
    section selectors, etc.
    """

    query: str = ""
