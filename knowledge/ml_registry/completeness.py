"""When is a campaign actually DONE?

The loop had no answer, so it stopped whenever its immediate queue emptied and looked finished.
Measured on the first real campaign: it ran a partial architecture search and halted. Augmentation,
training, tuning and capacity were never reached, no final train-to-convergence existed as a
concept at all, and every stage transition required a human to relaunch. Nothing was in an error
state -- each invocation exited 0 having done what it was asked, and "what it was asked" was one
stage's worth of arms.

That is the failure this module names: **an empty queue is not a finished campaign.** A campaign
is finished when every declared phase is CLOSED, every phase was actually POPULATED, and the
winning configuration has been trained to convergence and recorded.

Each check answers a different way of being unfinished, and they are genuinely distinct:

- `stage_never_authored` -- the phase exists in the plan and has ZERO registered arms. Silent
  today: a stage with no arms is trivially "all answered", so it closes instantly and the campaign
  sails past a question nobody ever asked. `tuning` and `capacity` were both empty on the first
  campaign and neither was mentioned anywhere.
- `stage_open` -- arms remain unanswered.
- `stage_thin` -- closed, but on too few arms that actually RAN (see `staging.stage_coverage`).
- `awaiting_rerun` -- voided arms are unmeasured, not answered.
- `no_convergence_run` -- every arm so far is a 4-seed cross-validation probe tuned for
  DISCRIMINATION between candidates, not a trained model. A campaign that never trains its winner
  to convergence has selected a configuration and produced nothing to deploy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from knowledge.ml_registry.contracts import CampaignArtifact, PromotionRecord
from knowledge.ml_registry.domain.status import answers_question, retryable
from knowledge.ml_registry.lifecycle import active_adoption
from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL
from knowledge.ml_registry.report import idea_verdicts
from knowledge.ml_registry.staging import stage_coverage, unreachable
from knowledge.ml_registry.write_path import RegistrySpace

#: Marks the trial that trained the chosen configuration to convergence. A campaign is not
#: finished without one: selecting a winner and never training it is half a job.
CONVERGENCE_FIELD = "convergence_run"


class PromotionSource(Protocol):
    def promotion_for_model(self, model_id: str) -> PromotionRecord | None: ...

    def verify_artifact(self, artifact_id: str) -> CampaignArtifact: ...


def campaign_completeness(space: RegistrySpace, model_id: str, stages: Sequence[str], *,
                          stage_of=None, min_measured: int = 3,
                          require_convergence: bool = True,
                          promotion_source: PromotionSource | None = None) -> dict[str, Any]:
    """Whether the campaign may stop, and every reason it may not.

    Returns ``{"done": bool, "blocking": [{kind, stage, detail}, ...]}``. A supervising loop runs
    until ``done`` or until a blocking reason it cannot resolve on its own.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise KeyError(f"model {model_id!r} was never registered")

    ideas = [f for f in space.list_facts(IDEA) if f.meta.get("model_id") == model_id]
    # `stage` and `depends_on` must both be copied. default_stage_of reads
    # stage then axis; unreachable() reads depends_on. Dropping either makes a
    # guard that is present, wired, and inert -- the same defect twice.
    items = [{"id": str(f.meta.get("id") or f.id),
              "axis": str(f.meta.get("axis") or ""),
              "stage": str(f.meta.get("stage") or ""),
              "status": str(f.meta.get("status") or "untried"),
              "depends_on": list(f.meta.get("depends_on") or []),
              "_fact": f.id} for f in ideas]

    # Answered comes from the latest TRIAL, not from idea.meta.status. resolve-verdict writes the
    # verdict onto the trial and does not stamp the idea, so asking the idea reports every
    # freshly-adjudicated arm as unanswered -- which is how a campaign loop re-ran the same arms
    # indefinitely, each iteration looking like honest new work.
    verdicts = idea_verdicts(space, model_id)
    answered = set(verdicts)
    answered |= {i["id"] for i in items if i["status"] not in ("untried", "voided", "None")}
    adopted = {tag for tag, st in verdicts.items() if st == "succeeded"}
    # Same union next_queue applies: a dep that can never be adopted must not
    # hold its stage open and block campaign-complete.
    answered |= unreachable(items, answered, adopted)

    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    latest: dict[str, Any] = {}
    for t in trials:
        latest[str(t.meta.get("idea_id"))] = t
    fact_to_tag = {f.id: str(f.meta.get("id") or f.id) for f in ideas}
    # An arm counts as MEASURED only if it produced a verdict from its own result. A voided trial
    # did not: voided means the run was unfair, so the question is still open.
    measured = {fact_to_tag[i] for i, t in latest.items()
                if i in fact_to_tag and _fair_candidate_measurement(t.meta)}

    kw = {"stage_of": stage_of} if stage_of else {}
    coverage = stage_coverage(items, stages, measured_ids=measured, answered_ids=answered,
                              min_measured=min_measured, **kw)

    blocking: list[dict[str, str]] = []
    for c in coverage:
        if c["total"] == 0:
            blocking.append({
                "kind": "stage_never_authored", "stage": c["stage"],
                "detail": f"phase {c['stage']!r} has ZERO registered arms. An empty stage is "
                          f"trivially 'all answered', so it closes instantly and the campaign "
                          f"sails past a question nobody ever asked. Author arms for it or drop "
                          f"it from the plan deliberately.",
            })
        elif not c["closed"]:
            blocking.append({
                "kind": "stage_open", "stage": c["stage"],
                "detail": f"{c['total'] - c['measured']} arm(s) still unanswered in "
                          f"{c['stage']!r}.",
            })
        elif c["thin"]:
            blocking.append({
                "kind": "stage_thin", "stage": c["stage"],
                "detail": f"{c['stage']!r} closed on {c['measured']} arm(s) that actually ran, "
                          f"below the floor of {min_measured}. Report it as thin rather than "
                          f"settled, and name the model families not tried.",
            })

    rerun = sorted(fact_to_tag[i] for i, t in latest.items()
                   if i in fact_to_tag and _retryable(t.meta.get("status")))
    if rerun:
        blocking.append({
            "kind": "awaiting_rerun", "stage": "",
            "detail": f"retryable arms are UNMEASURED, not answered: {', '.join(rerun)}",
        })

    if require_convergence:
        convergence_blocker = _convergence_blocker(space, model_id, model.meta, promotion_source)
        if convergence_blocker is not None:
            blocking.append(convergence_blocker)

    return {"model_id": model_id, "done": not blocking, "blocking": blocking,
            "coverage": coverage}


def _fair_candidate_measurement(meta: dict[str, Any]) -> bool:
    try:
        fair = answers_question(str(meta.get("status") or ""))
    except ValueError:
        return False
    if not fair or meta.get("incumbent_remeasurement") is True:
        return False
    resolved = meta.get("resolved_configuration")
    incumbent = meta.get("incumbent_configuration")
    return resolved is None or incumbent is None or resolved != incumbent


def _retryable(value: object) -> bool:
    try:
        return retryable(str(value or ""))
    except ValueError:
        return False


def _convergence_blocker(
    space: RegistrySpace,
    model_id: str,
    model_meta: dict[str, Any],
    promotion_source: PromotionSource | None,
) -> dict[str, str] | None:
    marker = model_meta.get(CONVERGENCE_FIELD)
    if promotion_source is None:
        if not marker:
            return _blocker(
                "no_convergence_run",
                "every arm so far is a short cross-validation probe tuned to DISCRIMINATE "
                "between candidates, not a trained model. Finalize the winning configuration "
                "into a canonical PromotionRecord before declaring the campaign complete.",
            )
        if isinstance(marker, dict) and marker.get("stale") is True:
            return _blocker("stale_convergence", "convergence metadata names a stale artifact")
        if isinstance(marker, dict) and marker.get("lineage_id"):
            return _blocker(
                "wrong_lineage_convergence",
                "convergence metadata is not a canonical promotion bound to the current adoption",
            )
        return _blocker(
            "invalid_convergence",
            "truthy convergence metadata is not a canonical PromotionRecord lookup",
        )
    try:
        promotion = promotion_source.promotion_for_model(model_id)
    except Exception as exc:
        return _blocker("invalid_convergence", f"canonical promotion lookup failed: {exc}")
    if promotion is None:
        return _blocker("no_convergence_run", "no canonical PromotionRecord exists for this model")
    if promotion.model_id != model_id or not promotion.compatibility_passed:
        return _blocker("invalid_convergence", "canonical PromotionRecord is malformed or incompatible")
    adopted = active_adoption(space, model_id)
    if adopted is None or adopted.meta.get("adopted_trial_id") != promotion.adopted_trial_id:
        return _blocker(
            "wrong_lineage_convergence",
            "canonical promotion is not bound to the current adopted trial",
        )
    try:
        artifact = promotion_source.verify_artifact(promotion.convergence_artifact_id)
    except Exception as exc:
        return _blocker("stale_convergence", f"convergence artifact is missing or tampered: {exc}")
    if artifact.trial_id != promotion.adopted_trial_id or artifact.lineage_id != promotion.lineage_id:
        return _blocker(
            "wrong_lineage_convergence",
            "convergence artifact is not bound to the current adopted lineage",
        )
    return None


def _blocker(kind: str, detail: str) -> dict[str, str]:
    return {"kind": kind, "stage": "", "detail": detail}
