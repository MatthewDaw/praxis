"""Baseline registration, the per-comparison rope, and harness retirement (R12, R3a).

Builds on R11's write path (:func:`knowledge.ml_registry.write_path.register_model`) and
R3's idea lifecycle (:func:`knowledge.ml_registry.lifecycle.invalidate_adoption`).

**THE ROPE IS COMPUTED, NOT STORED.** A model used to carry a threshold asserted at
registration, and every verdict was decided against that number for the life of the
campaign. R3a retired it. Two thresholds cannot decide one verdict, and the stored one was
the weaker of the pair: it was a caller's claim about a measurement praxis never made, so
the module around it grew into a policing apparatus -- a recomputation it had to agree
with, a declared method that let it disagree, a magnitude band bounding what the
disagreement could be, an override reason escaping the band, and a declared sigma count to
check the whole chain against. Every one of those existed to make a stored number
accountable. None of them is needed once the number is not stored: what a model records is
the EVIDENCE (``baseline_runs``, at least 4 commits on the external ledger), and the rope
is recomputed from those rows at every comparison, by :func:`comparison_rope`.

That single change is also what closes the two cold-start defects §5.2 of the build plan
names. A DETERMINISTIC incumbent -- classical CV, no random seed -- yields identical
baseline rows and a rope of exactly 0.0, which registration used to refuse outright; there
is nothing to refuse now, because nothing is being stored, and a campaign that wants a
positive bar for such a model measures one the way §5.2 says (a bootstrap of the metric
over the scoring corpus's own ``split_unit`` --
:func:`knowledge.ml_registry.policy_gate.compute_campaign_rope`) rather than by repeating a
run that cannot vary. And a campaign with NO incumbent no longer needs a champion to repeat
itself before its tie test is defined.

``baseline_throughput`` is unchanged and is still stored: it is a SPEED bar, not a
threshold on the metric, and the ledger's throughput column is the only place it can come
from. Registration stamps which of the two things it holds in
:data:`BASELINE_THROUGHPUT_UNITS_FIELD` -- the slowest rows/sec of the baseline runs when
``ledger_throughputs`` is supplied, the mean of their metric values otherwise -- because the
field collapsed those two incompatible meanings and :func:`adjudicate_trial` refuses to
read a rows/sec value as a metric bar.

What DOES survive from the old apparatus is the part that was never about policing a stored
number: :data:`SIGMAS_FIELD` (how many multiples of the measured dispersion the bar is --
the registry now does that multiplication itself, so the field can no longer contradict the
bar), :func:`guard_rope_provenance` (a rope is only a bar on the comparison whose variance
it measured), and the residual-to-ceiling scaling in this module's second half (a bar
measured in the incumbent's regime overcharges a campaign that has left it).

Once a model is registered, a candidate idea is adjudicated on a SINGLE trial --
:func:`adjudicate_trial` never asks for a confirmation run, however close the margin. Its
verdict comes from the SAME external ledger: it reads the value for the trial's own
``commit`` itself and refuses a commit with no scored row, so no number an agent reports
about its own run can decide that run's outcome. (The full production adjudication,
including throughput voiding, the stagnant band and the idea lifecycle, is R10's
:func:`~knowledge.ml_registry.verdict.adjudicate_verdict`; both use the same strict
``delta > rope`` win test so they cannot reach opposite conclusions.)

A model's "harness" (:data:`HARNESS_FIELDS` -- eval size, precision, hardware) is held
more tightly than an ordinary judging field: :func:`retire_harness` refuses to let a
change to any of them pass quietly. Mutating an already-RECORDED harness field retires the
baseline evidence the rope is computed from, reverts whatever adoption is currently scored
against it through the shared :func:`revert_adoption` (invalidation's re-queue side effects
AND the restore of ``previous_baseline``, so the retired adoption's commit does not stay
standing as the model's baseline), clears the ratchet counter, and stalls the campaign
(:data:`STALLED`) rather than failing it -- the model simply will not adjudicate again until
it is re-registered through :func:`register_model_with_baseline` with a fresh 4-run
procedure run at the baseline commit left standing after that reversion.
"""

from __future__ import annotations

from collections.abc import Sequence
import statistics
from pathlib import Path

from knowledge.ml_registry.contracts.ledger_v2 import read_ledger_compatibility
from knowledge.ml_registry.guards import ADJUDICATION_SOURCE
from knowledge.ml_registry.lifecycle import active_adoption, invalidate_adoption
from knowledge.ml_registry.schema import RegistryValidationError
from knowledge.ml_registry.write_path import Fact, RegistrySpace, mutate_model, register_model

#: The commits whose ledger rows ARE the rope's evidence. This is what a model stores in
#: place of the retired threshold, and protecting it is what
#: :data:`~knowledge.ml_registry.guards.PROTECTED_MODEL_FIELDS` now protects.
BASELINE_RUNS_FIELD = "baseline_runs"
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
#: the mean of the baseline runs' metric values -- a legitimate metric bar.
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

# The MINIMUM number of baseline repeats a rope is computed from -- not an exact count. It
# was exactly-4 until a campaign tried to follow af-seed-ml-supervise's own advice ("if a
# run is cheap, do more than 4") and was refused for logging 12 baselines, whose SD
# (0.0115 on sports_analysis) is the better number: 4 points carry ~40% relative
# uncertainty. More repeats are strictly better evidence, so more of them can never be the
# reason to refuse.
REQUIRED_BASELINE_RUN_COUNT = 4
#: Absolute agreement for a recomputed number against a stored one -- the throughput bar,
#: and a trial's self-reported value against its ledger row.
AGREEMENT_TOLERANCE = 1e-9

# HOW MANY SIGMAS of the measured dispersion the bar is.
#
# THE INCIDENT this field was hardened for: the court-marking campaign stored a floor of
# 0.012481 -- the RAW one-sigma bootstrap SD -- while its record declared `sigmas: 2`. Both
# numbers sat in the same dict and nothing compared them, so its adopt/park band was half
# the width its own record claimed (~16% one-sided false adoption per null arm rather than
# ~2.5%), and a human audit found it rather than the registry. That contradiction is now
# UNREACHABLE rather than checked: there is no second number to disagree with, because the
# registry does the multiplication itself at every comparison.
SIGMAS_FIELD = "sigmas"
#: WHY a campaign chose the sigmas it did. Optional, never required -- but it is what a
#: reader finds when campaign-status tells them this campaign is running a loose bar.
SIGMAS_REASON_FIELD = "sigmas_reason"

# THE STANDING DEFAULT IS ONE SIGMA. See the module docstring of
# knowledge.ml_registry.bootstrap for what that buys and what it costs; the number lives
# here because floor.py is what multiplies by it, and a campaign that wants the old
# 2-sigma bar sets `sigmas: 2` in one field.
DEFAULT_SIGMAS = 1.0
#: Above this, a campaign is running a bar tighter than the default and nobody needs warning.
#: At or below it, report.diagnose says so once the backlog is big enough to matter.
CONSERVATIVE_SIGMAS = 2.0


def declared_sigmas(meta: dict[str, object]) -> float:
    """The multiplier this model's bar is measured in, defaulting to :data:`DEFAULT_SIGMAS`.

    Refuses a value that cannot describe a bar at all. It no longer has a stored floor to
    be checked against -- :func:`comparison_rope` multiplies by it directly, so the field
    is load-bearing by construction rather than by agreement.
    """
    declared = meta.get(SIGMAS_FIELD)
    if declared in (None, ""):
        return DEFAULT_SIGMAS
    try:
        sigmas = float(declared)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RegistryValidationError(
            f"{SIGMAS_FIELD} {declared!r} is not a number. It states how many multiples of the "
            "measured dispersion the bar is, and a value that cannot be multiplied cannot "
            "produce a bar at all",
            field=SIGMAS_FIELD,
        ) from None
    if not sigmas > 0.0:
        raise RegistryValidationError(
            f"{SIGMAS_FIELD} {sigmas!r} is not positive; a bar is a positive multiple of a "
            "dispersion, and zero or negative sigmas describes no bar at all",
            field=SIGMAS_FIELD,
        )
    return sigmas


# WHAT VARIED when the rope was measured, and HOW a trial is compared to the baseline.
# These two are one guard, not two fields: a rope is only a bar on the comparison whose
# noise it measured, and the pair is the only way this module can tell whether it does.
#
# The vocabulary is deliberately three words, because a fourth would be a judgement call
# nobody could make consistently at registration time:
ROPE_VARIES_FIELD = "rope_varies"
#: WHICH eval items were scored varied between replicates -- frame/example bootstrap
#: resampling of a fixed config, which is what §5.2's split_unit bootstrap does. Measures
#: SAMPLING noise.
ROPE_VARIES_EVAL_SAMPLE = "eval_sample"
#: One fixed config re-run on the SAME eval data; seeds and nondeterminism varied.
#: Measures RUN noise -- what recomputing over ``baseline_runs`` measures.
ROPE_VARIES_RUN_REPEAT = "run_repeat"
#: Arm-minus-baseline DELTAS, each pair scored on identical data. Measures the noise of
#: the difference itself -- the quantity adjudication actually compares to the rope.
ROPE_VARIES_PAIRED_DELTA = "paired_delta"
KNOWN_ROPE_VARIES: frozenset[str] = frozenset(
    {ROPE_VARIES_EVAL_SAMPLE, ROPE_VARIES_RUN_REPEAT, ROPE_VARIES_PAIRED_DELTA}
)

#: How the harness dispatches a trial against the baseline. praxis reads a ledger and a
#: model record and never runs the harness, so pairing cannot be inferred -- it is
#: DECLARED, and :func:`guard_rope_provenance` holds the declared pair together.
TRIAL_COMPARISON_FIELD = "trial_comparison"
TRIAL_COMPARISON_PAIRED = "paired"
TRIAL_COMPARISON_UNPAIRED = "unpaired"
KNOWN_TRIAL_COMPARISONS: frozenset[str] = frozenset(
    {TRIAL_COMPARISON_PAIRED, TRIAL_COMPARISON_UNPAIRED}
)

_PROVENANCE_MISMATCHES: dict[tuple[str, str], str] = {
    (TRIAL_COMPARISON_PAIRED, ROPE_VARIES_EVAL_SAMPLE): (
        "a paired trial is scored on the SAME eval draw as the baseline row, so the sampling "
        "noise the rope measured is partly common to both sides and cancels in the delta -- the "
        "bar is measured over variance the comparison does not carry, and nothing clears it"
    ),
    (TRIAL_COMPARISON_UNPAIRED, ROPE_VARIES_PAIRED_DELTA): (
        "an unpaired trial is scored on its own draw, so the delta carries BOTH sides' sampling "
        "noise -- a rope measured over paired deltas is far too narrow for it, and ordinary "
        "wobble adjudicates as a win"
    ),
}


def guard_rope_provenance(meta: dict[str, object]) -> None:
    """Refuse a model whose rope measures a variance its trials do not carry.

    Both fields are OPTIONAL and independent. A model that declares neither -- every
    model registered before this guard existed, and every one whose operator has nothing
    to say -- passes untouched; this guard has an opinion only where the record gives it
    one. What it will not accept is a value outside the vocabulary, for the reason
    ``baseline_throughput_units`` learned the hard way: an unrecognised string reads to a
    later checker as NO opinion, which is silently the unguarded case the stamp exists to
    close.

    THE INCIDENT. The detection campaign's bar was 2x the SD of eight baseline ledger rows.
    Those rows were frame-BOOTSTRAP DRAWS of a deterministic detector -- the harness
    resampled which eval frames were scored on each run -- so that SD measured SAMPLING
    noise. But every trial was dispatched PAIRED, on the same draw (seed 2) as the
    registered baseline row, and pairing removes part of that variance. 34 trials, ZERO
    adoptions, ratchet 0.

    HOW MUCH IT REMOVES DEPENDS ON THE METRIC, AND THE FIRST VERSION OF THIS DOCSTRING GOT
    THAT WRONG. It claimed the paired noise was "orders of magnitude smaller", generalising
    from the sibling association campaign, where HOTA -- a smooth pooled statistic -- does
    collapse ~100x under pairing. It was then MEASURED here: 2000 paired bootstrap draws
    over five real detection arms gave a delta SD of 0.0094-0.0351, a reduction of only
    2.9x-9.5x, and a correct bar of 0.0703 rather than the ~0.005 the collapse assumption
    predicted. At that bar NOT ONE of the 34 recorded verdicts changes.

    The reason is that tiny_person_recall_at_p90 is a CONSTRAINED ARGMAX -- max recall
    subject to precision >= 0.90 -- not a pooled mean. The operating threshold is
    re-selected per arm and per draw (baseline threshold SD 0.0292 over 0.701-0.866), and
    the arm picks a different threshold from the baseline on 79-100% of draws, so the two
    land on different points of different PR curves and the sampling noise is only
    PARTIALLY common. Expect a large collapse for a smooth statistic and a modest one for
    an argmax or any other selection-based metric -- and MEASURE it rather than assuming,
    because the assumption is what cost this campaign its bar.

    The mismatch this guard refuses is real either way: an eval_sample rope is still the
    wrong quantity for a paired comparison, whatever the ratio turns out to be.
    """
    varies = meta.get(ROPE_VARIES_FIELD)
    comparison = meta.get(TRIAL_COMPARISON_FIELD)
    if varies not in (None, "") and str(varies) not in KNOWN_ROPE_VARIES:
        raise RegistryValidationError(
            f"{ROPE_VARIES_FIELD} {varies!r} is not one of {sorted(KNOWN_ROPE_VARIES)!r}. "
            "This field names the SOURCE OF VARIANCE the rope's replicates carried -- which eval "
            "items were scored (eval_sample), repeats of one fixed config on fixed data "
            "(run_repeat), or arm-minus-baseline deltas on identical data (paired_delta) -- and a "
            "word outside that vocabulary reads to this guard as no declaration at all, which is "
            "the unguarded case it exists to close",
            field=ROPE_VARIES_FIELD,
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
        f"{TRIAL_COMPARISON_FIELD}={comparison!r} cannot be judged against a rope with "
        f"{ROPE_VARIES_FIELD}={varies!r}: {why}. This is the LAST point it can be caught -- "
        "praxis reads a ledger and a model record, it does not run the harness, so after "
        "registration every trial looks individually correct and the loss shows up only as a "
        "campaign that never adopts. It cost the detection campaign 34 trials and zero adoptions: "
        "five arms genuinely beat the incumbent (0.6203, 0.6177, 0.6159, 0.6138, 0.6123 against "
        "0.6076) and all five were filed stagnant, so the composition mechanism -- an adopted arm "
        "composes into every later arm -- never opened at all. Re-measure the rope over the "
        "variance the trials actually carry (for paired trials: the SD of arm-minus-baseline "
        f"deltas on identical data, {ROPE_VARIES_FIELD}={ROPE_VARIES_PAIRED_DELTA!r}), or "
        f"dispatch the trials the way the rope was measured",
        field=ROPE_VARIES_FIELD,
    )


# The judging fields R12 holds even more tightly than R1's PROTECTED_MODEL_FIELDS: a
# change to any of them retires the baseline evidence and the throughput bar derived under
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
    projection = read_ledger_compatibility(path)
    if projection.duplicate_fair_metric_commits:
        raise RegistryValidationError(
            f"external ledger {str(path)!r} carries more than one scored row for "
            f"{list(projection.duplicate_fair_metric_commits)!r}; the registry joins a trial to its row BY THIS KEY, so "
            "a repeat silently decides the verdict on whichever run was written LAST and "
            "discards the other measurement entirely. Write '{sha}:{arm_tag}' so a campaign "
            "that varies arms by CONFIG still gets one key per run "
            "(bootstrap.check_ledger's join_keys_unique precondition checks the same thing at "
            "bootstrap time; this checks it again at every adjudication, because rows are "
            "appended long after bootstrap).",
            field="commit",
        )
    return dict(projection.metric_values)


def baseline_values(meta: dict[str, object], ledger_values: dict[str, float]) -> list[float]:
    """The ledger values of the (>= 4) commits named in ``baseline_runs`` -- the rope's
    evidence, read fresh at every comparison rather than distilled into a stored number.

    Refuses a model with too few baseline runs, and a run whose commit has no scored row:
    an absent measurement is not a narrow rope, it is no rope at all.
    """
    runs = meta.get(BASELINE_RUNS_FIELD)
    if not isinstance(runs, list) or len(runs) < REQUIRED_BASELINE_RUN_COUNT:
        raise RegistryValidationError(
            f"the rope is computed from at least {REQUIRED_BASELINE_RUN_COUNT} "
            f"{BASELINE_RUNS_FIELD} commits, got {runs!r}",
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
    return values


def measure_rope(values: Sequence[float], *, sigmas: float = DEFAULT_SIGMAS) -> float:
    """``sigmas`` x the sample stdev of the baseline replicates.

    Zero is a legitimate answer, not a refusal: a deterministic incumbent produces
    identical rows and the honest measurement of their spread is 0.0. Clamping it to some
    small positive number would invent uncertainty the data does not show and then decide
    every later verdict against that invention; a campaign that needs a positive bar for
    such a model measures one over its scoring corpus instead (§5.2).
    """
    return sigmas * statistics.stdev(values)


def register_model_with_baseline(
    space: RegistrySpace,
    meta: dict[str, object],
    ledger_values: dict[str, float],
    *,
    model_id: str | None = None,
    ledger_throughputs: dict[str, float] | None = None,
) -> str:
    """Register (or re-register) a model against the (>= 4) ledger rows named in
    ``meta["baseline_runs"]``, recomputing ``baseline_throughput`` from them.

    No threshold is stored: the rope is recomputed from those same rows at every
    comparison (:func:`comparison_rope`), so this path has nothing to police beyond the
    evidence itself. Delegates the rest of registration (campaign-budget defaults, the
    metric freeze) to :func:`~knowledge.ml_registry.write_path.register_model` unchanged.

    ``ledger_throughputs``, when given, makes ``baseline_throughput`` the slowest of those
    runs (rows/sec) rather than the mean of the metric values -- the two meanings this
    field used to collapse -- and registration stamps which one it stored.
    """
    values = baseline_values(meta, ledger_values)
    runs = list(meta[BASELINE_RUNS_FIELD])  # type: ignore[arg-type]
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

    stored_throughput = meta.get(BASELINE_THROUGHPUT_FIELD)
    if stored_throughput not in (None, ""):
        stored_tput_f = float(stored_throughput)  # type: ignore[arg-type]
        if not (_agree(stored_tput_f, throughput) or _agree(stored_tput_f, round(throughput, 4))):
            raise RegistryValidationError(
                f"stored baseline_throughput {stored_throughput!r} disagrees with the recomputed value "
                f"{throughput!r} from baseline_runs {runs!r}",
                field=BASELINE_THROUGHPUT_FIELD,
            )
        throughput = stored_tput_f

    merged = dict(meta)
    # v_measured for a scaled rope: the metric level these replicates describe. Stamped
    # here because this is the only registration path that HAS the ledger rows to derive it
    # from; register_model refuses a scaling declaration that arrives without it.
    if _rope_scaling_mode(merged) == ROPE_SCALING_RESIDUAL:
        merged.setdefault(ROPE_MEASURED_AT_FIELD, statistics.mean(values))
    merged[BASELINE_THROUGHPUT_FIELD] = throughput
    merged[BASELINE_THROUGHPUT_UNITS_FIELD] = throughput_units
    merged.setdefault(RATCHET_COUNT_FIELD, 0)
    merged[CAMPAIGN_STATUS_FIELD] = ACTIVE
    return register_model(space, merged, model_id=model_id)


def _agree(a: float, b: float) -> bool:
    return abs(a - b) <= AGREEMENT_TOLERANCE


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
    against its model's METRIC baseline +/- the rope recomputed for THIS comparison
    (:func:`comparison_rope`), per the model's ``direction``. A SINGLE call is the whole
    adjudication -- no confirmation run is ever required, however close the margin. Sets
    and returns the trial's ``status`` (``"succeeded"`` or ``"failed"``).

    The value adjudicated is never supplied by the judged agent. The trial's ``commit`` is
    looked up in ``ledger_values`` (:func:`load_ledger_values`) and a commit with no scored
    ledger row is REFUSED naming ``commit`` -- an unscored run is not a loss, it is an
    absent measurement. ``self_reported_value`` is optional and is only ever a claim to be
    CHECKED: when supplied it must agree with the ledger row, and a disagreement is refused
    naming ``observed_value``, the same recompute-refuses-drift shape
    :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` uses for a trial's
    self-reported throughput/diff_lines.

    The win test is STRICT (``delta > rope``) so that this and
    :func:`~knowledge.ml_registry.verdict.adjudicate_verdict` cannot disagree about a
    delta of exactly one standard deviation. ``adjudicate_verdict`` is the full production
    adjudication (it also voids on throughput collapse, parks the stagnant band, drives the
    idea lifecycle and the ratchet); this function decides the trial's status ALONE and
    exists for the model-level bar check without idea-lifecycle side effects.
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
    if stored_bar is None or model.meta.get(BASELINE_RUNS_FIELD) is None:
        raise RegistryValidationError(
            f"model {model_id!r} has no registered baseline_throughput/baseline_runs to adjudicate "
            "against -- its harness was retired and must be re-registered with a fresh baseline",
            field=BASELINE_THROUGHPUT_FIELD,
        )
    baseline = _metric_baseline(model, model_id, ledger_values, stored_bar)
    # The bar is derived AT THE BASELINE LEVEL, never at the trial's own value: an arm that
    # could shrink its own bar by scoring well would be grading its own homework.
    rope = comparison_rope(model.meta, baseline_values(model.meta, ledger_values), baseline)
    direction = model.meta.get("direction")
    if direction == "minimize":
        delta = float(baseline) - observed_value
    elif direction == "maximize":
        delta = observed_value - float(baseline)
    else:
        raise RegistryValidationError(
            f"model direction must be 'minimize' or 'maximize', got {direction!r}", field="direction"
        )
    status = "succeeded" if delta > rope else "failed"
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
    harness mutation: it retires ``baseline_runs``/``baseline_throughput`` -- so there is
    no evidence left to compute a rope from -- marks the campaign :data:`STALLED`, and
    reverts whatever adoption is currently scored for this model through
    :func:`revert_adoption` -- which resets the ratchet counter and streak, re-queues the
    tenure's rejections, and restores ``previous_baseline`` so the re-registration really
    does happen at the baseline commit left standing after the reversion -- it does NOT
    fail; the model just refuses to adjudicate again until re-registered through
    :func:`register_model_with_baseline`. A patch that never touches a recorded harness
    value (including setting one for the first time) is an ordinary update.
    """
    model = space.get(model_id)
    if model is None:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")

    mutates_harness = any(
        field in HARNESS_FIELDS and field in model.meta and model.meta[field] != value
        for field, value in patch.items()
    )
    if mutates_harness:
        model.meta.pop(BASELINE_THROUGHPUT_FIELD, None)
        model.meta.pop(BASELINE_THROUGHPUT_UNITS_FIELD, None)
        model.meta.pop(BASELINE_RUNS_FIELD, None)
        model.meta[CAMPAIGN_STATUS_FIELD] = STALLED
        revert_adoption(space, model_id, "harness field mutation retired the baseline evidence")
    model.meta.update(patch)
    return model


# ---------------------------------------------------------------------------
# A BAR THAT SHRINKS AS THE CAMPAIGN APPROACHES ITS CEILING
#
# WHY. A rope is measured over replicates taken in the regime the incumbent was in -- and a
# campaign exists to LEAVE that regime. +1pp at 0.90 recall is not the same finding as +1pp
# at 0.74: it cuts the residual error by 10% rather than 3.8%. A bar held at the measured
# number therefore charges a campaign the SAME absolute price for a discovery that is worth
# several times more, and it charges it exactly where progress is hardest. So the adoption
# bar is allowed to SHRINK as the metric closes on its ceiling.
#
# WHAT THIS IS NOT, and the four ways the obvious versions of it are wrong:
#
#   1. NOT sqrt(p(1-p)/n). The binomial SE peaks at p=0.5 and shrinks only ABOVE it, so it
#      RAISES the bar for every campaign climbing through the low half. court_marking runs
#      0.155648 -> 0.70 and would be handed a bar 26% HIGHER at its win condition than at
#      registration -- the exact inversion of the intent. Residual-to-ceiling is monotone
#      over the whole range and for BOTH directions, which is why it is the shape used.
#   2. NOT a pure relative-error scaling. residual -> 0 makes the bar -> 0 and certifies
#      pure noise as a win: association at HOTA 0.99 scales to ~0.0002, BELOW the 0.000790
#      SD of a perturbation whose true effect is zero. :data:`DEFAULT_ROPE_ARMOR` is the
#      answer to that and is not optional.
#   3. The bar is derived AT A BASELINE LEVEL, never at the trial's own value -- an arm
#      that could shrink its own bar by scoring well is grading its own homework. Each
#      comparison uses the level of the bar it is being compared to, which is also what
#      makes the ratchet's counterfactual (verdict._attributable_to_the_adoption) able to
#      ask its question at the PREVIOUS baseline's era rather than the current one's.
#   4. The derived bar is BOUNDED to [armor x measured, measured]. A rope evaluated at a
#      future metric level is a number no ledger row constrains, so it inherits its
#      magnitude from the rope the baseline rows actually show instead of escaping it --
#      see :func:`comparison_rope`.
#
# WHAT PRAXIS CANNOT CHECK, said out loud rather than implied: that the measurement noise
# REALLY does shrink in proportion to the residual. praxis holds a ledger and a model
# record; it never ran the harness, and the replicates it recomputes from sit at one metric
# level. The proportionality is a MODEL the campaign asserts, and the stamp
# :data:`ROPE_SCALING_BASIS_FIELD` says so in as many words.

#: Opt-in. ABSENT means a static bar -- the recomputed rope at every metric level, exactly
#: as before this existed.
ROPE_SCALING_FIELD = "rope_scaling"
#: The measured rope is the bar at every metric level. The default.
ROPE_SCALING_STATIC = "static"
#: The bar scales with distance-to-ceiling, armored below. See this section's header.
ROPE_SCALING_RESIDUAL = "residual_to_ceiling"
KNOWN_ROPE_SCALINGS: frozenset[str] = frozenset({ROPE_SCALING_STATIC, ROPE_SCALING_RESIDUAL})

#: WHERE THE METRIC RUNS OUT. Declared, never assumed: a maximize metric is NOT always
#: bounded at 1 (mAP-style sums, counts, speedups are not), and a minimize metric's floor
#: is not always 0. Residual is measured to THIS number, so guessing it wrong silently
#: mis-scales every later bar.
METRIC_CEILING_FIELD = "metric_ceiling"
#: The metric level the rope's replicates sit at -- v_measured, the denominator of the
#: residual ratio. Stamped by :func:`register_model_with_baseline` as the mean of the
#: baseline runs; required to be declared on any other registration path, which has no
#: ledger to derive it from.
ROPE_MEASURED_AT_FIELD = "rope_measured_at"

# THE ARMOR: the fraction of the measured rope below which the bar may never fall,
# whatever the residual says.
#
# WHY IT EXISTS is defect 2 above: relative scaling alone has no lower bound and hands a
# campaign a bar below its own measured noise as soon as it gets close to the ceiling.
#
# WHY 0.5, and it is an empirical number rather than a taste: the one direct measurement
# this registry has of a TRUE-ZERO effect is association's perturbation study -- delta SD
# 0.000790 HOTA against a measured rope of 0.0016, i.e. 0.494 of the rope. An armor of
# 0.5 is the largest round shrinkage that still keeps that campaign's bar (0.0008) ABOVE
# the noise of a perturbation known to do nothing (0.000790). Below 0.5 the only measured
# null this registry holds starts adjudicating as a win.
#
# WHAT IT COSTS, stated because it is not free: if the noise does NOT shrink with the
# residual -- the assumption praxis cannot check -- a fully-armored bar is 0.5 sigma
# instead of `sigmas` sigma, so a null arm's one-sided false-adoption rate rises from
# ~16% (1 sigma) to ~31%. That is the price of the whole feature, paid only by campaigns
# that opt in and bounded by this constant. A campaign with its own null measurement
# should override it: `rope_armor` takes any fraction in (0, 1].
DEFAULT_ROPE_ARMOR = 0.5
ROPE_ARMOR_FIELD = "rope_armor"

#: WHAT THE REGISTRY ESTABLISHED about the scaling, stamped at registration so a later
#: reader never has to infer it.
ROPE_SCALING_BASIS_FIELD = "rope_scaling_basis"
#: no scaling declared; the bar is the recomputed rope.
ROPE_SCALING_BASIS_STATIC = "static_measured_rope"
#: the shape and its inputs are well-formed and bounded, and the PROPORTIONALITY ITSELF --
#: that the noise really shrinks with the residual -- was NOT verified here and cannot be:
#: praxis holds replicates at one metric level.
ROPE_SCALING_BASIS_UNVERIFIED_MODEL = "shape_checked_noise_model_unverified"


def _rope_scaling_mode(meta: dict[str, object]) -> str:
    declared = meta.get(ROPE_SCALING_FIELD)
    if declared in (None, ""):
        return ROPE_SCALING_STATIC
    return str(declared)


def _residual(direction: str, ceiling: float, value: float) -> float:
    """Distance from ``value`` to the ceiling, in the direction the metric improves."""
    return (ceiling - value) if direction == "maximize" else (value - ceiling)


def guard_rope_scaling(meta: dict[str, object]) -> str:
    """Check a declared rope scaling and return its :data:`ROPE_SCALING_BASIS_FIELD` stamp.

    Sits on the registration choke point next to :func:`guard_rope_provenance`, for the
    reason that one does: it is the one path every model write passes, including the plain
    ``praxis register-model`` CLI, and it is the last moment before trials start burning
    against a bar nobody can reconstruct.

    A model that declares NOTHING passes untouched and keeps a static bar -- that is the
    backward-compatible case and it is the majority of them. What is refused is a
    half-declaration: a scaling word outside the vocabulary (which would read to a later
    checker as no declaration at all, the failure mode ``baseline_throughput_units``
    learned), a scaling with no ceiling to measure residual to, a ceiling the metric has
    already reached or passed at the level the rope was measured (residual zero: every
    later bar would be 0/0 or armored flat, so the declaration is not describing this
    campaign), or an armor outside (0, 1] (above 1 the bar would GROW away from the
    measured rope, at or below 0 there is no armor at all -- which is defect 2).
    """
    mode = _rope_scaling_mode(meta)
    if mode not in KNOWN_ROPE_SCALINGS:
        raise RegistryValidationError(
            f"{ROPE_SCALING_FIELD} {meta.get(ROPE_SCALING_FIELD)!r} is not one of "
            f"{sorted(KNOWN_ROPE_SCALINGS)!r}; a word outside that vocabulary reads to every "
            "later checker as NO declaration, which silently restores the static bar this "
            "field was set to change",
            field=ROPE_SCALING_FIELD,
        )
    if mode == ROPE_SCALING_STATIC:
        return ROPE_SCALING_BASIS_STATIC

    direction = meta.get("direction")
    if direction not in ("minimize", "maximize"):
        raise RegistryValidationError(
            f"{ROPE_SCALING_FIELD}={mode!r} needs a direction to know which way the ceiling "
            f"lies, got direction={direction!r}",
            field="direction",
        )
    ceiling = meta.get(METRIC_CEILING_FIELD)
    if ceiling in (None, ""):
        raise RegistryValidationError(
            f"{ROPE_SCALING_FIELD}={mode!r} scales the bar by DISTANCE TO THE CEILING and no "
            f"{METRIC_CEILING_FIELD} is declared. It is not defaulted to 1.0 (or 0.0): a "
            "maximize metric is not always bounded at 1 -- a count, a speedup or an unnormalised "
            "sum is not -- and a wrong ceiling mis-scales every bar this campaign ever "
            "adjudicates without ever looking wrong",
            field=METRIC_CEILING_FIELD,
        )
    measured_at = meta.get(ROPE_MEASURED_AT_FIELD)
    if measured_at in (None, ""):
        raise RegistryValidationError(
            f"{ROPE_SCALING_FIELD}={mode!r} needs {ROPE_MEASURED_AT_FIELD}, the metric level "
            "the rope's replicates sit at -- it is the denominator of the residual ratio, so "
            "without it the scaling has no reference regime. Register through "
            "register_model_with_baseline and it is stamped from the mean of the baseline "
            "runs, or declare it",
            field=ROPE_MEASURED_AT_FIELD,
        )
    try:
        ceiling_f = float(ceiling)  # type: ignore[arg-type]
        measured_at_f = float(measured_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RegistryValidationError(
            f"{METRIC_CEILING_FIELD} {ceiling!r} and {ROPE_MEASURED_AT_FIELD} {measured_at!r} "
            "must both be numbers; the bar is a ratio of the distances between them",
            field=METRIC_CEILING_FIELD,
        ) from None
    if _residual(str(direction), ceiling_f, measured_at_f) <= 0.0:
        raise RegistryValidationError(
            f"{METRIC_CEILING_FIELD} {ceiling_f!r} is not beyond {ROPE_MEASURED_AT_FIELD} "
            f"{measured_at_f!r} in the {direction!r} direction, so the residual the rope was "
            "measured at is zero or negative and there is no regime to scale FROM. Either the "
            "ceiling is wrong or this campaign has already finished",
            field=METRIC_CEILING_FIELD,
        )
    armor = meta.get(ROPE_ARMOR_FIELD, DEFAULT_ROPE_ARMOR)
    try:
        armor_f = float(armor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RegistryValidationError(
            f"{ROPE_ARMOR_FIELD} {armor!r} is not a number; it is the FRACTION of the measured "
            "rope the bar may never fall below",
            field=ROPE_ARMOR_FIELD,
        ) from None
    if not (0.0 < armor_f <= 1.0):
        raise RegistryValidationError(
            f"{ROPE_ARMOR_FIELD} {armor_f!r} is outside (0, 1]. At or below 0 there is no armor "
            "at all and the bar falls to zero as the metric nears its ceiling, certifying pure "
            "noise as a win (association at HOTA 0.99 would scale to ~0.0002, below the 0.000790 "
            "SD of a perturbation whose true effect is zero). Above 1 the derived bar would rise "
            f"above the measured rope, which no ledger row constrains. Default "
            f"{DEFAULT_ROPE_ARMOR}",
            field=ROPE_ARMOR_FIELD,
        )
    return ROPE_SCALING_BASIS_UNVERIFIED_MODEL


def comparison_rope(
    meta: dict[str, object], values: Sequence[float], at_value: float
) -> float:
    """THE bar for ONE comparison: the rope measured over ``values`` -- the model's own
    baseline rows, read from the ledger at this moment -- evaluated at metric level
    ``at_value``.

    This is the whole of what R3a's retirement leaves: one number, derived from evidence,
    at the level of the comparison being made. There is no second threshold stored anywhere
    that could disagree with it.

    ``at_value`` is always a BASELINE level (the bar a trial is measured against), never
    the trial's own value; see this section's header, point 3. For a model that declared no
    scaling the level is irrelevant and the bar is the measured rope.

    THE MAGNITUDE GUARANTEE, and it is the whole of defect 4. A bar derived at a FUTURE
    metric level is a number no ledger row constrains, so the derived bar never leaves the
    measured rope's neighbourhood: it is clamped to ``[armor x measured, measured]``. The
    upper clamp means the bar can only ever SHRINK (a scaling that would raise it -- a
    campaign that has moved AWAY from its ceiling -- is capped at the number the rows
    actually show), and the lower clamp is the armor.
    """
    rope = measure_rope(values, sigmas=declared_sigmas(meta))
    if _rope_scaling_mode(meta) != ROPE_SCALING_RESIDUAL:
        return rope
    direction = str(meta["direction"])
    ceiling = float(meta[METRIC_CEILING_FIELD])  # type: ignore[arg-type]
    measured_at = float(meta[ROPE_MEASURED_AT_FIELD])  # type: ignore[arg-type]
    armor = float(meta.get(ROPE_ARMOR_FIELD, DEFAULT_ROPE_ARMOR))  # type: ignore[arg-type]
    ratio = _residual(direction, ceiling, at_value) / _residual(direction, ceiling, measured_at)
    return rope * min(1.0, max(armor, ratio))


def describe_rope(
    meta: dict[str, object], values: Sequence[float], at_value: float
) -> dict[str, object]:
    """The bar at ``at_value`` plus WHY it is that number -- for report/CLI surfaces that
    have to explain a moving bar to a human, and labelled with what was and was not
    verified rather than presented as measured fact."""
    mode = _rope_scaling_mode(meta)
    measured = measure_rope(values, sigmas=declared_sigmas(meta))
    bar = comparison_rope(meta, values, at_value)
    described: dict[str, object] = {
        "rope": bar,
        "measured_rope": measured,
        "at_value": at_value,
        ROPE_SCALING_FIELD: mode,
        ROPE_SCALING_BASIS_FIELD: meta.get(ROPE_SCALING_BASIS_FIELD, ROPE_SCALING_BASIS_STATIC),
    }
    if mode == ROPE_SCALING_RESIDUAL:
        armor = float(meta.get(ROPE_ARMOR_FIELD, DEFAULT_ROPE_ARMOR))  # type: ignore[arg-type]
        described["scale"] = bar / measured if measured else 1.0
        described["armored"] = bar <= measured * armor + AGREEMENT_TOLERANCE
        described["caveat"] = (
            "the bar is scaled by distance to a DECLARED ceiling; that the measurement noise "
            "really shrinks in proportion is a model this campaign asserts and praxis never "
            "verified -- it recomputes the noise from replicates at one metric level"
        )
    return described
