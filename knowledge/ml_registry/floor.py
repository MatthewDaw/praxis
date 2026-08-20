"""Noise-floor registration, single-trial adjudication, and harness retirement (R12).

Builds on R11's write path (:func:`knowledge.ml_registry.write_path.register_model`) and
R3's idea lifecycle (:func:`knowledge.ml_registry.lifecycle.invalidate_adoption`). A
model's ``noise_floor`` and ``baseline_throughput`` are not caller-asserted numbers: they
are RECOMPUTED here from the baseline runs named by commit in ``meta["baseline_runs"]``
(at least 4, and MORE is better -- an SD from 4 points carries ~40% relative uncertainty),
read off the external results ledger, and a registration whose stored values disagree
with that recomputation is refused naming the disagreeing field. The one exception is a
floor that declares HOW it was measured in :data:`NOISE_FLOOR_METHOD_FIELD`: a bootstrap
resampling of the eval set measures a different thing than repeats of the run do, so it is
allowed to disagree and is stored as measured, stamped with its method. The method field
is therefore a GATE (does this disagreement get in) as well as provenance (how was it
measured). An UNDECLARED disagreement is still refused -- that refusal exists to catch an
unexplained number, not to force one method. The method is not consulted at adjudication.

A registered floor must be POSITIVE. A deterministic incumbent (classical CV, no random
seed) produces identical baseline rows, ``statistics.stdev`` returns exactly 0.0, and a
zero floor is the absence of a bar rather than a strict one: ``delta > noise_floor``
adopts on a 1e-12 float wobble, and the symmetric ``delta < -noise_floor`` rejection makes
the stagnant band a measure-zero set, so no arm can ever park. Registration REFUSES such a
floor and names the remedy rather than clamping it to some small positive number -- a
clamp would invent uncertainty the data does not show and then decide every later verdict
against that invention. The guard runs at REGISTRATION only; floors are stored per model,
so an already-registered model never re-enters this path. ``noise_floor`` is the
sample standard deviation of those runs; ``baseline_throughput`` is their mean, or --
when the caller passes ``ledger_throughputs`` -- the slowest of their rows/sec. Those two
meanings are NOT interchangeable, so registration stamps which one is stored in
:data:`BASELINE_THROUGHPUT_UNITS_FIELD` and :func:`adjudicate_trial` refuses to read a
rows/sec value as a metric bar.

Once a floor is registered, a candidate idea is adjudicated on a SINGLE trial --
:func:`adjudicate_trial` never asks for a confirmation run, however close the margin. Its
verdict comes from the SAME external ledger: it reads the value for the trial's own
``commit`` itself and refuses a commit with no scored row, so no number an agent reports
about its own run can decide that run's outcome. (The full production adjudication,
including throughput voiding, the stagnant band and the idea lifecycle, is R10's
:func:`~knowledge.ml_registry.verdict.adjudicate_verdict`; both use the same strict
``delta > noise_floor`` win test so they cannot reach opposite conclusions.)

A model's "harness" (:data:`HARNESS_FIELDS` -- eval size, precision, hardware) is held
more tightly than an ordinary judging field: :func:`retire_harness` refuses to let a
change to any of them pass quietly. Mutating an already-RECORDED harness field retires
both derived values, reverts whatever adoption is currently scored against them through
the shared :func:`revert_adoption` (invalidation's re-queue side effects AND the restore
of ``previous_baseline``, so the retired adoption's commit does not stay standing as the
model's baseline), clears the ratchet counter, and stalls the campaign (:data:`STALLED`) rather than
failing it -- the model simply will not adjudicate again until it is re-registered
through :func:`register_model_with_baseline` with a fresh 4-run procedure run at the
baseline commit left standing after that reversion.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

from knowledge.ml_registry.guards import ADJUDICATION_SOURCE
from knowledge.ml_registry.lifecycle import active_adoption, invalidate_adoption
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import Fact, RegistrySpace, mutate_model, register_model

BASELINE_RUNS_FIELD = "baseline_runs"
NOISE_FLOOR_FIELD = "noise_floor"
BASELINE_THROUGHPUT_FIELD = "baseline_throughput"
# WHICH of the two things `baseline_throughput` holds. The field collapsed two
# incompatible meanings -- the mean of the 4 baseline METRIC values when registration is
# given no throughputs, the slowest of their rows/sec when it is -- and nothing recorded
# which. adjudicate_trial read it as a metric bar either way, so a model registered from
# bootstrap's model_meta.json (throughput 3.5 rows/sec) adjudicated an F1 of 0.99 against
# 3.5 and failed it. Registration now STAMPS the meaning, so a later reader can tell the
# two apart instead of guessing. A model registered before this field existed carries no
# stamp; it is read as the legacy metric mean, which is what it in fact was.
BASELINE_THROUGHPUT_UNITS_FIELD = "baseline_throughput_units"
#: rows/sec measured from the ledger's own throughput column -- NOT comparable to a metric.
THROUGHPUT_UNITS_ROWS_PER_SEC = "rows_per_sec"
#: the mean of the 4 baseline runs' metric values -- a legitimate metric bar.
THROUGHPUT_UNITS_METRIC_MEAN = "metric_mean"
RATCHET_COUNT_FIELD = "ratchet_count"
# R10: the distinct idea ids behind the model's current consecutive-rejection streak --
# reset alongside RATCHET_COUNT_FIELD wherever the ratchet itself resets (here, on a
# harness mutation; in knowledge.ml_registry.verdict, on an adoption or an invalidation).
REJECTION_STREAK_FIELD = "rejection_streak_ideas"
CAMPAIGN_STATUS_FIELD = "campaign_status"
# The model's current baseline commit, and the one an adoption displaced. A reversion
# must put the latter back (see revert_adoption) -- knowledge.ml_registry.verdict names
# the same two fields.
BASELINE_FIELD = "baseline"
PREVIOUS_BASELINE_FIELD = "previous_baseline"

ACTIVE = "active"
STALLED = "stalled_pending_baseline"

# The MINIMUM number of baseline repeats a registration is built from -- not an exact
# count. It was exactly-4 until a campaign tried to follow af-seed-ml-supervise's own
# advice ("if a run is cheap, do more than 4 and pass the measured floor in") and was
# refused for logging 12 baselines, whose SD (0.0115 on sports_analysis) is the better
# number: 4 points carry ~40% relative uncertainty. More repeats are strictly better
# evidence, so more of them can never be the reason to refuse.
REQUIRED_BASELINE_RUN_COUNT = 4
FLOOR_AGREEMENT_TOLERANCE = 1e-9

# HOW the stored noise_floor was measured, when it was not measured by this module.
# Recomputing the SD of the baseline repeats is the default and needs no declaration; any
# OTHER measurement must say so, because a caller-supplied floor that disagrees with the
# recomputation is otherwise indistinguishable from a typo. Stamping the method keeps a
# bootstrap floor and a repeat-stdev floor tellable apart after the fact -- they answer
# different questions (how much the EVAL SET wobbles vs how much the RUN wobbles) and a
# reader who cannot tell which one is stored cannot interpret a 1-sigma margin.
NOISE_FLOOR_METHOD_FIELD = "noise_floor_method"
#: sigmas * sample stdev of the baseline repeats -- what this module computes itself.
NOISE_FLOOR_METHOD_REPEAT_STDEV = "repeat_stdev"

# The judging fields R12 holds even more tightly than R1's PROTECTED_MODEL_FIELDS: a
# change to any of them retires the noise floor and baseline throughput derived under
# the old harness, because a result measured under a different eval size, precision or
# hardware is not comparable to the runs that produced them.
HARNESS_FIELDS: frozenset[str] = frozenset({"eval_size", "precision", "hardware"})


def load_ledger_values(path: Path) -> dict[str, float]:
    """commit -> val_bpb for every SCORED row of the external results ledger
    (``results.tsv``), the same file :func:`knowledge.ml_registry.write_path.load_ledger_commits`
    reads and must tolerate identically.

    A key that appears TWICE is REFUSED naming it, never last-write-wins: the mapping is
    the join a verdict is decided through, so a silent overwrite decides that verdict on a
    different run than the one whose trial was registered (observed: rows ``abc 0.70``
    then ``abc 0.95`` left only 0.95, the 0.70 run gone with no warning). Only SCORED rows
    count as duplicates -- a crashed run and its re-run share a key legitimately, and the
    crashed one contributes no value to collide.

    That ledger carries a ``status`` column, so a crashed or aborted run is a real row
    with an empty (or short, or non-numeric) metric cell. Such a row is UNSCORED, not
    malformed input to choke on: it is skipped individually, and the commit is simply
    absent from the returned mapping -- so a caller that actually needs it fails naming
    that commit (:func:`register_model_with_baseline`, :func:`adjudicate_trial`) instead
    of one unrelated crashed run making the whole ledger unreadable.
    """
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            next(reader)  # header
        except StopIteration:
            return {}
        values: dict[str, float] = {}
        duplicates: list[str] = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            if len(row) < 2:
                continue
            key = row[0].strip()
            try:
                value = float(row[1])
            except ValueError:
                continue  # unscored run (crashed/aborted): no metric to join against
            if key in values:
                duplicates.append(key)
            values[key] = value
        if duplicates:
            raise RegistryValidationError(
                f"external ledger {str(path)!r} carries more than one scored row for "
                f"{sorted(set(duplicates))!r}; the registry joins a trial to its row BY THIS KEY, so "
                "a repeat silently decides the verdict on whichever run was written LAST and "
                "discards the other measurement entirely. Write '{sha}:{arm_tag}' so a campaign "
                "that varies arms by CONFIG still gets one key per run "
                "(bootstrap.check_ledger's join_keys_unique precondition checks the same thing at "
                "bootstrap time; this checks it again at every adjudication, because rows are "
                "appended long after bootstrap).",
                field="commit",
            )
        return values


def compute_noise_floor(values: list[float]) -> tuple[float, float]:
    """``(noise_floor, baseline_throughput)`` = ``(sample stdev, mean)`` of AT LEAST 4 runs."""
    if len(values) < REQUIRED_BASELINE_RUN_COUNT:
        raise RegistryValidationError(
            f"noise floor requires at least {REQUIRED_BASELINE_RUN_COUNT} baseline runs, got {len(values)}",
            field=BASELINE_RUNS_FIELD,
        )
    return statistics.stdev(values), statistics.mean(values)


def register_model_with_baseline(
    space: RegistrySpace,
    meta: dict[str, object],
    ledger_values: dict[str, float],
    *,
    model_id: str | None = None,
    ledger_throughputs: dict[str, float] | None = None,
) -> str:
    """Register (or re-register) a model, recomputing ``noise_floor``/``baseline_throughput``
    from the (>= 4) ledger rows named in ``meta["baseline_runs"]``. Refuses naming the field
    when a caller-supplied ``noise_floor`` or ``baseline_throughput`` disagrees with that
    recomputation AND declares no measurement method, and refuses a non-positive floor
    outright.

    ``meta[NOISE_FLOOR_METHOD_FIELD]`` is the declared escape hatch: a floor measured by
    bootstrap-resampling the eval set (what ``mvpvu.ball_campaign`` does) is a BETTER
    number than the SD of a handful of repeats, and refusing it forced callers to bypass
    this helper for plain ``register-model``, which checks nothing at all. The method is
    stored on the model so a bootstrap floor and a repeat-stdev floor stay tellable apart. Delegates the rest of registration (campaign-budget defaults, the
    metric freeze) to :func:`~knowledge.ml_registry.write_path.register_model` unchanged.

    ``meta["sigmas"]`` defaults to 1 so in-process R12 callers keep a one-sigma floor.
    Bootstrap emits ``sigmas=2`` and ``baseline_runs``, which is what makes its
    ``model_meta.json`` valid input to this function. ``ledger_throughputs``, when given,
    makes ``baseline_throughput`` the slowest of those runs (rows/sec) rather than the
    mean of the metric values -- the two meanings this field used to collapse.
    """
    runs = meta.get(BASELINE_RUNS_FIELD)
    if not isinstance(runs, list) or len(runs) < REQUIRED_BASELINE_RUN_COUNT:
        raise RegistryValidationError(
            f"model registration requires at least {REQUIRED_BASELINE_RUN_COUNT} baseline_runs commits, "
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
    sigmas = float(meta.get("sigmas", 1.0))
    sd = statistics.stdev(values)
    floor = sigmas * sd
    if ledger_throughputs is not None:
        tputs = []
        for commit in runs:
            if commit not in ledger_throughputs:
                raise RegistryValidationError(
                    f"baseline run commit {commit!r} has no throughput in the external ledger",
                    field=BASELINE_RUNS_FIELD,
                )
            tputs.append(ledger_throughputs[commit])
        throughput = min(tputs)
        throughput_units = THROUGHPUT_UNITS_ROWS_PER_SEC
    else:
        throughput = statistics.mean(values)
        throughput_units = THROUGHPUT_UNITS_METRIC_MEAN

    declared_method = meta.get(NOISE_FLOOR_METHOD_FIELD)
    method = NOISE_FLOOR_METHOD_REPEAT_STDEV
    stored_floor = meta.get(NOISE_FLOOR_FIELD)
    if stored_floor not in (None, ""):
        stored_floor_f = float(stored_floor)
        agrees = _agree(stored_floor_f, floor) or _agree(stored_floor_f, round(floor, 6))
        if declared_method not in (None, "", NOISE_FLOOR_METHOD_REPEAT_STDEV):
            # A DECLARED measurement is allowed to disagree, because it is answering a
            # different question than the repeats are. Only an UNDECLARED disagreement is
            # still refused -- the point of that refusal was never to force one method, it
            # was to catch a number nobody can account for.
            method = str(declared_method)
        elif not agrees:
            raise RegistryValidationError(
                f"stored noise_floor {stored_floor!r} disagrees with the recomputed value {floor!r} "
                f"from baseline_runs {runs!r}; if it was measured some OTHER way (bootstrap "
                f"resampling of the eval set, a held-out replicate study), declare it in "
                f"{NOISE_FLOOR_METHOD_FIELD!r} and it will be stored as measured",
                field=NOISE_FLOOR_FIELD,
            )
        floor = stored_floor_f
    elif declared_method not in (None, "", NOISE_FLOOR_METHOD_REPEAT_STDEV):
        raise RegistryValidationError(
            f"{NOISE_FLOOR_METHOD_FIELD} {declared_method!r} is declared but no noise_floor is "
            "stored to go with it; a declared method describes a floor the caller MEASURED, so "
            "pass that measured value in noise_floor or drop the method",
            field=NOISE_FLOOR_METHOD_FIELD,
        )

    if not (float(floor) > 0.0):
        raise RegistryValidationError(
            f"noise_floor {floor!r} is not positive (recomputed {sd!r} x {sigmas!r} sigmas over "
            f"{len(runs)} baseline runs {values!r}). A DETERMINISTIC incumbent -- classical CV, no "
            "random seed -- returns four IDENTICAL rows and statistics.stdev of those is exactly "
            "0.0. A zero floor is not a strict bar, it is the absence of one: adjudication adopts "
            "on delta > noise_floor, so a 1e-12 float wobble adopts, and the symmetric "
            "delta < -noise_floor rejection makes the stagnant band a measure-zero set, so nothing "
            "can ever park -- every arm adopts or rejects. Measure the floor the way "
            "sports_analysis's mvpvu.ball_campaign does (bootstrap_se: resample the EVAL SET, "
            f"since 'the noise floor is measured from bootstrap resampling of those 16 frames, not "
            f"from detector stochasticity'), then pass it as noise_floor with "
            f"{NOISE_FLOOR_METHOD_FIELD}='bootstrap'. It is deliberately NOT clamped to some small "
            "positive number here: a clamp would invent uncertainty this data does not show, and "
            "every later verdict would be decided against that invention.",
            field=NOISE_FLOOR_FIELD,
        )
    stored_throughput = meta.get(BASELINE_THROUGHPUT_FIELD)
    if stored_throughput not in (None, ""):
        stored_tput_f = float(stored_throughput)
        if not (_agree(stored_tput_f, throughput) or _agree(stored_tput_f, round(throughput, 4))):
            raise RegistryValidationError(
                f"stored baseline_throughput {stored_throughput!r} disagrees with the recomputed value "
                f"{throughput!r} from baseline_runs {runs!r}",
                field=BASELINE_THROUGHPUT_FIELD,
            )
        throughput = stored_tput_f

    merged = dict(meta)
    merged[NOISE_FLOOR_FIELD] = floor
    merged[BASELINE_THROUGHPUT_FIELD] = throughput
    merged[BASELINE_THROUGHPUT_UNITS_FIELD] = throughput_units
    merged[NOISE_FLOOR_METHOD_FIELD] = method
    merged.setdefault(RATCHET_COUNT_FIELD, 0)
    merged[CAMPAIGN_STATUS_FIELD] = ACTIVE
    return register_model(space, merged, model_id=model_id)


def _agree(a: float, b: float) -> bool:
    return abs(a - b) <= FLOOR_AGREEMENT_TOLERANCE


def _metric_baseline(
    model: Fact, model_id: str, ledger_values: dict[str, float], stored_bar: object
) -> float:
    """The METRIC value a trial is adjudicated against, in the order a reader would trust it.

    1. The ledger's value for the model's own ``baseline`` commit -- the same number
       :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` uses (its
       ``baseline_row.value``), so the two adjudications cannot disagree about the bar.
    2. Failing that, ``baseline_throughput`` -- but ONLY when it is stamped (or, on a model
       registered before the stamp existed, implied) to be the mean of the baseline runs'
       metric values, which is a real metric bar.
    3. A ``baseline_throughput`` stamped :data:`THROUGHPUT_UNITS_ROWS_PER_SEC` is REFUSED,
       not used. Comparing an F1 of 0.99 against 3.5 rows/sec is not a close call to be
       resolved conservatively -- it is a category error, and the honest answer when the
       metric bar cannot be recovered is that this trial cannot be adjudicated here.
    """
    baseline_commit = model.meta.get(BASELINE_FIELD)
    if baseline_commit is not None and str(baseline_commit) in ledger_values:
        return float(ledger_values[str(baseline_commit)])
    if model.meta.get(BASELINE_THROUGHPUT_UNITS_FIELD) == THROUGHPUT_UNITS_ROWS_PER_SEC:
        raise RegistryValidationError(
            f"model {model_id!r} records baseline_throughput {stored_bar!r} in rows/sec, which is "
            f"not a metric bar, and its baseline commit {baseline_commit!r} has no scored row in "
            "the external ledger to read the metric baseline from -- adjudicate against the ledger "
            "row for the baseline commit (resolve-verdict), or re-register the model so the "
            "baseline commit is scored",
            field=BASELINE_THROUGHPUT_FIELD,
        )
    return float(stored_bar)  # type: ignore[arg-type]


def adjudicate_trial(
    space: RegistrySpace,
    trial_id: str,
    ledger_values: dict[str, float],
    *,
    self_reported_value: float | None = None,
) -> str:
    """Decide a trial's ``status`` from the EXTERNAL LEDGER value for its own ``commit``,
    against its model's METRIC baseline +/- ``noise_floor`` per the model's ``direction``
    (:func:`_metric_baseline` -- the ledger value for the model's ``baseline`` commit,
    exactly the number ``adjudicate_verdict`` compares against, falling back to
    ``baseline_throughput`` only where that field demonstrably holds the mean of the
    baseline runs' metric values, and REFUSING where it holds rows/sec). A SINGLE call is
    the whole adjudication -- no confirmation run is ever
    required, however close the margin. Sets and returns the trial's ``status``
    (``"succeeded"`` or ``"failed"``).

    The value adjudicated is never supplied by the judged agent. The trial's ``commit`` is
    looked up in ``ledger_values`` (:func:`load_ledger_values`) and a commit with no scored
    ledger row is REFUSED naming ``commit`` -- an unscored run is not a loss, it is an
    absent measurement. ``self_reported_value`` is optional and is only ever a claim to be
    CHECKED: when supplied it must agree with the ledger row, and a disagreement is refused
    naming ``observed_value``, the same recompute-refuses-drift shape
    :func:`register_model_with_baseline` uses for a stored floor and
    :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` uses for a trial's
    self-reported throughput/diff_lines.

    The win test is STRICT (``delta > noise_floor``) so that this and
    :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` cannot disagree about a
    delta of exactly one standard deviation. ``adjudicate_verdict`` is the full production
    adjudication (it also voids on throughput collapse, parks the stagnant band, drives the
    idea lifecycle and the ratchet); this function decides the trial's status ALONE and
    exists for the model-level floor check without idea-lifecycle side effects.
    """
    trial = space.get(trial_id)
    if trial is None:
        raise RegistryValidationError(f"trial {trial_id!r} was never registered", field="trial_id")
    commit = trial.meta.get("commit")
    if commit is None or str(commit).strip() == "":
        raise RegistryValidationError(
            f"trial {trial_id!r} names no commit to join against the external ledger", field="commit"
        )
    commit = str(commit)
    if commit not in ledger_values:
        raise RegistryValidationError(
            f"trial commit {commit!r} has no scored row in the external ledger; "
            "a trial is adjudicated on the ledger's value, never on a reported one",
            field="commit",
        )
    observed_value = float(ledger_values[commit])
    if self_reported_value is not None and not _agree(float(self_reported_value), observed_value):
        raise RegistryValidationError(
            f"trial {trial_id!r} self-reported value {self_reported_value!r} disagrees with the "
            f"external ledger's value {observed_value!r} for commit {commit!r}",
            field="observed_value",
        )
    model_id = str(trial.meta.get("model_id"))
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(
            f"trial references model {model_id!r} that was never registered", field="model_id"
        )
    stored_bar = model.meta.get(BASELINE_THROUGHPUT_FIELD)
    floor = model.meta.get(NOISE_FLOOR_FIELD)
    if stored_bar is None or floor is None:
        raise RegistryValidationError(
            f"model {model_id!r} has no registered baseline_throughput/noise_floor to adjudicate against "
            "-- its harness was retired and must be re-registered with a fresh baseline",
            field=BASELINE_THROUGHPUT_FIELD,
        )
    baseline = _metric_baseline(model, model_id, ledger_values, stored_bar)
    direction = model.meta.get("direction")
    if direction == "minimize":
        delta = float(baseline) - observed_value
    elif direction == "maximize":
        delta = observed_value - float(baseline)
    else:
        raise RegistryValidationError(
            f"model direction must be 'minimize' or 'maximize', got {direction!r}", field="direction"
        )
    status = "succeeded" if delta > float(floor) else "failed"
    trial.meta["status"] = status
    trial.meta["observed_value"] = observed_value
    return status


def revert_adoption(space: RegistrySpace, model_id: str, reason: str) -> bool:
    """THE reversion routine for a repudiated adoption -- the single place both callers
    (R12's :func:`retire_harness` here, R10's ``verdict._invalidate_ratchet``) go, so they
    cannot diverge on what "revert the adoption" means.

    Reverting is three things, never a subset: the adoption is invalidated (with
    :func:`~knowledge.ml_registry.lifecycle.invalidate_adoption`'s re-queue side effects),
    the model's ``baseline`` is restored to the ``previous_baseline`` that adoption
    displaced -- an un-adopted idea whose commit is still the model's baseline would leave
    every later trial scored against a bar that was just repudiated -- and the ratchet
    counter/streak are cleared. Returns whether there was an active adoption to revert;
    the ratchet reset happens either way.
    """
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")
    adopted = active_adoption(space, model_id)
    if adopted is not None:
        invalidate_adoption(space, adopted.id, reason)
        previous = model.meta.pop(PREVIOUS_BASELINE_FIELD, None)
        if previous is not None:
            mutate_model(space, model_id, {BASELINE_FIELD: previous}, source=ADJUDICATION_SOURCE)
    model.meta[RATCHET_COUNT_FIELD] = 0
    model.meta[REJECTION_STREAK_FIELD] = []
    return adopted is not None


def retire_harness(space: RegistrySpace, model_id: str, patch: dict[str, object]) -> Fact:
    """Apply a harness-field patch to an already-registered model.

    A key in ``patch`` that names a :data:`HARNESS_FIELDS` field the model already has a
    RECORDED value for, and whose new value differs from that recorded value, is a
    harness mutation: it retires ``noise_floor``/``baseline_throughput``/``baseline_runs``,
    marks the campaign :data:`STALLED`, and reverts whatever adoption is currently scored
    for this model through :func:`revert_adoption` -- which resets the ratchet counter and
    streak, re-queues the tenure's rejections, and restores ``previous_baseline`` so the
    re-registration really does happen at the baseline commit left standing after the
    reversion -- it does NOT fail; the model just refuses to adjudicate again until re-registered
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
        model.meta.pop(BASELINE_THROUGHPUT_UNITS_FIELD, None)
        model.meta.pop(BASELINE_RUNS_FIELD, None)
        model.meta[CAMPAIGN_STATUS_FIELD] = STALLED
        revert_adoption(space, model_id, "harness field mutation retired the noise floor")
    model.meta.update(patch)
    return model
