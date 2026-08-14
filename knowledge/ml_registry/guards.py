"""Mutation guards on a registered model's judging contract (R1).

Once a model is registered its comparison contract must not drift mid-campaign --
R1's acceptance condition refuses two kinds of write:

1. any worker-sourced write that touches one of the PROTECTED judging fields.
2. any write moving ``baseline`` from a source other than ``"adjudication"``,
   worker or not -- baseline is more tightly held than the other judging fields.

These are pure guards over a proposed meta patch; the caller (R2's write path)
decides who actually invokes them and with what ``source`` string.
"""

from __future__ import annotations

from knowledge.ml_registry.schema import RegistryValidationError

WORKER_SOURCE = "worker"
ADJUDICATION_SOURCE = "adjudication"

PROTECTED_MODEL_FIELDS: frozenset[str] = frozenset(
    {
        "metric",
        "direction",
        "win_condition",
        "noise_floor",
        "baseline_throughput",
        "diff_size_limit",
    }
)

BASELINE_FIELD = "baseline"


def guard_model_mutation(patch: dict[str, object], *, source: str) -> None:
    """Refuse a worker-sourced patch that touches a protected judging field.

    ``patch`` is the set of meta keys a write intends to change on an ALREADY
    REGISTERED model fact. Raises naming the first protected field the patch
    touches. Non-worker sources are not guarded here -- R1's acceptance only
    requires refusing the worker path.
    """
    if source != WORKER_SOURCE:
        return
    for field in PROTECTED_MODEL_FIELDS:
        if field in patch:
            raise RegistryValidationError(
                f"worker-sourced write may not mutate registered model field {field!r}",
                field=field,
            )


def guard_baseline_move(patch: dict[str, object], *, source: str) -> None:
    """Refuse any write moving ``baseline`` unless it comes from adjudication."""
    if BASELINE_FIELD in patch and source != ADJUDICATION_SOURCE:
        raise RegistryValidationError(
            f"baseline may only be moved by {ADJUDICATION_SOURCE!r}, not {source!r}",
            field=BASELINE_FIELD,
        )
