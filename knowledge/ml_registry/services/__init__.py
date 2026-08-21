"""Lifecycle services that own campaign state transitions."""

from .finalize import FinalizationError, FinalizationRequest, Finalizer
from .registry_adjudication import adjudicate_against_champion

__all__ = ["FinalizationError", "FinalizationRequest", "Finalizer", "adjudicate_against_champion"]
