"""Lifecycle services that own campaign state transitions."""

from .finalize import FinalizationError, FinalizationRequest, Finalizer
from .registry_adjudication import adjudicate_against_champion
from .campaign_view import CampaignViewError, build_campaign_view
from .completeness import campaign_completeness, campaign_coverage
from .registry_ratchet import consider_rejection, reconcile_registry_space_requeue
from .registry_finalize import (FinalizedModel, RegistryFinalizationError,
                                RegistryFinalizer)

registry_campaign_completeness = campaign_completeness

__all__ = ["CampaignViewError", "FinalizationError", "FinalizationRequest", "Finalizer",
           "campaign_completeness", "campaign_coverage", "registry_campaign_completeness",
           "adjudicate_against_champion", "build_campaign_view", "consider_rejection",
           "reconcile_registry_space_requeue"]
__all__ += ["FinalizedModel", "RegistryFinalizationError", "RegistryFinalizer"]
