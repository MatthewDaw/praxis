"""Noise-floor registration, single-trial adjudication, and harness retirement (R12).

Builds on R11's write path (:func:`knowledge.ml_registry.write_path.register_model`) and
R3's idea lifecycle (:func:`knowledge.ml_registry.lifecycle.invalidate_adoption`). A
model's ``noise_floor`` and ``baseline_throughput`` are not caller-asserted numbers: they
are RECOMPUTED here from 4 baseline runs named by commit in ``meta["baseline_runs"]``,
read off the external results ledger, and a registration whose stored values disagree
with that recomputation is refused naming the disagreeing field. ``noise_floor`` is the
sample standard deviation of those 4 runs; ``baseline_throughput`` is their mean.

Once a floor is registered, a candidate idea is adjudicated on a SINGLE trial --
:func:`adjudicate_trial` never asks for a confirmation run, however close the margin.

A model's "harness" (:data:`HARNESS_FIELDS` -- eval size, precision, hardware) is held
more tightly than an ordinary judging field: :func:`retire_harness` refuses to let a
change to any of them pass quietly. Mutating an already-RECORDED harness field retires
both derived values, reverts whatever adoption is currently scored against them (with
:func:`~knowledge.ml_registry.lifecycle.invalidate_adoption`'s re-queue side effects),
clears the ratchet counter, and stalls the campaign (:data:`STALLED`) rather than
failing it -- the model simply will not adjudicate again until it is re-registered
through :func:`register_model_with_baseline` with a fresh 4-run procedure run at the
baseline commit left standing after that reversion.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

from knowledge.ml_registry.lifecycle import active_adoption, invalidate_adoption
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import Fact, RegistrySpace, register_model

BASELINE_RUNS_FIELD = "baseline_runs"
NOISE_FLOOR_FIELD = "noise_floor"
BASELINE_THROUGHPUT_FIELD = "baseline_throughput"
RATCHET_COUNT_FIELD = "ratchet_count"
# R10: the distinct idea ids behind the model's current consecutive-rejection streak --
# reset alongside RATCHET_COUNT_FIELD wherever the ratchet itself resets (here, on a
# harness mutation; in knowledge.ml_registry.verdict, on an adoption or an invalidation).
REJECTION_STREAK_FIELD = "rejection_streak_ideas"
CAMPAIGN_STATUS_FIELD = "campaign_status"

ACTIVE = "active"
STALLED = "stalled_pending_baseline"

REQUIRED_BASELINE_RUN_COUNT = 4
FLOOR_AGREEMENT_TOLERANCE = 1e-9

# The judging fields R12 holds even more tightly than R1's PROTECTED_MODEL_FIELDS: a
# change to any of them retires the noise floor and baseline throughput derived under
# the old harness, because a result measured under a different eval size, precision or
# hardware is not comparable to the runs that produced them.
HARNESS_FIELDS: frozenset[str] = frozenset({"eval_size", "precision", "hardware"})


def load_ledger_values(path: Path) -> dict[str, float]:
    """commit -> val_bpb for every row of the external results ledger (``results.tsv``),
    the same file :func:`knowledge.ml_registry.write_path.load_ledger_commits` reads."""
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            next(reader)  # header
        except StopIteration:
            return {}
        return {row[0].strip(): float(row[1]) for row in reader if row and row[0].strip()}


def compute_noise_floor(values: list[float]) -> tuple[float, float]:
    """``(noise_floor, baseline_throughput)`` = ``(sample stdev, mean)`` of exactly 4 runs."""
    if len(values) != REQUIRED_BASELINE_RUN_COUNT:
        raise RegistryValidationError(
            f"noise floor requires exactly {REQUIRED_BASELINE_RUN_COUNT} baseline runs, got {len(values)}",
            field=BASELINE_RUNS_FIELD,
        )
    return statistics.stdev(values), statistics.mean(values)


def register_model_with_baseline(
    space: RegistrySpace,
    meta: dict[str, object],
    ledger_values: dict[str, float],
    *,
    model_id: str | None = None,
) -> str:
    """Register (or re-register) a model, recomputing ``noise_floor``/``baseline_throughput``
    from the 4 ledger rows named in ``meta["baseline_runs"]``. Refuses naming the field
    when a caller-supplied ``noise_floor`` or ``baseline_throughput`` disagrees with that
    recomputation. Delegates the rest of registration (campaign-budget defaults, the
    metric freeze) to :func:`~knowledge.ml_registry.write_path.register_model` unchanged.
    """
    runs = meta.get(BASELINE_RUNS_FIELD)
    if not isinstance(runs, list) or len(runs) != REQUIRED_BASELINE_RUN_COUNT:
        raise RegistryValidationError(
            f"model registration requires exactly {REQUIRED_BASELINE_RUN_COUNT} baseline_runs commits, "
            f"got {runs!r}",
            field=BASELINE_RUNS_FIELD,
        )
    values = []
    for commit in runs:
        if commit not in ledger_values:
            raise RegistryValidationError(
                f"baseline run commit {commit!r} has no matching row in the external ledger",
                field=BASELINE_RUNS_FIELD,
            )
        values.append(ledger_values[commit])
    floor, throughput = compute_noise_floor(values)

    stored_floor = meta.get(NOISE_FLOOR_FIELD)
    if stored_floor not in (None, "") and not _agree(float(stored_floor), floor):
        raise RegistryValidationError(
            f"stored noise_floor {stored_floor!r} disagrees with the recomputed value {floor!r} "
            f"from baseline_runs {runs!r}",
            field=NOISE_FLOOR_FIELD,
        )
    stored_throughput = meta.get(BASELINE_THROUGHPUT_FIELD)
    if stored_throughput not in (None, "") and not _agree(float(stored_throughput), throughput):
        raise RegistryValidationError(
            f"stored baseline_throughput {stored_throughput!r} disagrees with the recomputed value "
            f"{throughput!r} from baseline_runs {runs!r}",
            field=BASELINE_THROUGHPUT_FIELD,
        )

    merged = dict(meta)
    merged[NOISE_FLOOR_FIELD] = floor
    merged[BASELINE_THROUGHPUT_FIELD] = throughput
    merged.setdefault(RATCHET_COUNT_FIELD, 0)
    merged[CAMPAIGN_STATUS_FIELD] = ACTIVE
    return register_model(space, merged, model_id=model_id)


def _agree(a: float, b: float) -> bool:
    return abs(a - b) <= FLOOR_AGREEMENT_TOLERANCE


def adjudicate_trial(space: RegistrySpace, trial_id: str, observed_value: float) -> str:
    """Decide a trial's ``status`` against its model's ``baseline_throughput`` +/-
    ``noise_floor``, per the model's ``direction``. A SINGLE call is the whole
    adjudication -- no confirmation run is ever required, however close the margin.
    Sets and returns the trial's ``status`` (``"succeeded"`` or ``"failed"``).
    """
    trial = space.get(trial_id)
    if trial is None:
        raise RegistryValidationError(f"trial {trial_id!r} was never registered", field="trial_id")
    model_id = str(trial.meta.get("model_id"))
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(
            f"trial references model {model_id!r} that was never registered", field="model_id"
        )
    baseline = model.meta.get(BASELINE_THROUGHPUT_FIELD)
    floor = model.meta.get(NOISE_FLOOR_FIELD)
    if baseline is None or floor is None:
        raise RegistryValidationError(
            f"model {model_id!r} has no registered baseline_throughput/noise_floor to adjudicate against "
            "-- its harness was retired and must be re-registered with a fresh baseline",
            field=BASELINE_THROUGHPUT_FIELD,
        )
    direction = model.meta.get("direction")
    if direction == "minimize":
        won = observed_value <= float(baseline) - float(floor)
    elif direction == "maximize":
        won = observed_value >= float(baseline) + float(floor)
    else:
        raise RegistryValidationError(
            f"model direction must be 'minimize' or 'maximize', got {direction!r}", field="direction"
        )
    status = "succeeded" if won else "failed"
    trial.meta["status"] = status
    trial.meta["observed_value"] = observed_value
    return status


def retire_harness(space: RegistrySpace, model_id: str, patch: dict[str, object]) -> Fact:
    """Apply a harness-field patch to an already-registered model.

    A key in ``patch`` that names a :data:`HARNESS_FIELDS` field the model already has a
    RECORDED value for, and whose new value differs from that recorded value, is a
    harness mutation: it retires ``noise_floor``/``baseline_throughput``/``baseline_runs``,
    resets the ratchet counter to 0, marks the campaign :data:`STALLED`, and reverts
    whatever adoption is currently scored for this model (with
    :func:`~knowledge.ml_registry.lifecycle.invalidate_adoption`'s re-queue side effects)
    -- it does NOT fail; the model just refuses to adjudicate again until re-registered
    through :func:`register_model_with_baseline`. A patch that never touches a recorded
    harness value (including setting one for the first time) is an ordinary update.
    """
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")

    mutates_harness = any(
        field in HARNESS_FIELDS and field in model.meta and model.meta[field] != value
        for field, value in patch.items()
    )
    if mutates_harness:
        model.meta.pop(NOISE_FLOOR_FIELD, None)
        model.meta.pop(BASELINE_THROUGHPUT_FIELD, None)
        model.meta.pop(BASELINE_RUNS_FIELD, None)
        model.meta[RATCHET_COUNT_FIELD] = 0
        model.meta[REJECTION_STREAK_FIELD] = []
        model.meta[CAMPAIGN_STATUS_FIELD] = STALLED
        adopted = active_adoption(space, model_id)
        if adopted is not None:
            invalidate_adoption(space, adopted.id, "harness field mutation retired the noise floor")
    model.meta.update(patch)
    return model
