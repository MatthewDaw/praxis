"""Praxis knowledge-graph client SDK.

Importable, dependency-light client for using a Praxis backend as a
knowledge-graph from an external repo. See ``praxis_client.client.PraxisClient``.
"""

from __future__ import annotations

from praxis_client.client import PraxisClient, PraxisError

__all__ = ["PraxisClient", "PraxisError"]
