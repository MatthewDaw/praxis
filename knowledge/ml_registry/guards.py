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
        # The four fields a SCALED floor is derived from (knowledge.ml_registry.floor's
        # residual-to-ceiling section). Protecting `noise_floor` alone would harden the
        # bar's magnitude and leave its SHAPE writable: a patch moving `metric_ceiling`
        # from 1.0 to 0.62 shrinks every later bar toward the armor without touching a
        # single protected field, which is the same exploit `noise_floor=-99` was.
        "noise_floor_scaling",
        "metric_ceiling",
        "noise_floor_armor",
        "noise_floor_measured_at",
        "baseline_throughput",
        "diff_size_limit",
    }
)

BASELINE_FIELD = "baseline"


def guard_model_mutation(patch: dict[str, object], *, source: str) -> None:
    """Refuse ANY patch that touches a protected judging field on a registered model.

    ``patch`` is the set of meta keys a write intends to change on an ALREADY
    REGISTERED model fact. Raises naming the first protected field the patch touches.

    This is a DENY-ALL, not a denylist of one source. It used to refuse only
    ``source == "worker"``, which made it a no-op for every other string -- and
    ``--source`` is free text at the CLI, so ``--source adjudication`` (or any invented
    value) disabled it outright. That was exploitable end to end: setting
    ``noise_floor=-99`` and ``baseline_throughput=99`` drove a trial whose LEDGER value
    was a clear loss to ``succeeded`` and advanced the model's baseline onto the losing
    commit. Hardening the metric value while leaving the threshold caller-writable
    hardens nothing -- both operands of the comparison have to come from the ledger.

    No legitimate caller needs this: the judging fields are computed from the ledger by
    :func:`~knowledge.ml_registry.floor.register_model_with_baseline` at registration and
    retired by :func:`~knowledge.ml_registry.floor.retire_harness`, which re-registers
    rather than patching. Every :func:`~knowledge.ml_registry.write_path.mutate_model`
    call site in the package patches ``baseline`` only, which is governed separately by
    :func:`guard_baseline_move`.
    """
    for field in PROTECTED_MODEL_FIELDS:
        if field in patch:
            raise RegistryValidationError(
                f"registered model field {field!r} is part of the model's judging contract and "
                f"may not be patched mid-campaign (source {source!r}); re-register the model "
                "instead, so every trial is scored against one declared bar",
                field=field,
            )


def guard_baseline_move(patch: dict[str, object], *, source: str) -> None:
    """Refuse any write moving ``baseline`` unless it comes from adjudication."""
    if BASELINE_FIELD in patch and source != ADJUDICATION_SOURCE:
        raise RegistryValidationError(
            f"baseline may only be moved by {ADJUDICATION_SOURCE!r}, not {source!r}",
            field=BASELINE_FIELD,
        )
