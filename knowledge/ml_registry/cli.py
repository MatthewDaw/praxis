"""Runnable entrypoint for the af-ml-research registry (R1 schema/guards, R2 write path,
R3 idea lifecycle, R4 query surface, R5 cross-project model linkage, R6 ideation axis sweep,
R10 trial verdict).

``python -m knowledge.ml_registry.cli <subcommand> ...`` -- exit 0 on acceptance, 1 on a
named registry refusal, 2 on malformed input. This is the real entrypoint later tickets
call into for their own decisions; it also gives the registry a runnable surface an
automated check can invoke rather than merely import.

The R2 ``register-*``/``readback`` subcommands persist a :class:`RegistrySpace` as JSON
at ``--space-file`` across separate process invocations, so a CLI-driven test can
register a model, then an idea against it, then a trial against that idea, then read
all three back -- the same sequence the write API supports in-process.

THE LEDGER IS THE ONLY ACCEPTANCE SIGNAL. Every subcommand that decides something about a
trial (``adjudicate-trial``, ``resolve-verdict``, ``supervise-campaign``) reads the
autoresearch loop's real ``results.tsv`` via ``--ledger`` and joins the trial's own commit
against it (:func:`load_ledger_rows`). No subcommand accepts a caller-supplied JSON blob
standing in for that file, and none SYNTHESIZES a ledger column it cannot read: a ledger
carrying no ``throughput``/``diff_lines`` column is REFUSED naming the missing column,
because inventing agreeing values for them silently disables the throughput void and the
net-line rejection. The same rule governs citations: ``resolve-citation`` will not take the
resolution outcome from its caller outside an explicit ``--test-resolver``.

DURABILITY. A mutating subcommand saves the space it mutated even when the run ends in a
refusal (:func:`_load_mutate_save` saves in a ``finally``), and ``supervise-campaign``
additionally checkpoints between dispatches, so a campaign that refuses on its 41st
dispatch keeps the 40 trials it really ran. Every counter the registry reports is
recomputed from that durable substrate, which only works if the substrate survives.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from typing import Callable, TypeVar

from knowledge.ml_registry.citation import Resolver, ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.cross_project import TicketIndex, model_to_projects, project_to_models
from knowledge.ml_registry.floor import adjudicate_trial, load_ledger_values, register_model_with_baseline, retire_harness
from knowledge.ml_registry.guards import guard_baseline_move, guard_model_mutation
from knowledge.ml_registry.ideate import (
    GENERATIVE_AXES,
    RETRIEVAL_AXES,
    RetrievalResult,
    always_confirm,
    seed_campaign,
)
from knowledge.ml_registry.lifecycle import (
    adopt_idea,
    claim_idea,
    flagged_trials,
    heartbeat_idea_claim,
    invalidate_adoption,
    is_retriable,
    park_idea,
    per_axis_yield,
    reject_idea,
    rejection_memory,
    untried_backlog,
)
from knowledge.ml_registry.schema import IDEA, MODEL, RegistryValidationError, validate_fact
from knowledge.ml_registry.supervisor import (
    Intervention,
    record_keep_pushing_marker,
    record_out_of_diff_change,
    supervise_campaign,
)
from knowledge.ml_registry.verdict import LedgerRow, adjudicate_verdict
from knowledge.ml_registry.write_path import (
    MAX_DISCOVERED_IDEAS_FIELD,
    METRIC_FIELD,
    MODEL_DEFAULTS,
    UNLIMITED_DISCOVERED_IDEAS,
    RegistrySpace,
    load_ledger_commits,
    mutate_model,
    register_idea,
    register_model,
    register_trial,
    resolve_idea_citation,
)

_T = TypeVar("_T")

# --- the external ledger (results.tsv) ---------------------------------------------------
# The autoresearch loop's ledger is read BY COLUMN NAME, so a ledger may carry the columns
# an adjudication needs without the CLI guessing at positions. ``commit`` plus a metric
# column (either the legacy ``val_bpb`` or the generic ``metric_value`` --
# agent_factory/scripts/checks/af_ml_research_target.py's two header versions) are what a
# value-only read needs; a VERDICT additionally needs the run's measured throughput and its
# net diff lines, which the loop must record per row.
LEDGER_COMMIT_COLUMN = "commit"
LEDGER_METRIC_COLUMNS: tuple[str, ...] = ("metric_value", "val_bpb")
LEDGER_THROUGHPUT_COLUMN = "throughput"
LEDGER_DIFF_LINES_COLUMN = "diff_lines"


# Sources a CLI caller may CLAIM. "adjudication" is deliberately absent: it is an
# in-process source that authorises a baseline move (guard_baseline_move), so letting a
# caller assert it at the command line would defeat that guard entirely.
CLI_CLAIMABLE_SOURCES = ("worker", "operator")

def load_ledger_rows(path: Path) -> dict[str, LedgerRow]:
    """``{commit: LedgerRow}`` read from the autoresearch loop's real ``results.tsv``.

    This is the ONLY source of a verdict's inputs. A ledger with no
    :data:`LEDGER_THROUGHPUT_COLUMN` or :data:`LEDGER_DIFF_LINES_COLUMN` is REFUSED naming
    the missing column rather than being papered over with synthesized values: a fabricated
    throughput equal to the model's baseline can never fall below the void floor, and a
    fabricated ``diff_lines`` of 0 can never breach ``diff_size_limit``, so synthesizing them
    turns two of :func:`~knowledge.ml_registry.verdict.adjudicate_verdict`'s four verdicts
    into dead code. The fix for a ledger that lacks them is to record them in the loop that
    writes it, not here.

    A row whose metric/throughput/diff_lines cell is empty, short, or non-numeric is an
    UNSCORED run (a crash or an abort) and is skipped individually -- the same tolerance
    :func:`~knowledge.ml_registry.floor.load_ledger_values` has for the same file. The commit
    is then simply absent, and whichever caller actually needed it refuses naming it.
    """
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = [column.strip() for column in next(reader)]
        except StopIteration:
            raise RegistryValidationError(
                f"external ledger {str(path)!r} is empty; it must carry a header row",
                field="ledger",
            ) from None

        def _column(name: str) -> int:
            if name not in header:
                raise RegistryValidationError(
                    f"external ledger {str(path)!r} carries no {name!r} column (header {header!r}); "
                    "a trial verdict is decided on the ledger's own measurements, so a ledger that "
                    "does not record this one cannot adjudicate -- add the column to the loop that "
                    "writes results.tsv",
                    field=name,
                )
            return header.index(name)

        commit_at = _column(LEDGER_COMMIT_COLUMN)
        metric_at = next((header.index(c) for c in LEDGER_METRIC_COLUMNS if c in header), None)
        if metric_at is None:
            raise RegistryValidationError(
                f"external ledger {str(path)!r} carries no metric column "
                f"(expected one of {LEDGER_METRIC_COLUMNS}, header {header!r})",
                field="metric",
            )
        throughput_at = _column(LEDGER_THROUGHPUT_COLUMN)
        diff_lines_at = _column(LEDGER_DIFF_LINES_COLUMN)
        widest = max(commit_at, metric_at, throughput_at, diff_lines_at)

        rows: dict[str, LedgerRow] = {}
        for row in reader:
            if len(row) <= widest or not row[commit_at].strip():
                continue
            try:
                rows[row[commit_at].strip()] = LedgerRow(
                    value=float(row[metric_at]),
                    throughput=float(row[throughput_at]),
                    diff_lines=float(row[diff_lines_at]),
                )
            except ValueError:
                continue  # unscored run (crashed/aborted): nothing to adjudicate against
        return rows


def _checked_model_budgets(meta: dict[str, object], *, fill_missing: bool) -> dict[str, object]:
    """``meta`` with its campaign budgets (:data:`MODEL_DEFAULTS`) checked, and -- on a fresh
    registration -- defaulted.

    ``setdefault`` alone is not enough: it fills only a MISSING key, so an explicit
    ``{"max_discovered_ideas": null}`` survives into the model fact, and a budget that is
    null, blank, unparseable or negative must never be read as "unlimited" or crash a
    campaign mid-run with a bare ``TypeError``. A null/blank budget is a budget the caller
    did not state, so it takes the documented default; anything unparseable or non-positive
    is a NAMED refusal. Unlimited discovered ideas stay reachable only through the explicit
    :data:`UNLIMITED_DISCOVERED_IDEAS` sentinel.

    ``fill_missing=False`` is the update path: an omitted budget means "leave this model's
    budget alone", never "reset it to the default".
    """
    checked = dict(meta)
    for field_name, default in MODEL_DEFAULTS.items():
        if field_name not in checked:
            if fill_missing:
                checked[field_name] = default
            continue
        raw = checked[field_name]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            checked[field_name] = default
            continue
        try:
            value: object = float(raw) if field_name == "per_trial_seconds" else int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise RegistryValidationError(
                f"campaign budget {field_name}={raw!r} is not a number; omit it to take the "
                f"default {default!r}",
                field=field_name,
            ) from None
        if field_name == MAX_DISCOVERED_IDEAS_FIELD:
            if int(value) < 0 and int(value) != UNLIMITED_DISCOVERED_IDEAS:  # type: ignore[arg-type]
                raise RegistryValidationError(
                    f"campaign budget {field_name}={value} is not a budget; use "
                    f"{UNLIMITED_DISCOVERED_IDEAS} to ask for unlimited discovered ideas explicitly",
                    field=field_name,
                )
        elif float(value) <= 0:  # type: ignore[arg-type]
            raise RegistryValidationError(
                f"campaign budget {field_name}={value} is not a budget; it must be positive",
                field=field_name,
            )
        checked[field_name] = value
    return checked


def _update_registered_model(
    space: RegistrySpace, model_id: str, patch: dict[str, object], *, source: str | None
) -> str:
    """Re-register (update) an ALREADY REGISTERED model through the GUARDED write path.

    ``register_model(..., model_id=...)`` replaces the fact wholesale: it froze only
    ``metric``, ran neither R1 guard, and dropped every derived campaign field the model had
    accumulated (``campaign_status``, ``ratchet_count``, ``rejection_streak_ideas``, the
    keep-pushing markers) -- the very state the registry recomputes its counters from. This
    path instead MERGES the caller's keys onto the existing meta through
    :func:`~knowledge.ml_registry.write_path.mutate_model`, so both guards sit on the data
    path (a worker-sourced patch cannot touch a judging field; ``baseline`` moves only from
    adjudication) and derived state survives. ``metric`` stays frozen for the model's life.
    """
    if not (source or "").strip():
        raise RegistryValidationError(
            "updating an already-registered model is a guarded mutation: name its --source "
            "(e.g. 'worker' or 'adjudication')",
            field="source",
        )
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")
    if METRIC_FIELD in patch and patch[METRIC_FIELD] != model.meta.get(METRIC_FIELD):
        raise RegistryValidationError(
            f"model {model_id!r} metric is frozen for the life of the model; cannot change it from "
            f"{model.meta.get(METRIC_FIELD)!r} to {patch[METRIC_FIELD]!r}",
            field=METRIC_FIELD,
        )
    validate_fact(MODEL, {**model.meta, **patch})
    mutate_model(space, model_id, patch, source=str(source))
    return model_id


def _refuse_a_campaign_with_no_floor(space: RegistrySpace, model_id: str) -> None:
    """Refuse to spend a dispatch on a model that cannot be adjudicated.

    A model whose harness was retired (:func:`~knowledge.ml_registry.floor.retire_harness`)
    has no ``baseline_throughput``/``noise_floor`` until it is re-registered with a fresh
    4-run baseline, and ``verdict.adjudicate_verdict`` reads both by bare subscript -- so
    without this the campaign dispatches a real worker session and then dies on a ``KeyError``
    that neither of ``main``'s handlers catches. Refused BEFORE any compute is spent, naming
    the missing field.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(f"model {model_id!r} was never registered", field="model_id")
    for missing in ("baseline_throughput", "noise_floor"):
        if model.meta.get(missing) is None:
            raise RegistryValidationError(
                f"model {model_id!r} has no registered {missing} to adjudicate against "
                f"(campaign_status={model.meta.get('campaign_status')!r}); re-register it with "
                "register-model-with-baseline before running a campaign",
                field=missing,
            )


def _json_arg(raw: str) -> dict[str, object]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_intervention(raw: str) -> Intervention:
    kind, _, axis = raw.partition(":")
    if not axis:
        raise ValueError(f"--intervention must be 'kind:axis', got {raw!r}")
    return Intervention(kind=kind, axis=axis)


def _load_mutate_save(space_file: str, fn: Callable[[RegistrySpace], _T]) -> _T:
    """Load the space at ``space_file``, apply a single mutation, save, and return its result.

    The save happens in a ``finally``: a refusal raised part-way through a MULTI-write run
    (``supervise-campaign`` is forty dispatches, not one) must not discard everything the run
    already durably decided. The registry recomputes its counters and streaks from the space
    on resume, which is only safe if the space is really there -- a run that dispatched 40
    trials and then refused must leave 40 trials on disk, not zero. The refusal still exits
    non-zero; it just no longer erases the evidence.
    """
    space_path = Path(space_file)
    space = RegistrySpace.load(space_path)
    try:
        return fn(space)
    finally:
        space.save(space_path)


def _fixed_outcome_resolver(outcome: str, title: str, authors: tuple[str, ...]) -> Resolver:
    """A TEST resolver that reports one caller-declared outcome for this one attempt.

    Reachable only behind ``--test-resolver``. The resolution outcome (does this reference
    exist? who wrote it?) is a finding about the outside world, so taking it from the caller
    is exactly the self-report the registry refuses everywhere else: ``--outcome resolved``
    with a made-up title would otherwise record ``basis="external"`` for a reference nobody
    ever fetched. The CLI has no network, so a real arXiv/DOI lookup belongs to the service
    that calls :func:`~knowledge.ml_registry.write_path.resolve_idea_citation` in-process
    with its own resolver; this stub exists to exercise that write path offline.
    """

    def resolver(reference: str) -> ResolvedCitation | None:
        if outcome == "unreachable":
            raise ResolverUnreachable(reference)
        if outcome == "non-existent":
            return None
        return ResolvedCitation(title=title, authors=authors)

    return resolver


def _test_resolver_or_refuse(args: argparse.Namespace) -> Resolver:
    """The resolver ``resolve-citation`` may use, or a named refusal.

    ``--outcome`` is unavailable outside ``--test-resolver``, and a ``resolved`` outcome must
    carry a non-empty ``--title`` and at least one ``--author`` -- a "resolved" citation with
    no title is a fabricated external basis, and the help text promising the title was
    required never enforced it.
    """
    if not args.test_resolver:
        raise RegistryValidationError(
            "resolve-citation has no live resolver: this entrypoint cannot fetch a reference, and "
            "the resolution outcome is never taken from the caller. Call "
            "write_path.resolve_idea_citation in-process with a real resolver, or pass "
            "--test-resolver to drive the fixed stub deliberately",
            field="resolver",
        )
    if args.outcome is None:
        raise RegistryValidationError(
            "--test-resolver needs the outcome it should report: pass --outcome", field="outcome"
        )
    if args.outcome == "resolved":
        if not args.title.strip():
            raise RegistryValidationError(
                "--outcome=resolved records an external basis, so it requires a non-empty --title",
                field="title",
            )
        if not [a for a in args.author if a.strip()]:
            raise RegistryValidationError(
                "--outcome=resolved records an external basis, so it requires at least one --author",
                field="authors",
            )
    return _fixed_outcome_resolver(args.outcome, args.title, tuple(args.author))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap_p = sub.add_parser(
        "bootstrap-campaign",
        help="check a project's ledger, measure its noise floor, and emit a schema-valid model "
             "meta plus seeded ideas -- the systematic half of standing a campaign up")
    bootstrap_p.add_argument("--ledger", required=True)
    bootstrap_p.add_argument("--backlog", required=True,
                             help="JSONL; each record needs id + axis and a description or "
                                  "hypothesis(+basis)")
    bootstrap_p.add_argument("--model-id", required=True)
    bootstrap_p.add_argument("--metric", required=True)
    bootstrap_p.add_argument("--direction", required=True, choices=["maximize", "minimize"])
    bootstrap_p.add_argument("--diff-size-limit", type=int, required=True)
    bootstrap_p.add_argument("--baseline-prefix", default="baseline",
                             help="ledger rows whose description starts with this are baselines")
    bootstrap_p.add_argument("--sigmas", type=float, default=2.0)
    bootstrap_p.add_argument("--noise-floor", type=float, default=None,
                             help="override the floor measured from the ledger; use when it was "
                                  "measured over MORE runs than the ledger holds")
    bootstrap_p.add_argument("--skip-ids", default="",
                             help="comma-separated backlog ids to omit (settled losers)")
    bootstrap_p.add_argument("--notes", default=None)
    bootstrap_p.add_argument("--out-dir", default=None,
                             help="write model_meta.json and ideas.jsonl here")

    validate_p = sub.add_parser("validate-fact", help="validate a fact against its category schema")
    validate_p.add_argument("--category", required=True)
    validate_p.add_argument("--meta-json", required=True)

    mutate_p = sub.add_parser(
        "guard-model-mutation", help="check a patch against a registered model's protected fields"
    )
    mutate_p.add_argument("--patch-json", required=True)
    mutate_p.add_argument("--source", required=True)

    baseline_p = sub.add_parser(
        "guard-baseline-move", help="check whether a patch may move a model's baseline"
    )
    baseline_p.add_argument("--patch-json", required=True)
    baseline_p.add_argument("--source", required=True)

    register_model_p = sub.add_parser("register-model", help="register a model fact")
    register_model_p.add_argument("--space-file", required=True)
    register_model_p.add_argument("--meta-json", required=True)
    register_model_p.add_argument(
        "--model-id",
        default=None,
        help="update an already-registered model instead of creating a new one -- a GUARDED, "
        "MERGING mutation (both R1 guards apply; derived campaign state survives), so it needs --source",
    )
    register_model_p.add_argument(
        "--source",
        default=None,
        choices=CLI_CLAIMABLE_SOURCES,
        help="who is making the change -- required with --model-id, ignored on a fresh registration. "
        "'adjudication' is NOT claimable here: it is an in-process source that authorises a baseline "
        "move, and a CLI caller asserting it would defeat guard_baseline_move",
    )

    mutate_model_p = sub.add_parser(
        "mutate-model",
        help="apply a patch to an ALREADY REGISTERED model through the guarded write path "
        "(both R1 guards applied on the data path)",
    )
    mutate_model_p.add_argument("--space-file", required=True)
    mutate_model_p.add_argument("--model-id", required=True)
    mutate_model_p.add_argument("--patch-json", required=True)
    mutate_model_p.add_argument("--source", required=True, choices=CLI_CLAIMABLE_SOURCES)

    register_idea_p = sub.add_parser("register-idea", help="register an idea fact")
    register_idea_p.add_argument("--space-file", required=True)
    register_idea_p.add_argument("--meta-json", required=True)

    register_trial_p = sub.add_parser("register-trial", help="register a trial fact")
    register_trial_p.add_argument("--space-file", required=True)
    register_trial_p.add_argument("--meta-json", required=True)
    register_trial_p.add_argument(
        "--ledger", required=True, help="path to the autoresearch loop's results.tsv"
    )

    register_baseline_p = sub.add_parser(
        "register-model-with-baseline",
        help="register a model, recomputing noise_floor/baseline_throughput from 4 ledger-named baseline_runs (R12)",
    )
    register_baseline_p.add_argument("--space-file", required=True)
    register_baseline_p.add_argument("--meta-json", required=True)
    register_baseline_p.add_argument("--ledger", required=True, help="path to the autoresearch loop's results.tsv")
    register_baseline_p.add_argument("--model-id", default=None)

    adjudicate_p = sub.add_parser(
        "adjudicate-trial",
        help="decide a trial's status on a single LEDGER-sourced observed value (R12)",
    )
    adjudicate_p.add_argument("--space-file", required=True)
    adjudicate_p.add_argument("--trial-id", required=True)
    adjudicate_p.add_argument(
        "--ledger", required=True,
        help="path to the autoresearch loop's results.tsv -- the value adjudicated is the one "
        "recorded there for the trial's own commit",
    )
    adjudicate_p.add_argument(
        "--observed-value", type=float, default=None,
        help="OPTIONAL self-reported value; it is CHECKED against the ledger row and refused on "
        "disagreement, never used as the decision input",
    )

    verdict_p = sub.add_parser(
        "resolve-verdict",
        help="decide and apply a trial's full table-driven verdict against the model's current "
        "baseline -- adopt/park/reject/void, with the 3-consecutive-rejection ratchet (R10)",
    )
    verdict_p.add_argument("--space-file", required=True)
    verdict_p.add_argument("--trial-id", required=True)
    verdict_p.add_argument(
        "--ledger", required=True,
        help="path to the autoresearch loop's results.tsv -- it must carry the "
        f"{LEDGER_THROUGHPUT_COLUMN!r} and {LEDGER_DIFF_LINES_COLUMN!r} columns a verdict is decided on",
    )
    verdict_p.add_argument("--reactivation-trigger", default=None)

    retire_harness_p = sub.add_parser(
        "retire-harness",
        help="apply a patch to a model's harness fields, retiring the noise floor and reverting its "
        "active adoption if the patch mutates a recorded harness field (R12)",
    )
    retire_harness_p.add_argument("--space-file", required=True)
    retire_harness_p.add_argument("--model-id", required=True)
    retire_harness_p.add_argument("--patch-json", required=True)

    resolve_p = sub.add_parser(
        "resolve-citation", help="resolve a registered idea's reference (R7)"
    )
    resolve_p.add_argument("--space-file", required=True)
    resolve_p.add_argument("--idea-id", required=True)
    resolve_p.add_argument("--reference", required=True)
    resolve_p.add_argument(
        "--test-resolver",
        action="store_true",
        help="drive the FIXED stub resolver from --outcome instead of a real lookup. For tests: "
        "the resolution outcome is a finding about the outside world and is otherwise never "
        "taken from the caller",
    )
    resolve_p.add_argument(
        "--outcome",
        default=None,
        choices=["resolved", "non-existent", "unreachable"],
        help="what the stub resolver reports for this attempt -- requires --test-resolver",
    )
    resolve_p.add_argument("--title", default="", help="resolved title, required when --outcome=resolved")
    resolve_p.add_argument(
        "--author", action="append", default=[],
        help="resolved author, repeatable, at least one required when --outcome=resolved",
    )

    readback_p = sub.add_parser("readback", help="read back every fact in the space")
    readback_p.add_argument("--space-file", required=True)
    readback_p.add_argument("--category", choices=["model", "idea", "trial"], default=None)

    register_ticket_p = sub.add_parser(
        "register-ticket", help="index a project ticket by its meta (R5 cross-project linkage)"
    )
    register_ticket_p.add_argument("--index-file", required=True)
    register_ticket_p.add_argument("--project", required=True)
    register_ticket_p.add_argument("--ticket-id", required=True)
    register_ticket_p.add_argument("--meta-json", required=True)

    m2p_p = sub.add_parser(
        "model-to-projects", help="every project whose ticket references this experiment_id"
    )
    m2p_p.add_argument("--index-file", required=True)
    m2p_p.add_argument("--space-file", required=True)
    m2p_p.add_argument("--experiment-id", required=True)

    p2m_p = sub.add_parser(
        "project-to-models", help="every registered model a project's tickets reference"
    )
    p2m_p.add_argument("--index-file", required=True)
    p2m_p.add_argument("--space-file", required=True)
    p2m_p.add_argument("--project", required=True)

    claim_p = sub.add_parser("claim-idea", help="claim (or reclaim, if stale) an idea's lease")
    claim_p.add_argument("--space-file", required=True)
    claim_p.add_argument("--idea-id", required=True)
    claim_p.add_argument("--owner", required=True)
    claim_p.add_argument("--ttl", type=int, default=None)
    claim_p.add_argument("--now", type=float, default=None)

    heartbeat_p = sub.add_parser("heartbeat-idea-claim", help="bump a held idea claim's heartbeat")
    heartbeat_p.add_argument("--space-file", required=True)
    heartbeat_p.add_argument("--idea-id", required=True)
    heartbeat_p.add_argument("--owner", required=True)
    heartbeat_p.add_argument("--now", type=float, default=None)

    adopt_p = sub.add_parser("adopt-idea", help="adopt an idea from one of its own succeeded trials")
    adopt_p.add_argument("--space-file", required=True)
    adopt_p.add_argument("--idea-id", required=True)
    adopt_p.add_argument("--trial-id", required=True)

    park_p = sub.add_parser("park-idea", help="park an idea with a reactivation trigger")
    park_p.add_argument("--space-file", required=True)
    park_p.add_argument("--idea-id", required=True)
    park_p.add_argument("--trigger", required=True)

    reject_p = sub.add_parser("reject-idea", help="reject an idea, naming a reason")
    reject_p.add_argument("--space-file", required=True)
    reject_p.add_argument("--idea-id", required=True)
    reject_p.add_argument("--reason", required=True)

    invalidate_p = sub.add_parser("invalidate-adoption", help="revert an adoption, naming a reason")
    invalidate_p.add_argument("--space-file", required=True)
    invalidate_p.add_argument("--idea-id", required=True)
    invalidate_p.add_argument("--reason", required=True)

    backlog_p = sub.add_parser("backlog", help="the untried-idea backlog")
    backlog_p.add_argument("--space-file", required=True)
    backlog_p.add_argument("--model-id", default=None)
    backlog_p.add_argument("--now", type=float, default=None)

    rejection_memory_p = sub.add_parser("rejection-memory", help="every rejected idea, with its reason")
    rejection_memory_p.add_argument("--space-file", required=True)
    rejection_memory_p.add_argument("--model-id", default=None)

    flagged_trials_p = sub.add_parser("flagged-trials", help="trials derived from a since-rejected idea")
    flagged_trials_p.add_argument("--space-file", required=True)

    per_axis_yield_p = sub.add_parser(
        "per-axis-yield", help="attempt and adoption counts per idea axis and per idea origin"
    )
    per_axis_yield_p.add_argument("--space-file", required=True)
    per_axis_yield_p.add_argument("--model-id", default=None)

    retriable_p = sub.add_parser(
        "retriable-ideas", help="parked ideas whose reactivation_trigger is among the fired triggers"
    )
    retriable_p.add_argument("--space-file", required=True)
    retriable_p.add_argument("--fired-trigger", action="append", default=[])

    supervise_p = sub.add_parser(
        "supervise-campaign",
        help="drive one model's campaign to close, dispatching one worker per trial serially (R8)",
    )
    supervise_p.add_argument("--space-file", required=True)
    supervise_p.add_argument("--model-id", required=True)
    supervise_p.add_argument("--ledger", required=True, help="path to the autoresearch loop's results.tsv")
    supervise_p.add_argument(
        "--dispatch-script", required=True,
        help="JSON file: a list of trial-meta objects, one per worker session, consumed in dispatch order",
    )
    supervise_p.add_argument(
        "--idea-script", default=None,
        help="JSON file: a list of idea-meta objects consumed, in order, whenever the backlog is empty",
    )
    supervise_p.add_argument(
        "--intervention", action="append", default=[],
        help="kind:axis, repeatable -- e.g. forced_axis:data or exclude_axis:architecture",
    )
    supervise_p.add_argument("--max-dispatches", type=int, default=None)
    supervise_p.add_argument(
        "--lesson-file", default=None,
        help="append each CONFIRMED cross-model lesson payload to this file, one JSON object per "
        "line (R17). The cross-model gate runs either way; without this the payloads are only "
        "reported in the outcome",
    )

    keep_pushing_p = sub.add_parser(
        "record-keep-pushing-marker",
        help="durably record the ONLY suppression of R9's rabbit-hole axis-switch intervention "
        "for one axis of one model",
    )
    keep_pushing_p.add_argument("--space-file", required=True)
    keep_pushing_p.add_argument("--model-id", required=True)
    keep_pushing_p.add_argument("--axis", required=True)
    keep_pushing_p.add_argument("--author", required=True, help="who is choosing to keep pushing this axis")

    out_of_diff_p = sub.add_parser(
        "record-out-of-diff-change",
        help="durably record a code change landed OUTSIDE any trial's own diff, resetting R9's "
        "trailing non-improving count once",
    )
    out_of_diff_p.add_argument("--space-file", required=True)
    out_of_diff_p.add_argument("--model-id", required=True)
    out_of_diff_p.add_argument("--author", required=True, help="who landed the out-of-diff change")

    seed_p = sub.add_parser(
        "seed-campaign",
        help="seed a model's starting idea set by sweeping the nine-axis closed set (R6)",
    )
    seed_p.add_argument("--space-file", required=True)
    seed_p.add_argument("--model-id", required=True)
    seed_p.add_argument(
        "--mode", choices=["batch", "interactive"], default="batch",
        help="batch auto-confirms every candidate; interactive consumes --confirm-script in order",
    )
    seed_p.add_argument(
        "--generator-script", required=True,
        help="JSON file: {axis: [candidate_meta, ...]} for each of the six generative axes",
    )
    seed_p.add_argument(
        "--retriever-script", required=True,
        help="JSON file: {axis: {query: str, rows: [{id, description, ...}, ...]}} for each of "
        "the three retrieval axes",
    )
    seed_p.add_argument(
        "--confirm-script", default=None,
        help="JSON file: a list of booleans, consumed in order across every candidate offered -- "
        "required in --mode=interactive, ignored in --mode=batch",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "bootstrap-campaign":
            from pathlib import Path as _P

            from knowledge.ml_registry.bootstrap import bootstrap as _bootstrap

            backlog = [json.loads(l) for l in _P(args.backlog).read_text().splitlines() if l.strip()]
            report = _bootstrap(
                ledger=_P(args.ledger), backlog=backlog, model_id=args.model_id,
                metric=args.metric, direction=args.direction,
                diff_size_limit=args.diff_size_limit, baseline_prefix=args.baseline_prefix,
                sigmas=args.sigmas, noise_floor_override=args.noise_floor,
                skip_ids={i for i in args.skip_ids.split(",") if i}, notes=args.notes)
            if args.out_dir and report.ready:
                out = _P(args.out_dir); out.mkdir(parents=True, exist_ok=True)
                (out / "model_meta.json").write_text(json.dumps(report.model_meta, indent=2))
                (out / "ideas.jsonl").write_text(
                    "\n".join(json.dumps(i) for i in report.ideas) + "\n")
            print(json.dumps(report.to_dict(), indent=2))
            # A campaign that cannot be adjudicated must not look like a success.
            return 0 if report.ready else 1

        if args.command == "validate-fact":
            meta = _json_arg(args.meta_json)
            validate_fact(args.category, meta)
            print(f"OK: {args.category} fact is well-formed")
            return 0
        if args.command == "guard-model-mutation":
            patch = _json_arg(args.patch_json)
            guard_model_mutation(patch, source=args.source)
            print("OK: mutation allowed")
            return 0
        if args.command == "guard-baseline-move":
            patch = _json_arg(args.patch_json)
            guard_baseline_move(patch, source=args.source)
            print("OK: baseline move allowed")
            return 0
        if args.command == "register-model":
            if args.model_id is None:
                meta = _checked_model_budgets(_json_arg(args.meta_json), fill_missing=True)
                fact_id = _load_mutate_save(args.space_file, lambda space: register_model(space, meta))
                print(f"OK: registered model {fact_id}")
                return 0
            patch = _checked_model_budgets(_json_arg(args.meta_json), fill_missing=False)
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: _update_registered_model(space, args.model_id, patch, source=args.source),
            )
            print(f"OK: registered model {fact_id}")
            return 0
        if args.command == "mutate-model":
            fact = _load_mutate_save(
                args.space_file,
                lambda space: mutate_model(
                    space, args.model_id, _json_arg(args.patch_json), source=args.source
                ),
            )
            print(json.dumps(fact.to_json()))
            return 0
        if args.command == "register-idea":
            fact_id = _load_mutate_save(
                args.space_file, lambda space: register_idea(space, _json_arg(args.meta_json))
            )
            print(f"OK: registered idea {fact_id}")
            return 0
        if args.command == "register-trial":
            ledger_commits = load_ledger_commits(Path(args.ledger))
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_trial(space, _json_arg(args.meta_json), ledger_commits),
            )
            print(f"OK: registered trial {fact_id}")
            return 0
        if args.command == "register-model-with-baseline":
            ledger_values = load_ledger_values(Path(args.ledger))
            meta = _checked_model_budgets(_json_arg(args.meta_json), fill_missing=True)
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_model_with_baseline(
                    space, meta, ledger_values, model_id=args.model_id
                ),
            )
            print(f"OK: registered model {fact_id}")
            return 0
        if args.command == "adjudicate-trial":
            # The value adjudicated comes from the ledger row for the trial's own commit;
            # --observed-value, when given, is only a claim checked against it.
            ledger_values = load_ledger_values(Path(args.ledger))
            status = _load_mutate_save(
                args.space_file,
                lambda space: adjudicate_trial(
                    space, args.trial_id, ledger_values, self_reported_value=args.observed_value
                ),
            )
            print(f"OK: trial {args.trial_id} adjudicated {status}")
            return 0
        if args.command == "resolve-verdict":
            ledger_rows = load_ledger_rows(Path(args.ledger))
            kwargs = {"reactivation_trigger": args.reactivation_trigger} if args.reactivation_trigger else {}
            verdict = _load_mutate_save(
                args.space_file,
                lambda space: adjudicate_verdict(space, args.trial_id, ledger_rows, **kwargs),
            )
            print(f"OK: trial {args.trial_id} verdict {verdict}")
            return 0
        if args.command == "retire-harness":
            fact = _load_mutate_save(
                args.space_file, lambda space: retire_harness(space, args.model_id, _json_arg(args.patch_json))
            )
            print(json.dumps(fact.to_json()))
            return 0
        if args.command == "resolve-citation":
            resolver = _test_resolver_or_refuse(args)
            meta = _load_mutate_save(
                args.space_file,
                lambda space: resolve_idea_citation(space, args.idea_id, args.reference, resolver),
            )
            print(json.dumps(meta))
            return 0
        if args.command == "readback":
            space = RegistrySpace.load(Path(args.space_file))
            facts = space.list_facts(args.category)
            print(json.dumps([f.to_json() for f in facts]))
            return 0
        if args.command == "register-ticket":
            index_path = Path(args.index_file)
            index = TicketIndex.load(index_path)
            index.add(args.project, args.ticket_id, _json_arg(args.meta_json))
            index.save(index_path)
            print(f"OK: indexed ticket {args.ticket_id} for project {args.project}")
            return 0
        if args.command == "model-to-projects":
            space = RegistrySpace.load(Path(args.space_file))
            index = TicketIndex.load(Path(args.index_file))
            projects = model_to_projects(space, index, args.experiment_id)
            print(json.dumps(projects))
            return 0
        if args.command == "project-to-models":
            space = RegistrySpace.load(Path(args.space_file))
            index = TicketIndex.load(Path(args.index_file))
            models = project_to_models(space, index, args.project)
            print(json.dumps(models))
            return 0
        if args.command == "claim-idea":
            kwargs = {k: v for k, v in (("ttl", args.ttl), ("now", args.now)) if v is not None}
            claimed = _load_mutate_save(
                args.space_file, lambda space: claim_idea(space, args.idea_id, args.owner, **kwargs)
            )
            print(json.dumps({"claimed": claimed}))
            return 0 if claimed else 1
        if args.command == "heartbeat-idea-claim":
            kwargs = {"now": args.now} if args.now is not None else {}
            ok = _load_mutate_save(
                args.space_file, lambda space: heartbeat_idea_claim(space, args.idea_id, args.owner, **kwargs)
            )
            print(json.dumps({"heartbeat": ok}))
            return 0 if ok else 1
        if args.command == "adopt-idea":
            _load_mutate_save(args.space_file, lambda space: adopt_idea(space, args.idea_id, args.trial_id))
            print(f"OK: adopted {args.idea_id}")
            return 0
        if args.command == "park-idea":
            _load_mutate_save(args.space_file, lambda space: park_idea(space, args.idea_id, args.trigger))
            print(f"OK: parked {args.idea_id}")
            return 0
        if args.command == "reject-idea":
            _load_mutate_save(args.space_file, lambda space: reject_idea(space, args.idea_id, args.reason))
            print(f"OK: rejected {args.idea_id}")
            return 0
        if args.command == "invalidate-adoption":
            _load_mutate_save(
                args.space_file, lambda space: invalidate_adoption(space, args.idea_id, args.reason)
            )
            print(f"OK: invalidated adoption of {args.idea_id}")
            return 0
        if args.command == "retriable-ideas":
            space = RegistrySpace.load(Path(args.space_file))
            fired = set(args.fired_trigger)
            retriable = [f for f in space.list_facts(IDEA) if is_retriable(f, fired)]
            print(json.dumps([f.to_json() for f in retriable]))
            return 0
        if args.command == "backlog":
            space = RegistrySpace.load(Path(args.space_file))
            kwargs = {"model_id": args.model_id} if args.model_id is not None else {}
            if args.now is not None:
                kwargs["now"] = args.now
            print(json.dumps([f.to_json() for f in untried_backlog(space, **kwargs)]))
            return 0
        if args.command == "rejection-memory":
            space = RegistrySpace.load(Path(args.space_file))
            kwargs = {"model_id": args.model_id} if args.model_id is not None else {}
            print(json.dumps(
                [{"idea": f.to_json(), "reason": reason} for f, reason in rejection_memory(space, **kwargs)]
            ))
            return 0
        if args.command == "flagged-trials":
            space = RegistrySpace.load(Path(args.space_file))
            print(json.dumps([f.to_json() for f in flagged_trials(space)]))
            return 0
        if args.command == "per-axis-yield":
            space = RegistrySpace.load(Path(args.space_file))
            kwargs = {"model_id": args.model_id} if args.model_id is not None else {}
            print(json.dumps(per_axis_yield(space, **kwargs)))
            return 0
        if args.command == "supervise-campaign":
            # The REAL ledger, with the throughput/diff_lines a verdict is decided on. A
            # ledger that does not carry them is refused (load_ledger_rows) rather than
            # having them invented here.
            ledger_rows = load_ledger_rows(Path(args.ledger))
            dispatch_script = list(json.loads(Path(args.dispatch_script).read_text()))
            idea_script = (
                list(json.loads(Path(args.idea_script).read_text())) if args.idea_script else []
            )
            interventions = tuple(_parse_intervention(raw) for raw in args.intervention)

            dispatch_iter = iter(dispatch_script)
            idea_iter = iter(idea_script)
            space_path = Path(args.space_file)
            lesson_path = Path(args.lesson_file) if args.lesson_file else None
            filed_lessons: list[dict[str, object]] = []

            def dispatcher(space, model, idea):  # noqa: ANN001 -- matches supervisor.Dispatcher
                # Checkpoint BEFORE this worker session runs: everything the previous
                # dispatch adjudicated is already durable, so an abort mid-campaign costs at
                # most the one trial in flight (see _load_mutate_save's finally-save).
                space.save(space_path)
                try:
                    return next(dispatch_iter)
                except StopIteration as exc:
                    raise RegistryValidationError(
                        "dispatch-script exhausted before the campaign closed", field="dispatch_script"
                    ) from exc

            def idea_generator(space, model_id, forced_axis, permitted_axes):  # noqa: ANN001
                return next(idea_iter, None)

            def lesson_filer(payload: dict[str, object]) -> dict[str, object]:
                """R17's filing seam, wired from the shipped entrypoint.

                Without this the cross-model gate ran but nothing it confirmed ever left the
                process, so guarantee 8's filing path was unreachable from the CLI.
                """
                if lesson_path is not None:
                    with lesson_path.open("a") as fh:
                        fh.write(json.dumps(payload) + "\n")
                filed = {"filed": len(filed_lessons), "payload": payload}
                filed_lessons.append(filed)
                return filed

            def _supervise(space: RegistrySpace) -> dict[str, object]:
                _refuse_a_campaign_with_no_floor(space, args.model_id)
                return supervise_campaign(
                    space, args.model_id, ledger_rows, dispatcher,
                    interventions=interventions, idea_generator=idea_generator,
                    max_dispatches=args.max_dispatches, lesson_filer=lesson_filer,
                )

            outcome = _load_mutate_save(args.space_file, _supervise)
            outcome["lessons_filed"] = filed_lessons
            print(json.dumps(outcome, default=lambda o: {"kind": o.kind, "axis": o.axis}))
            return 0
        if args.command == "record-keep-pushing-marker":
            _load_mutate_save(
                args.space_file,
                lambda space: record_keep_pushing_marker(space, args.model_id, args.axis, args.author),
            )
            print(f"OK: keep-pushing marker recorded for {args.model_id} on axis {args.axis}")
            return 0
        if args.command == "record-out-of-diff-change":
            _load_mutate_save(
                args.space_file,
                lambda space: record_out_of_diff_change(space, args.model_id, args.author),
            )
            print(f"OK: out-of-diff change recorded for {args.model_id}")
            return 0
        if args.command == "seed-campaign":
            generator_script = json.loads(Path(args.generator_script).read_text())
            retriever_script = json.loads(Path(args.retriever_script).read_text())
            confirm_script = (
                list(json.loads(Path(args.confirm_script).read_text())) if args.confirm_script else []
            )
            confirm_iter = iter(confirm_script)

            def generator(axis: str, model_meta: dict[str, object]) -> list[dict[str, object]]:
                return [dict(c) for c in generator_script.get(axis, [])]

            def retriever(axis: str, model_meta: dict[str, object]) -> RetrievalResult:
                entry = retriever_script.get(axis, {})
                rows = tuple(dict(r) for r in entry.get("rows", []))
                return RetrievalResult(query=str(entry.get("query", "")), rows=rows)

            def interactive_confirm(axis: str, candidate: dict[str, object]) -> bool:
                try:
                    return bool(next(confirm_iter))
                except StopIteration as exc:
                    raise RegistryValidationError(
                        "confirm-script exhausted before every candidate was confirmed",
                        field="confirm_script",
                    ) from exc

            confirm = always_confirm if args.mode == "batch" else interactive_confirm

            def _seed(space: RegistrySpace) -> dict[str, object]:
                run = seed_campaign(
                    space, args.model_id, generator=generator, retriever=retriever, confirm=confirm
                )
                return {
                    "written": run.written,
                    "receipts": [r.to_meta() for r in run.receipts],
                    "generative_axes": list(GENERATIVE_AXES),
                    "retrieval_axes": list(RETRIEVAL_AXES),
                }

            outcome = _load_mutate_save(args.space_file, _seed)
            print(json.dumps(outcome))
            return 0
    except RegistryValidationError as exc:
        print(f"REFUSED [{exc.field}]: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2

    return 2  # pragma: no cover - argparse's `required=True` makes this unreachable


if __name__ == "__main__":
    sys.exit(main())
