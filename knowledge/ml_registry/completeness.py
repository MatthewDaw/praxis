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
from typing import Any

from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL
from knowledge.ml_registry.staging import stage_coverage
from knowledge.ml_registry.write_path import RegistrySpace

#: Marks the trial that trained the chosen configuration to convergence. A campaign is not
#: finished without one: selecting a winner and never training it is half a job.
CONVERGENCE_FIELD = "convergence_run"


def campaign_completeness(space: RegistrySpace, model_id: str, stages: Sequence[str], *,
                          stage_of=None, min_measured: int = 3,
                          require_convergence: bool = True) -> dict[str, Any]:
    """Whether the campaign may stop, and every reason it may not.

    Returns ``{"done": bool, "blocking": [{kind, stage, detail}, ...]}``. A supervising loop runs
    until ``done`` or until a blocking reason it cannot resolve on its own.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise KeyError(f"model {model_id!r} was never registered")

    ideas = [f for f in space.list_facts(IDEA) if f.meta.get("model_id") == model_id]
    items = [{"id": str(f.meta.get("id") or f.id), "axis": str(f.meta.get("axis") or ""),
              "status": str(f.meta.get("status") or "untried"), "_fact": f.id} for f in ideas]

    answered = {i["id"] for i in items if i["status"] not in ("untried", "voided")}

    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    latest: dict[str, Any] = {}
    for t in trials:
        latest[str(t.meta.get("idea_id"))] = t
    fact_to_tag = {f.id: str(f.meta.get("id") or f.id) for f in ideas}
    # An arm counts as MEASURED only if it produced a verdict from its own result. A voided trial
    # did not: voided means the run was unfair, so the question is still open.
    measured = {fact_to_tag[i] for i, t in latest.items()
                if i in fact_to_tag and str(t.meta.get("status")) not in ("voided",)}

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
                   if i in fact_to_tag and str(t.meta.get("status")) == "voided")
    if rerun:
        blocking.append({
            "kind": "awaiting_rerun", "stage": "",
            "detail": f"voided arms are UNMEASURED, not answered: {', '.join(rerun)}",
        })

    if require_convergence and not model.meta.get(CONVERGENCE_FIELD):
        blocking.append({
            "kind": "no_convergence_run", "stage": "",
            "detail": "every arm so far is a short cross-validation probe tuned to DISCRIMINATE "
                      "between candidates, not a trained model. Train the winning configuration "
                      "to convergence, record it on the model as "
                      f"{CONVERGENCE_FIELD!r}, and only then is the campaign finished. Selecting "
                      "a winner and never training it is half a job.",
        })

    return {"model_id": model_id, "done": not blocking, "blocking": blocking,
            "coverage": coverage}
