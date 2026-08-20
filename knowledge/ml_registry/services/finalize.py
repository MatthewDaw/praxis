"""The sole writer of canonical campaign promotions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from knowledge.ml_registry.completeness import campaign_completeness
from knowledge.ml_registry.contracts import CampaignArtifact, PromotionRecord
from knowledge.ml_registry.lifecycle import active_adoption
from knowledge.ml_registry.schema import TRIAL
from knowledge.ml_registry.storage import ArtifactStore, ArtifactStoreError, FinalizationCommit
from knowledge.ml_registry.write_path import RegistrySpace


class FinalizationError(ValueError):
    """A campaign has not proved every invariant required for promotion."""


CompatibilityRunner = Callable[[str, CampaignArtifact], bool]
Failpoint = Callable[[str], None]


@dataclass(frozen=True)
class FinalizationRequest:
    promotion: PromotionRecord
    stages: tuple[str, ...]
    min_measured: int = 3
    result: Mapping[str, Any] = field(default_factory=dict)
    verdict: Mapping[str, Any] = field(default_factory=dict)
    baseline: Mapping[str, Any] = field(default_factory=dict)
    readiness: Mapping[str, Any] = field(default_factory=dict)


class Finalizer:
    """Verify campaign science and commit its promotion as one immutable event."""

    def __init__(
        self,
        store: ArtifactStore,
        compatibility_runner: CompatibilityRunner,
        *,
        failpoint: Failpoint | None = None,
    ) -> None:
        self.store = store
        self.compatibility_runner = compatibility_runner
        self.failpoint = failpoint or (lambda _checkpoint: None)

    def finalize(self, space: RegistrySpace, request: FinalizationRequest) -> PromotionRecord:
        promotion = request.promotion
        commit = FinalizationCommit(
            promotion=promotion,
            result=request.result,
            verdict=request.verdict,
            baseline=request.baseline,
            readiness=request.readiness,
        )
        existing = self.store.promotion_for_campaign(promotion.campaign_id)
        if existing is not None:
            if existing != promotion:
                raise FinalizationError(
                    f"campaign {promotion.campaign_id!r} already has a different promotion"
                )
            self.verify(space, existing)
            try:
                self.store._commit_finalization(commit)
            except ArtifactStoreError as exc:
                raise FinalizationError(f"finalization retry drifted: {exc}") from exc
            return existing

        complete = campaign_completeness(
            space, promotion.model_id, request.stages,
            min_measured=request.min_measured, require_convergence=False,
        )
        if not complete["done"]:
            kinds = ", ".join(item["kind"] for item in complete["blocking"])
            raise FinalizationError(f"campaign stages are incomplete: {kinds}")
        self.failpoint("after_completeness")

        artifact = self._verify_artifact(promotion)
        self.failpoint("after_artifact_verification")
        self._verify_lineage(space, promotion, artifact)
        self.failpoint("after_lineage_verification")
        self._verify_upstreams(promotion)
        self.failpoint("after_upstream_verification")

        self._verify_compatibility(promotion, artifact)
        self.failpoint("after_compatibility")

        try:
            written = self.store._commit_finalization(commit)
        except ArtifactStoreError as exc:
            raise FinalizationError(f"finalization commit refused: {exc}") from exc
        self.failpoint("after_commit")
        return written

    def verify(self, space: RegistrySpace, promotion: PromotionRecord) -> PromotionRecord:
        canonical = self.store.promotion(promotion.promotion_record_id)
        if canonical != promotion:
            raise FinalizationError("promotion does not match its canonical stored record")
        artifact = self._verify_artifact(canonical)
        self._verify_lineage(space, canonical, artifact)
        self._verify_upstreams(canonical)
        self._verify_compatibility(canonical, artifact)
        return canonical

    def _verify_artifact(self, promotion: PromotionRecord) -> CampaignArtifact:
        try:
            artifact = self.store.verify_artifact(promotion.convergence_artifact_id)
        except ArtifactStoreError as exc:
            raise FinalizationError(f"convergence artifact verification failed: {exc}") from exc
        if artifact.producer_campaign_id != promotion.campaign_id:
            raise FinalizationError("convergence artifact producer does not match campaign")
        if artifact.trial_id != promotion.adopted_trial_id:
            raise FinalizationError("convergence artifact is not bound to the adopted trial")
        if artifact.lineage_id != promotion.lineage_id:
            raise FinalizationError("convergence artifact is not bound to the promoted lineage")
        return artifact

    @staticmethod
    def _verify_lineage(
        space: RegistrySpace, promotion: PromotionRecord, artifact: CampaignArtifact,
    ) -> None:
        adopted = active_adoption(space, promotion.model_id)
        if adopted is None:
            raise FinalizationError("model has no current adopted idea")
        if adopted.meta.get("adopted_trial_id") != promotion.adopted_trial_id:
            raise FinalizationError("promotion is not bound to the current adopted trial")
        trial = space.get(promotion.adopted_trial_id)
        if trial is None or trial.category != TRIAL:
            raise FinalizationError("adopted trial is missing from the registry")
        if trial.meta.get("status") != "succeeded":
            raise FinalizationError("adopted trial is not a succeeded trial")
        trial_lineage = trial.meta.get("lineage_id")
        if trial_lineage is not None and trial_lineage != promotion.lineage_id:
            raise FinalizationError("promotion lineage differs from the adopted trial lineage")
        if artifact.lineage_id != promotion.lineage_id:
            raise FinalizationError("artifact lineage differs from the current adoption")

    def _verify_upstreams(self, promotion: PromotionRecord) -> None:
        for artifact_id in promotion.upstream_artifact_ids:
            try:
                self.store.verify_artifact(artifact_id)
            except ArtifactStoreError as exc:
                raise FinalizationError(
                    f"upstream artifact {artifact_id!r} verification failed: {exc}"
                ) from exc

    def _verify_compatibility(
        self, promotion: PromotionRecord, artifact: CampaignArtifact,
    ) -> None:
        if not promotion.compatibility_passed:
            raise FinalizationError("promotion does not record a passing compatibility test")
        try:
            compatible = self.compatibility_runner(promotion.compatibility_test, artifact)
        except Exception as exc:
            raise FinalizationError(f"compatibility test errored: {exc}") from exc
        if compatible is not True:
            raise FinalizationError(
                f"compatibility test {promotion.compatibility_test!r} did not pass"
            )
