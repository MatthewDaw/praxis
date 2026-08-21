"""Fact-schema validation for the af-ml-research registry (R1).

Defines the three fact categories the registry write path (R2+) persists as Praxis
facts -- "model", "idea", "trial" -- and the meta keys each MUST carry. Presence-only
validation; cross-field business rules (origin enum, idea/trial cross-references,
external-ledger checks, ...) belong to later tickets that build on this schema.
"""

from __future__ import annotations

from knowledge.ml_registry.domain.status import (TERMINAL_TRIAL_STATUSES as TERMINAL_TRIAL_STATUSES,
                                                 TrialStatus)

#: A trial in one of these statuses is FINISHED: it has a verdict, or was deliberately abandoned.
#: Anything else means the trial is still in flight, and an idea may have only one of those at a
#: time -- see `write_path.register_trial`. Defined here, in the leaf module, because `lifecycle`
#: imports `write_path` and so `write_path` cannot import `lifecycle` back.
#:
#: Note these are the TRIAL's status vocabulary, which is deliberately not the verdict vocabulary:
#: `adjudicate_verdict` RETURNS "adopted"/"parked"/"rejected"/"voided" while writing
#: a six-value execution status onto the trial and an orthogonal verdict tag beside it.
#: Listing the verdict names as statuses instead
#: would leave every adjudicated trial looking in flight forever.
#:
#: `voided` is terminal on purpose: a voided run is meant to be RE-RUN, so it must not keep
#: blocking its idea.
#:
#: "complete" and "running" are NOT terminal, and that is the point. "complete" means the training
#: run finished and is awaiting adjudication -- a trial in that state is exactly the one a
#: duplicate dispatch would race.
STATUS_SUPERSEDED = TrialStatus.SUPERSEDED.value

MODEL = "model"
IDEA = "idea"
TRIAL = "trial"

REGISTRY_CATEGORIES: tuple[str, ...] = (MODEL, IDEA, TRIAL)

# The trial status meaning "this run is unreliable on its face, re-run it" -- the single source
# of truth every module that needs to exclude a voided trial (supervisor's max_trials budget,
# insight's cross-model trial counts, ...) imports rather than re-literals.
TRIAL_STATUS_VOIDED = TrialStatus.VOIDED.value

# The "judging fields" a registered model is compared against, plus its baseline --
# R1's acceptance condition and R11's registration contract agree on this set.
REQUIRED_META_KEYS: dict[str, tuple[str, ...]] = {
    MODEL: (
        "metric",
        "direction",
        "win_condition",
        "baseline",
        "noise_floor",
        "baseline_throughput",
        "diff_size_limit",
    ),
    IDEA: (
        "model_id",
        "origin",
        "axis",
        "description",
    ),
    TRIAL: (
        "model_id",
        "idea_id",
        "commit",
        "status",
    ),
}


class RegistryValidationError(ValueError):
    """A write to the registry fails its category's schema or a mutation guard.

    ``field`` names the single offending meta key so a caller (and a human) can act
    on the refusal without re-deriving it from prose.
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def validate_fact(category: str, meta: dict[str, object]) -> None:
    """Raise :class:`RegistryValidationError` naming the first missing required meta key.

    ``category`` must be one of :data:`REGISTRY_CATEGORIES`; an unknown category is
    itself refused rather than silently accepted as schema-free.
    """
    if category not in REQUIRED_META_KEYS:
        raise RegistryValidationError(
            f"unknown registry fact category {category!r}; expected one of {REGISTRY_CATEGORIES}",
            field="category",
        )
    for key in REQUIRED_META_KEYS[category]:
        if meta.get(key) in (None, ""):
            raise RegistryValidationError(
                f"{category} fact is missing required meta key {key!r}",
                field=key,
            )
