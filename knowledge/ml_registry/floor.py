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
Declaring a method does not buy an ARBITRARY floor, only a disagreeing one: a declared
floor is still bounded in MAGNITUDE against the spread the baseline rows show, since a
declaration is prose and nothing else in this module bounded the number it admitted.

Magnitude was the lesser half. A floor can be a perfectly reasonable number and still
measure the WRONG VARIANCE, so :data:`NOISE_FLOOR_VARIES_FIELD` states what VARIED across
the replicates it was measured over (``eval_sample`` / ``run_repeat`` / ``paired_delta``)
and :data:`TRIAL_COMPARISON_FIELD` states whether trials are dispatched ``paired`` against
the baseline row or ``unpaired``. :func:`guard_floor_provenance` refuses the two
combinations no magnitude check can see -- paired trials judged against a sampling-derived
floor (the noise cancels, so nothing clears the bar) and unpaired trials judged against a
paired-delta floor (the noise does not cancel, so wobble adopts). Both fields are
OPTIONAL: a record declaring neither is judged exactly as it was before they existed.
praxis reads a ledger and a model record and never runs the harness, so pairing cannot be
inferred -- it is DECLARED, and the guard holds the declared pair together.

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
# stamp -- and is NOT read as the legacy metric mean on that account: `ledger_throughputs`
# is older than the stamp, so an unstamped model is exactly as likely to hold rows/sec.
# adjudicate_trial refuses such a model rather than guessing (see _metric_baseline).
BASELINE_THROUGHPUT_UNITS_FIELD = "baseline_throughput_units"
#: rows/sec measured from the ledger's own throughput column -- NOT comparable to a metric.
THROUGHPUT_UNITS_ROWS_PER_SEC = "rows_per_sec"
#: the mean of the 4 baseline runs' metric values -- a legitimate metric bar.
THROUGHPUT_UNITS_METRIC_MEAN = "metric_mean"
# The stamp is a CLOSED vocabulary, checked as one. Reading it as "rows_per_sec or else no
# opinion" made every other string -- including a perfectly reasonable-looking one -- turn
# the units guard off rather than trip it: the court-marking campaign in sports_analysis
# stamped 'samples_per_second', which is rows/sec by another name, and _metric_baseline
# read straight past it into the metric-mean fallthrough it exists to prevent. An
# unrecognised stamp is a stamp whose meaning this module cannot establish, and the one
# thing it must never be taken to mean is the permissive case.
KNOWN_THROUGHPUT_UNITS: frozenset[str] = frozenset(
    {THROUGHPUT_UNITS_ROWS_PER_SEC, THROUGHPUT_UNITS_METRIC_MEAN}
)
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

# How far a DECLARED floor may sit from the spread the baseline rows actually show, as a
# multiple of their sample stdev. Declaring a method bought a floor its way past the
# recomputation, and past NOTHING else: the declaration is unverifiable prose, so
# noise_floor=1e9 method='bootstrap' registered and parked every future arm forever, and
# 1e-12 registered and adjudicated float wobble as signal. Neither is a typo -- the
# existing refusal catches typos -- so bound the MAGNITUDE against the only evidence this
# module has, the rows themselves. The band is wide on purpose: a bootstrap resampling of
# the eval set answers a different question than the repeats do and is expected to
# disagree, sometimes by a lot; what it cannot plausibly do is land three orders of
# magnitude away. Every real registration this registry serves sits between 0.15x and
# 2.6x.
SUPPLIED_FLOOR_MIN_SPREAD_RATIO = 0.1
SUPPLIED_FLOOR_MAX_SPREAD_RATIO = 10.0
# The way OUT of that band, for the case the band is genuinely wrong about. It is a stated
# reason rather than a boolean so the number stays accountable to a reader who finds it
# years later, which is the same thing NOISE_FLOOR_METHOD_FIELD is for.
NOISE_FLOOR_OVERRIDE_REASON_FIELD = "noise_floor_override_reason"

# WHAT VARIED when the floor was measured, and HOW a trial is compared to the baseline.
# These two are one guard, not two fields: a floor is only a bar on the comparison whose
# noise it measured, and the pair is the only way this module can tell whether it does.
#
# noise_floor_method answered "by what procedure", which turned out to be the lesser
# half -- 'bootstrap' is a procedure, and the same word covers resampling the eval set
# (whose noise a paired comparison CANCELS) and resampling arm-minus-baseline deltas on
# identical data (whose noise it does not). The magnitude bound in
# _check_floor_against_spread cannot separate them either: both are perfectly reasonable
# numbers a few tenths of the baseline spread apart. So provenance is stated as the thing
# the guard actually needs -- what SOURCE OF VARIANCE the replicates carried.
#
# The vocabulary is deliberately three words, because a fourth would be a judgement call
# nobody could make consistently at registration time:
NOISE_FLOOR_VARIES_FIELD = "noise_floor_varies"
#: WHICH eval items were scored varied between replicates -- frame/example bootstrap
#: resampling of a fixed config. Measures SAMPLING noise.
FLOOR_VARIES_EVAL_SAMPLE = "eval_sample"
#: One fixed config re-run on the SAME eval data; seeds and nondeterminism varied.
#: Measures RUN noise.
FLOOR_VARIES_RUN_REPEAT = "run_repeat"
#: Arm-minus-baseline DELTAS, each pair scored on identical data. Measures the noise of
#: the difference itself -- the quantity adjudication actually compares to the floor.
FLOOR_VARIES_PAIRED_DELTA = "paired_delta"
KNOWN_FLOOR_VARIES: frozenset[str] = frozenset(
    {FLOOR_VARIES_EVAL_SAMPLE, FLOOR_VARIES_RUN_REPEAT, FLOOR_VARIES_PAIRED_DELTA}
)

#: How the harness dispatches a trial against the baseline. praxis reads a ledger and a
#: model record; it never runs the harness, so it CANNOT infer this -- a ledger row is a
#: commit and a number and carries no trace of which eval draw produced it. A guard that
#: needed to infer it would not be a guard, so the model DECLARES it and the registry
#: holds it to the declaration.
TRIAL_COMPARISON_FIELD = "trial_comparison"
#: Every arm is scored on the SAME eval draw as the registered baseline row.
TRIAL_COMPARISON_PAIRED = "paired"
#: Each arm gets its own draw; arm and baseline share no per-item pairing.
TRIAL_COMPARISON_UNPAIRED = "unpaired"
KNOWN_TRIAL_COMPARISONS: frozenset[str] = frozenset(
    {TRIAL_COMPARISON_PAIRED, TRIAL_COMPARISON_UNPAIRED}
)

# The two combinations that are wrong in a way no magnitude check can see. Everything
# else registers: run_repeat noise does not cancel under pairing and does not vanish
# without it, so it is a defensible (if conservative) bar either way, and this guard
# refuses only what it can be SURE about.
_PROVENANCE_MISMATCHES: dict[tuple[str, str], str] = {
    (TRIAL_COMPARISON_PAIRED, FLOOR_VARIES_EVAL_SAMPLE): (
        "the floor measures how much the score moves when you change WHICH ITEMS are "
        "scored, and paired trials score the arm on the SAME draw as the baseline row, so "
        "exactly that variance CANCELS out of every delta. The bar is therefore orders of "
        "magnitude above the noise it claims to describe and nothing can clear it"
    ),
    (TRIAL_COMPARISON_UNPAIRED, FLOOR_VARIES_PAIRED_DELTA): (
        "the floor measures the spread of arm-minus-baseline deltas taken on IDENTICAL "
        "data, which is the small residue left after pairing cancels the sampling noise. "
        "Unpaired trials do not cancel it, so the real comparison carries that noise on "
        "top and the bar sits far below it -- resampling wobble adjudicates as a win"
    ),
}


def guard_floor_provenance(meta: dict[str, object]) -> None:
    """Refuse a model whose noise floor measures a variance its trials do not carry.

    Both fields are OPTIONAL and independent. A model that declares neither -- every
    model registered before this guard existed, and every one whose operator has nothing
    to say -- passes untouched; this guard has an opinion only where the record gives it
    one. What it will not accept is a value outside the vocabulary, for the reason
    ``baseline_throughput_units`` learned the hard way: an unrecognised string reads to a
    later checker as NO opinion, which is silently the unguarded case the stamp exists to
    close.

    THE INCIDENT. The detection campaign registered noise_floor=0.099758 = 2x the SD of
    eight baseline ledger rows. Those rows were frame-BOOTSTRAP DRAWS of a deterministic
    detector -- the harness resampled which eval frames were scored on each run -- so that
    SD measured SAMPLING noise. But every trial was dispatched PAIRED, on the same draw
    (seed 2) as the registered baseline row, and under pairing the frame-sampling noise
    cancels. The campaign spent 34 trials demanding a +0.0998 improvement from a
    comparison whose real noise was orders of magnitude smaller. ZERO adoptions, ratchet
    0. Five arms genuinely beat the incumbent -- 0.6203, 0.6177, 0.6159, 0.6138, 0.6123
    against 0.6076 -- and all five were filed "stagnant". Because an adopted arm composes
    into every later arm, the campaign's whole composition mechanism never opened: a
    silent, total loss of campaign progress with every individual component behaving
    exactly as written. The sibling association campaign, same registry and same CLI, took
    its floor as "2x the largest PAIRED-BOOTSTRAP DELTA SD over four perturbation arms"
    and was fine -- so nothing here noticed the difference, which is what this closes.
    """
    varies = meta.get(NOISE_FLOOR_VARIES_FIELD)
    comparison = meta.get(TRIAL_COMPARISON_FIELD)
    if varies not in (None, "") and str(varies) not in KNOWN_FLOOR_VARIES:
        raise RegistryValidationError(
            f"{NOISE_FLOOR_VARIES_FIELD} {varies!r} is not one of {sorted(KNOWN_FLOOR_VARIES)!r}. "
            "This field names the SOURCE OF VARIANCE the floor's replicates carried -- which eval "
            "items were scored (eval_sample), repeats of one fixed config on fixed data "
            "(run_repeat), or arm-minus-baseline deltas on identical data (paired_delta) -- and a "
            "word outside that vocabulary reads to this guard as no declaration at all, which is "
            "the unguarded case it exists to close",
            field=NOISE_FLOOR_VARIES_FIELD,
        )
    if comparison not in (None, "") and str(comparison) not in KNOWN_TRIAL_COMPARISONS:
        raise RegistryValidationError(
            f"{TRIAL_COMPARISON_FIELD} {comparison!r} is not one of "
            f"{sorted(KNOWN_TRIAL_COMPARISONS)!r}. praxis cannot infer this from the ledger -- a "
            "row is a commit and a number, with no trace of which eval draw produced it -- so the "
            "model must say it in a word this guard recognises or say nothing",
            field=TRIAL_COMPARISON_FIELD,
        )
    if varies in (None, "") or comparison in (None, ""):
        return
    why = _PROVENANCE_MISMATCHES.get((str(comparison), str(varies)))
    if why is None:
        return
    raise RegistryValidationError(
        f"{TRIAL_COMPARISON_FIELD}={comparison!r} cannot be judged against a noise_floor with "
        f"{NOISE_FLOOR_VARIES_FIELD}={varies!r}: {why}. This is the LAST point it can be caught -- "
        "praxis reads a ledger and a model record, it does not run the harness, so after "
        "registration every trial looks individually correct and the loss shows up only as a "
        "campaign that never adopts. It cost the detection campaign 34 trials and zero adoptions: "
        "five arms genuinely beat the incumbent (0.6203, 0.6177, 0.6159, 0.6138, 0.6123 against "
        "0.6076) and all five were filed stagnant, so the composition mechanism -- an adopted arm "
        "composes into every later arm -- never opened at all. Re-measure the floor over the "
        "variance the trials actually carry (for paired trials: the SD of arm-minus-baseline "
        f"deltas on identical data, {NOISE_FLOOR_VARIES_FIELD}={FLOOR_VARIES_PAIRED_DELTA!r}), or "
        f"dispatch the trials the way the floor was measured",
        field=NOISE_FLOOR_VARIES_FIELD,
    )


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

    Nor does an UNFAIR row collide. A run cut short but still scored -- a numeric metric
    with ``status=budget_exhausted``, exactly the row shape
    :data:`~knowledge.ml_registry.verdict.FAIR_RUN_STATUSES` exists to describe -- is a
    measurement the registry has already decided not to adjudicate on, and its legitimate
    re-run under the same ``{sha}:{arm_tag}`` key used to raise the duplicate error and
    make the WHOLE ledger unreadable, for every model in it. That collides head-on with
    ``register_trial``'s doctrine that a voided trial may be re-run -- which is what voided
    MEANS -- and left the operator inventing a new arm tag as the only escape, corrupting
    the join key's meaning to get around a guard aimed at something else. So the last FAIR
    row for a key wins; an unfair row is read only where no fair row exists, and two FAIR
    rows under one key still RAISE, which is the duplicate detection this whole refusal is
    for.

    That ledger carries a ``status`` column, so a crashed or aborted run is a real row
    with an empty (or short, or non-numeric) metric cell. Such a row is UNSCORED, not
    malformed input to choke on: it is skipped individually, and the commit is simply
    absent from the returned mapping -- so a caller that actually needs it fails naming
    that commit (:func:`register_model_with_baseline`, :func:`adjudicate_trial`) instead
    of one unrelated crashed run making the whole ledger unreadable.
    """
    # Imported HERE rather than at module scope: knowledge.ml_registry.verdict imports this
    # module for the ratchet and units fields, so a top-level import back would be a cycle.
    # The set stays defined next to the verdict rule it governs, and both loaders read that
    # one definition instead of each keeping a copy to drift.
    from knowledge.ml_registry.verdict import FAIR_RUN_STATUSES

    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = [column.strip() for column in next(reader)]
        except StopIteration:
            return {}
        # Optional, exactly as in cli.load_ledger_rows: a ledger without a status column is
        # older, not broken, and every row in it reads as fair.
        status_at = header.index("status") if "status" in header else None
        values: dict[str, float] = {}
        fair_keys: set[str] = set()
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
            status = row[status_at].strip() if status_at is not None and len(row) > status_at else ""
            if status.lower() not in FAIR_RUN_STATUSES:
                # Readable, but never authoritative: it fills the key only while no fair row
                # has, and a fair row arriving later overwrites it without complaint.
                if key not in fair_keys:
                    values[key] = value
                continue
            if key in fair_keys:
                duplicates.append(key)
            fair_keys.add(key)
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

    stored_units = meta.get(BASELINE_THROUGHPUT_UNITS_FIELD)
    if stored_units not in (None, ""):
        if str(stored_units) not in KNOWN_THROUGHPUT_UNITS:
            raise RegistryValidationError(
                f"{BASELINE_THROUGHPUT_UNITS_FIELD} {stored_units!r} is not one of "
                f"{sorted(KNOWN_THROUGHPUT_UNITS)!r}; this stamp is what later readers use to tell a "
                "rows/sec bar from a metric bar, and a string they do not recognise reads to them as "
                "NO opinion -- which is the unguarded case the stamp exists to close (a campaign that "
                "stamped 'samples_per_second' had the guard silently off). Say which of the two "
                "meanings the number carries, or omit the field and let registration stamp it",
                field=BASELINE_THROUGHPUT_UNITS_FIELD,
            )
        if str(stored_units) != throughput_units:
            raise RegistryValidationError(
                f"stored {BASELINE_THROUGHPUT_UNITS_FIELD} {stored_units!r} disagrees with what this "
                f"registration actually computed ({throughput_units!r}): baseline_throughput is the "
                f"{'slowest rows/sec of the baseline runs' if throughput_units == THROUGHPUT_UNITS_ROWS_PER_SEC else 'mean of the baseline runs metric values'} "
                "because that is what the inputs given here support (ledger_throughputs "
                f"{'was' if ledger_throughputs is not None else 'was not'} supplied)",
                field=BASELINE_THROUGHPUT_UNITS_FIELD,
            )

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
            _check_floor_against_spread(stored_floor_f, sd, meta, runs=runs, values=values)
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


def _check_floor_against_spread(
    floor: float, sd: float, meta: dict[str, object], *, runs: list[object], values: list[float]
) -> None:
    """Bound a DECLARED floor against the spread its own baseline rows show.

    ``noise_floor_method`` was a gate that verified nothing: it admitted any string that was
    not ``repeat_stdev``, and once admitted the floor was checked only for positivity. So
    the two floors that break a campaign in opposite directions both registered cleanly --
    a huge one parks every arm forever, a vanishing one adjudicates float wobble as signal
    -- and every later verdict was decided against a number nobody could account for. This
    is the accounting: within :data:`SUPPLIED_FLOOR_MIN_SPREAD_RATIO` and
    :data:`SUPPLIED_FLOOR_MAX_SPREAD_RATIO` of the rows' sample stdev.

    IDENTICAL rows (sd exactly 0) are EXEMPT, and that exemption is load-bearing rather
    than a loophole: a deterministic incumbent -- classical CV, no random seed -- produces
    the same number every repeat, its own stdev refuses to register as a floor at all, and
    a bootstrap-resampled floor is the ONE legitimate way such a model gets one. That is
    precisely the path this whole declared-floor branch exists for, and four live campaigns
    (association, court-marking among them) come through it. A zero denominator has no
    opinion to enforce, so it does not get to enforce one.
    """
    if sd == 0.0:
        return
    ratio = floor / sd
    if SUPPLIED_FLOOR_MIN_SPREAD_RATIO <= ratio <= SUPPLIED_FLOOR_MAX_SPREAD_RATIO:
        return
    override = meta.get(NOISE_FLOOR_OVERRIDE_REASON_FIELD)
    if override not in (None, "") and str(override).strip():
        return
    raise RegistryValidationError(
        f"declared noise_floor {floor!r} is {ratio:.3g}x the spread its own baseline runs show "
        f"(sample stdev {sd!r} over {runs!r} = {values!r}), outside "
        f"[{SUPPLIED_FLOOR_MIN_SPREAD_RATIO}x, {SUPPLIED_FLOOR_MAX_SPREAD_RATIO}x]. Declaring "
        f"{NOISE_FLOOR_METHOD_FIELD} lets a floor DISAGREE with the recomputation -- a bootstrap "
        "of the eval set measures a different thing than the repeats do -- but it is not a way to "
        "register an arbitrary number: a floor far above this band parks every arm forever, and one "
        "far below adjudicates float wobble as a win. Re-measure it, or state why this one is right "
        f"in {NOISE_FLOOR_OVERRIDE_REASON_FIELD} and it will be stored as measured",
        field=NOISE_FLOOR_FIELD,
    )


def _metric_baseline(
    model: Fact, model_id: str, ledger_values: dict[str, float], stored_bar: object
) -> float:
    """The METRIC value a trial is adjudicated against, in the order a reader would trust it.

    1. The ledger's value for the model's own ``baseline`` commit -- the same number
       :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` uses (its
       ``baseline_row.value``), so the two adjudications cannot disagree about the bar.
    2. Failing that, ``baseline_throughput`` -- but ONLY when it is stamped
       :data:`THROUGHPUT_UNITS_METRIC_MEAN`, i.e. it demonstrably holds the mean of the
       baseline runs' metric values, which is a real metric bar.
    3. Anything else is REFUSED, not used. Comparing an F1 of 0.99 against 3.5 rows/sec is
       not a close call to be resolved conservatively -- it is a category error, and the
       honest answer when the metric bar cannot be recovered is that this trial cannot be
       adjudicated here. That covers a :data:`THROUGHPUT_UNITS_ROWS_PER_SEC` stamp, a stamp
       outside :data:`KNOWN_THROUGHPUT_UNITS`, and -- see below -- NO stamp at all.
    """
    baseline_commit = model.meta.get(BASELINE_FIELD)
    if baseline_commit is not None and str(baseline_commit) in ledger_values:
        return float(ledger_values[str(baseline_commit)])
    units = model.meta.get(BASELINE_THROUGHPUT_UNITS_FIELD)
    if units in (None, ""):
        # An ABSENT stamp used to fall straight through to the metric-mean reading, on the
        # argument that a model registered before the stamp existed did in fact store a
        # metric mean. That presumption is wrong for exactly the models it was written to
        # excuse: `ledger_throughputs` (bae7abb) predates the stamp (5027002), so a model
        # registered through that parameter carries rows/sec and NO stamp to say so.
        # Reproduced: an unstamped model with baseline_throughput=3.5 rows/sec, whose
        # baseline commit had no scored ledger row, adjudicated an F1 of 0.99 against 3.5
        # and returned "failed" -- the same category error the stamp was introduced to
        # stop, arrived at by reading its absence as consent. Silence is not evidence, and
        # the remedy is cheap (re-register, or score the baseline commit), so refuse.
        raise RegistryValidationError(
            f"model {model_id!r} carries baseline_throughput {stored_bar!r} with no "
            f"{BASELINE_THROUGHPUT_UNITS_FIELD} stamp to say whether that is a metric mean or "
            f"rows/sec, and its baseline commit {baseline_commit!r} has no scored row in the "
            "external ledger to read the metric baseline from -- so there is no bar here that is "
            "known to be a metric. Adjudicate against the ledger row for the baseline commit "
            "(resolve-verdict), or re-register the model through register-model-with-baseline so "
            "the stamp exists",
            field=BASELINE_THROUGHPUT_UNITS_FIELD,
        )
    if str(units) not in KNOWN_THROUGHPUT_UNITS:
        raise RegistryValidationError(
            f"model {model_id!r} stamps its baseline_throughput {stored_bar!r} "
            f"{str(units)!r}, which is not one of {sorted(KNOWN_THROUGHPUT_UNITS)!r}; an "
            "unrecognised unit is a unit this module cannot establish the meaning of, and the one "
            "reading it must never get is the permissive one (a campaign stamped "
            "'samples_per_second' -- rows/sec by another name -- and had this guard silently off). "
            "Re-register the model so the stamp says which of the two meanings the number carries",
            field=BASELINE_THROUGHPUT_UNITS_FIELD,
        )
    if str(units) == THROUGHPUT_UNITS_ROWS_PER_SEC:
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
