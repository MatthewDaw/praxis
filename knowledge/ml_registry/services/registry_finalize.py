from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge.ml_registry.domain import Alias, CampaignView, ModelVersion
from knowledge.ml_registry.services.completeness import campaign_completeness, campaign_coverage
from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
from knowledge.ml_registry.storage.blobs import BlobError
from knowledge.ml_registry.storage.registry import Registry, RegistryError, _PRODUCTION_CAPABILITY


class RegistryFinalizationError(RegistryError):
    """The current registry state cannot safely become production."""


CompatibilityLoader = Callable[[ModelVersion, Path, str], bool]


@dataclass(frozen=True)
class FinalizedModel:
    model_version: ModelVersion
    production_alias: Alias


LandingCommitWriter = Callable[[FinalizedModel], str]
LandingCommitInverse = Callable[[str], str]


@dataclass(frozen=True)
class ConvergePromotion:
    verdict: str
    finalized: FinalizedModel | None = None
    landing_commit: str | None = None


@dataclass(frozen=True)
class Unpromotion:
    landing_commit: str
    production_alias: Alias


def _decoded(row: Mapping[str, Any], key: str) -> Any:
    value = row[key]
    return json.loads(value) if isinstance(value, str) else value


def _version_view(row: Mapping[str, Any], compat: Mapping[str, Any]) -> ModelVersion:
    return ModelVersion(
        model_id=row["model_id"], version=row["version"], run_id=row["run_id"],
        artifact_id=row["artifact_id"], checksum=row["checksum"],
        family_version=row["family_version"], code_sha=row["code_sha"],
        preprocessing_hash=row["preprocessing_hash"], calibration=_decoded(row, "calibration"),
        thresholds=_decoded(row, "thresholds"), compat_result=dict(compat), status=row["status"],
    )


class RegistryFinalizer:
    """Verify the adopted registry lineage and atomically publish it as production."""

    def __init__(self, registry: Registry, *, compatibility_loader: CompatibilityLoader,
                 min_measured: int = 3) -> None:
        self.registry = registry
        self.compatibility_loader = compatibility_loader
        self.min_measured = min_measured

    def finalize(self, view: CampaignView, *, version: int, reason: str) -> FinalizedModel:
        if not reason.strip():
            raise RegistryFinalizationError("finalization requires a non-empty reason")
        model_id = view.binding.model_id
        version_row, run, artifact, head_sha = self._verify_state(view, version)
        coverage = campaign_coverage(view, self.registry, min_measured=self.min_measured)
        if coverage["done"] is not True:
            raise RegistryFinalizationError("campaign coverage is not ready for finalization")

        compat_view = _version_view(version_row, {"head_sha": head_sha, "passed": True,
                                                   "at": self.registry.clock()})
        blob_path = self.registry.blobs.path(artifact["artifact_id"])
        try:
            compatible = self.compatibility_loader(compat_view, blob_path, head_sha)
        except Exception as exc:
            raise RegistryFinalizationError(f"compatibility load errored: {exc}") from exc
        if compatible is not True:
            raise RegistryFinalizationError("compatibility load did not pass against current HEAD")
        # HEAD may move while the loader runs. Never publish evidence from a stale checkout.
        code_ref = _decoded(run, "code_ref")
        if self.registry._git_head(code_ref["repo"]) != head_sha:
            raise RegistryFinalizationError("repository HEAD moved during compatibility load")

        upstreams = self._upstream_payload(model_id, version)
        payload = {
            "model_id": model_id, "version": version, "run_id": run["run_id"],
            "artifact_id": artifact["artifact_id"], "checksum": version_row["checksum"],
            "head_sha": head_sha, "reason": reason, "upstreams": upstreams,
        }
        try:
            self.registry._finalize_registry_version(payload, capability=_PRODUCTION_CAPABILITY)
        except RegistryError as exc:
            raise RegistryFinalizationError(str(exc)) from exc
        complete = campaign_completeness(view, self.registry, min_measured=self.min_measured)
        if complete["done"] is not True:
            raise RegistryFinalizationError("campaign completeness did not close after finalization")
        return self.verify(view, version=version)

    def verify(self, view: CampaignView, *, version: int) -> FinalizedModel:
        model_id = view.binding.model_id
        version_row, run, artifact, head_sha = self._verify_state(view, version)
        events = [event for event in self.registry.list_events()
                  if event.event_type == "registry_finalized"
                  and event.payload.get("model_id") == model_id and event.payload.get("version") == version]
        alias_row = next((row for row in self.registry.rows("aliases")
                          if row["model_id"] == model_id and row["alias"] == "production"), None)
        if alias_row is None or alias_row["version"] != version:
            raise RegistryFinalizationError("production alias does not match the finalized model version")
        if not events:
            raise RegistryFinalizationError("model version has no canonical finalization event")
        event = events[-1]
        code_ref = _decoded(run, "code_ref")
        if event.payload["head_sha"] != head_sha or self.registry._git_head(code_ref["repo"]) != head_sha:
            raise RegistryFinalizationError("finalization compatibility evidence is stale for current HEAD")
        if event.payload.get("upstreams") != self._upstream_payload(model_id, version):
            raise RegistryFinalizationError("finalization event does not match the current upstream lineage")
        complete = campaign_completeness(view, self.registry, min_measured=self.min_measured)
        if complete["done"] is not True:
            raise RegistryFinalizationError("registry completeness verifier refused finalization")
        candidate = _version_view(version_row, {"head_sha": head_sha, "passed": True, "at": event.at})
        try:
            compatible = self.compatibility_loader(
                candidate, self.registry.blobs.path(artifact["artifact_id"]), head_sha,
            )
        except Exception as exc:
            raise RegistryFinalizationError(f"compatibility load errored: {exc}") from exc
        if compatible is not True or self.registry._git_head(code_ref["repo"]) != head_sha:
            raise RegistryFinalizationError("compatibility load did not pass against current HEAD")
        compat = {"head_sha": head_sha, "passed": True, "at": event.at}
        return FinalizedModel(
            _version_view(version_row, compat),
            Alias(model_id, "production", version, alias_row["set_by"], alias_row["reason"], alias_row["at"]),
        )

    def _verify_state(self, view: CampaignView, version: int
                      ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        model_id = view.binding.model_id
        if (view.experiment.get("experiment_id") != view.binding.experiment_id
                or view.registered_model.get("model_id") != model_id):
            raise RegistryFinalizationError("campaign view does not match its experiment/model binding")
        versions = self.registry.rows("model_versions")
        version_row = next((row for row in versions
                            if row["model_id"] == model_id and row["version"] == version), None)
        champion = next((row for row in self.registry.rows("aliases")
                         if row["model_id"] == model_id and row["alias"] == "champion"), None)
        if version_row is None or champion is None or champion["version"] != version:
            raise RegistryFinalizationError("finalization requires the current champion model version")
        effective = self.registry.effective_model_version(model_id, version)
        if effective["effective_status"] == "superseded":
            raise RegistryFinalizationError("champion model version has superseded lineage")
        if effective["effective_status"] != "active":
            raise RegistryFinalizationError(
                f"champion model version is {effective['effective_status']}"
            )
        run = next((row for row in self.registry.rows("runs")
                    if row["run_id"] == version_row["run_id"]), None)
        if run is None or (run["status"], run["verdict"]) != ("succeeded", "adopted"):
            raise RegistryFinalizationError("champion version is not bound to an adopted succeeded run")
        if run["experiment_id"] != view.binding.experiment_id:
            raise RegistryFinalizationError("champion run does not belong to the bound experiment")
        if not any(candidate["run_id"] == run["run_id"] for candidate in view.runs):
            raise RegistryFinalizationError("champion run is not the campaign view's adopted lineage")
        artifact = next((row for row in self.registry.rows("artifacts")
                         if row["artifact_id"] == version_row["artifact_id"]), None)
        if artifact is None or artifact["run_id"] != run["run_id"]:
            raise RegistryFinalizationError("champion artifact is not bound to its adopted run")
        if version_row["checksum"] != artifact["artifact_id"]:
            raise RegistryFinalizationError("model version checksum differs from its artifact")
        try:
            path = self.registry.blobs.verify(artifact["artifact_id"], artifact["bytes"])
        except BlobError as exc:
            raise RegistryFinalizationError(f"champion blob verification failed: {exc}") from exc
        if hashlib.sha256(path.read_bytes()).hexdigest() != version_row["checksum"]:
            raise RegistryFinalizationError("champion blob checksum differs from its model version")
        self._upstream_payload(model_id, version)
        code_ref = _decoded(run, "code_ref")
        if not isinstance(code_ref, Mapping) or not code_ref.get("repo"):
            raise RegistryFinalizationError("adopted run has no verifiable repository provenance")
        head_sha = self.registry._git_head(code_ref["repo"])
        return version_row, run, artifact, head_sha

    def _upstream_payload(self, model_id: str, version: int) -> list[dict[str, Any]]:
        # A model's prior champion is adoption ancestry, not an external readiness
        # dependency. Requiring it to remain the production alias after this atomic move
        # would make verification of every version after v1 fail by construction.
        links = sorted((row for row in self.registry.rows("lineage")
                        if row["child_model_id"] == model_id and row["child_version"] == version
                        and not (row["parent_model_id"] == model_id
                                 and row["kind"] == "derived_from")),
                       key=lambda row: (row["parent_model_id"], row["parent_version"], row["kind"]))
        versions = self.registry.rows("model_versions")
        aliases = self.registry.rows("aliases")
        result: list[dict[str, Any]] = []
        for link in links:
            parent = next((row for row in versions if row["model_id"] == link["parent_model_id"]
                           and row["version"] == link["parent_version"]), None)
            if parent is None:
                raise RegistryFinalizationError("lineage references a missing upstream model version")
            production = next((row for row in aliases if row["model_id"] == link["parent_model_id"]
                               and row["alias"] == "production"), None)
            if production is None or production["version"] != link["parent_version"]:
                raise RegistryFinalizationError("lineage upstream is not the current production version")
            artifact = next((row for row in self.registry.rows("artifacts")
                             if row["artifact_id"] == parent["artifact_id"]), None)
            if artifact is None or parent["checksum"] != artifact["artifact_id"]:
                raise RegistryFinalizationError("lineage upstream artifact/checksum is invalid")
            try:
                self.registry.blobs.verify(artifact["artifact_id"], artifact["bytes"])
            except BlobError as exc:
                raise RegistryFinalizationError(f"lineage upstream blob verification failed: {exc}") from exc
            effective = self.registry.effective_model_version(parent["model_id"], parent["version"])
            parent_run = next((row for row in self.registry.rows("runs")
                               if row["run_id"] == parent["run_id"]), None)
            parent_code_ref = _decoded(parent_run, "code_ref") if parent_run is not None else None
            compat = effective["effective_compat_result"]
            if (effective["effective_status"] != "active" or compat.get("passed") is not True
                    or not isinstance(parent_code_ref, Mapping) or not parent_code_ref.get("repo")
                    or compat.get("head_sha") != self.registry._git_head(parent_code_ref["repo"])):
                raise RegistryFinalizationError(
                    "lineage upstream is not active and compatible with its current HEAD"
                )
            result.append({"model_id": parent["model_id"], "version": parent["version"],
                           "artifact_id": parent["artifact_id"], "checksum": parent["checksum"],
                           "kind": link["kind"]})
        return result


class ConvergePromoter:
    """Adjudicate a full-length run, finalize its aliases, then call the landing writer."""

    def __init__(
        self,
        registry: Registry,
        *,
        compatibility_loader: CompatibilityLoader,
        landing_commit: LandingCommitWriter,
        landing_commit_inverse: LandingCommitInverse | None = None,
        min_measured: int = 3,
    ) -> None:
        self.registry = registry
        self.finalizer = RegistryFinalizer(
            registry, compatibility_loader=compatibility_loader, min_measured=min_measured,
        )
        self.landing_commit = landing_commit
        self.landing_commit_inverse = landing_commit_inverse

    def run(
        self,
        view: CampaignView,
        *,
        run_id: str,
        version: int,
        reason: str,
        promotion: Mapping[str, Any] | None = None,
        consuming_sports: tuple[str, ...] = (),
        measured_sports: tuple[str, ...] = (),
    ) -> ConvergePromotion:
        if view.registered_model["sport_scope"] == "shared":
            missing = sorted(set(consuming_sports) - set(measured_sports))
            if missing:
                raise RegistryFinalizationError(
                    "shared-family promotion is missing consumer measurements: " + ", ".join(missing)
                )
        aliases = {
            row["alias"]: dict(row)
            for row in self.registry.rows("aliases")
            if row["model_id"] == view.binding.model_id
            and row["alias"] in {"champion", "production"}
        }
        verdict = adjudicate_against_champion(
            self.registry,
            run_id=run_id,
            model_id=view.binding.model_id,
            reason=reason,
            promotion=promotion,
        )
        if verdict != "adopted":
            return ConvergePromotion(verdict)
        current_runs = self.registry.rows("runs")
        refreshed = CampaignView(
            view.binding,
            view.experiment,
            view.registered_model,
            view.model_fact,
            tuple(replace(idea, runs=tuple(
                row for row in current_runs if row["idea_id"] == idea.fact_id
            )) for idea in view.ideas),
        )
        try:
            finalized = self.finalizer.finalize(refreshed, version=version, reason=reason)
            commit = self.landing_commit(finalized).strip()
            if not commit:
                raise RegistryFinalizationError("landing writer returned no commit")
            self.registry._record_landed_promotion(
                model_id=view.binding.model_id,
                version=version,
                landing_commit=commit,
                aliases=aliases,
                capability=_PRODUCTION_CAPABILITY,
            )
        except Exception as exc:
            try:
                self.registry._rollback_failed_landing(
                    model_id=view.binding.model_id,
                    failed_version=version,
                    aliases=aliases,
                    capability=_PRODUCTION_CAPABILITY,
                )
            except Exception as rollback_exc:
                raise RegistryFinalizationError(
                    f"promotion failed ({exc}); alias rollback also failed: {rollback_exc}"
                ) from rollback_exc
            raise RegistryFinalizationError(f"promotion failed: {exc}") from exc
        return ConvergePromotion(verdict, finalized, commit)

    def unpromote(self, *, model_id: str, reason: str) -> Unpromotion:
        """Undo one landed promotion, compensating aliases if the landing inverse refuses."""
        if self.landing_commit_inverse is None:
            raise RegistryFinalizationError("unpromote requires a landing-commit inverse")
        if not reason.strip():
            raise RegistryFinalizationError("unpromote requires a non-empty reason")
        lifecycle = [
            event for event in self.registry.list_events()
            if event.event_type in {"campaign_landed", "campaign_unpromoted"}
            and event.payload.get("model_id") == model_id
        ]
        if not lifecycle or lifecycle[-1].event_type != "campaign_landed":
            raise RegistryFinalizationError(f"model {model_id!r} has no landed promotion to undo")
        landed = lifecycle[-1].payload
        promoted_aliases = {
            row["alias"]: dict(row)
            for row in self.registry.rows("aliases")
            if row["model_id"] == model_id and row["alias"] in {"champion", "production"}
        }
        self.registry._rollback_failed_landing(
            model_id=model_id,
            failed_version=int(landed["version"]),
            aliases=landed["aliases"],
            capability=_PRODUCTION_CAPABILITY,
        )
        try:
            revert_commit = self.landing_commit_inverse(str(landed["landing_commit"])).strip()
            if not revert_commit:
                raise RegistryFinalizationError("landing inverse returned no commit")
        except Exception as exc:
            self.registry._restore_unpromotion(
                model_id=model_id,
                aliases=promoted_aliases,
                capability=_PRODUCTION_CAPABILITY,
            )
            raise RegistryFinalizationError(f"unpromote failed: {exc}") from exc
        self.registry._record_campaign_unpromoted(
            model_id=model_id,
            landing_commit=str(landed["landing_commit"]),
            revert_commit=revert_commit,
            capability=_PRODUCTION_CAPABILITY,
        )
        production = next(
            (row for row in self.registry.rows("aliases")
             if row["model_id"] == model_id and row["alias"] == "production"),
            None,
        )
        if production is None:
            raise RegistryFinalizationError("unpromote restored no production alias")
        return Unpromotion(revert_commit, Alias(
            production["model_id"], production["alias"], production["version"],
            production["set_by"], production["reason"], production["at"],
        ))


class RegistryFinalizeService:
    """The sole §5.11 writer of the ``production`` alias."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def move_production(self, *, model_id: str, version: int, reason: str) -> None:
        effective = self.registry.effective_model_version(model_id, version)
        if effective["effective_status"] != "active":
            raise ValueError("finalize refuses an incompatible model version")
        self.registry._set_alias(
            model_id=model_id, alias="production", version=version, set_by="finalize",
            reason=reason, capability=_PRODUCTION_CAPABILITY,
        )
