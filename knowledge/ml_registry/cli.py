"""Monolithic command line for the canonical standard registry and IDEA bridge.

Live experiment, run, artifact, registered-model, model-version, lineage, alias, status,
and finalization commands open :class:`storage.Registry` through ``--registry-root``.
``RegistrySpace`` remains permanently authoritative for IDEA inventory and adjudication
metadata; those bridge commands alone use ``--space-file``. Historical tabular evidence is
accepted only by the explicitly named import commands and is emitted only by ``export-runs``.

The pre-cutover parser and handlers remain private in this module until their underlying
legacy modules can be removed in a later witnessed deletion. They are intentionally not
reachable through the public parser.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from typing import Callable, TypeVar

from knowledge.ml_registry.citation import Resolver, ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.contracts.ledger_v2 import (
    read_ledger_compatibility,
    read_ledger_compatibility_header,
)
from knowledge.ml_registry.cross_project import TicketIndex, model_to_projects, project_to_models
from knowledge.ml_registry.floor import (DEFAULT_SIGMAS, adjudicate_trial, load_ledger_values,
                                          register_model_with_baseline, retire_harness)
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
    reopen_idea,
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
from knowledge.ml_registry.completeness import campaign_completeness
from knowledge.ml_registry.report import (acknowledge_diagnosis, campaign_status,
                                          format_status)
from knowledge.ml_registry.verdict import LedgerRow, adjudicate_verdict, reset_ratchet
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
    supersede_trial,
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

    A duplicated commit key among FAIR rows is REFUSED naming it rather than
    last-write-winning -- see :func:`~knowledge.ml_registry.floor.load_ledger_values`, which
    refuses the same shape for the same file, and skips the same unfair one: a row outside
    :data:`~knowledge.ml_registry.verdict.FAIR_RUN_STATUSES` is not a competing measurement
    of a run, so its re-run under the same key is legitimate and the last FAIR row wins.

    A row whose metric/throughput/diff_lines cell is empty, short, or non-numeric is an
    UNSCORED run (a crash or an abort) and is skipped individually -- the same tolerance
    :func:`~knowledge.ml_registry.floor.load_ledger_values` has for the same file. The commit
    is then simply absent, and whichever caller actually needed it refuses naming it.
    """
    header_projection = read_ledger_compatibility_header(path)
    header = list(header_projection.columns)
    if not header_projection.has_header:
        raise RegistryValidationError(
            f"external ledger {str(path)!r} is empty; it must carry a header row",
            field="ledger",
        )

    def _require(name: str) -> None:
        if name not in header:
            raise RegistryValidationError(
                f"external ledger {str(path)!r} carries no {name!r} column (header {header!r}); "
                "a trial verdict is decided on the ledger's own measurements, so a ledger that "
                "does not record this one cannot adjudicate -- add the column to the loop that "
                "writes results.tsv",
                field=name,
            )

    _require(LEDGER_COMMIT_COLUMN)
    if not any(column in header for column in LEDGER_METRIC_COLUMNS):
        raise RegistryValidationError(
            f"external ledger {str(path)!r} carries no metric column "
            f"(expected one of {LEDGER_METRIC_COLUMNS}, header {header!r})",
            field="metric",
        )
    _require(LEDGER_THROUGHPUT_COLUMN)
    _require(LEDGER_DIFF_LINES_COLUMN)
    # Header refusal deliberately happens before the body is consumed.  A malformed body
    # cannot mask the more actionable missing-column error that the former reader raised first.
    projection = read_ledger_compatibility(path)
    if projection.duplicate_fair_commits:
        raise RegistryValidationError(
            f"external ledger {str(path)!r} carries more than one scored row for "
            f"{list(projection.duplicate_fair_commits)!r}; a verdict joins a trial to its row BY THIS KEY, so a "
            "repeat silently adjudicates whichever run was written LAST. Write "
            "'{sha}:{arm_tag}' so a campaign that varies arms by CONFIG still gets one key "
            "per run.",
            field=LEDGER_COMMIT_COLUMN,
        )
    return {
        key: LedgerRow(
            value=row.metric_value,
            throughput=float(row.throughput),
            diff_lines=float(row.diff_lines),
            status=row.status,
        )
        for key, row in projection.measurements.items()
        if row.throughput is not None and row.diff_lines is not None
    }


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
    """A JSON object, given either literally or as a path to a file containing one.

    Accepting the path is not sugar, it is the seam between the two halves of the documented
    workflow. `bootstrap-campaign` WRITES `model_meta.json` and `ideas.jsonl`, and every
    `--meta-json` example in af-ml-supervise/SKILL.md is spelled `<meta>.json` -- but this only
    ever parsed a literal string, so following the documented sequence failed on the first
    register-model-with-baseline with `MALFORMED INPUT: Expecting value: line 1 column 1`, which
    names neither the argument nor the reason. A caller writing a tempfile and passing its path
    (the composing-campaign pattern the same skill recommends) hit it too.

    Disambiguation is by leading brace, not by trying json.loads first and falling back: a
    fallback would report a FILE error for a malformed literal and a PARSE error for a missing
    file, which is the wrong message in each case.
    """
    stripped = raw.strip()
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
    else:
        path = Path(stripped)
        if not path.is_file():
            raise ValueError(
                f"{stripped!r} is neither a JSON object (it does not start with '{{') nor an "
                f"existing file")
        parsed = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _parse_intervention(raw: str) -> Intervention:
    kind, _, axis = raw.partition(":")
    if not axis:
        raise ValueError(f"--intervention must be 'kind:axis', got {raw!r}")
    return Intervention(kind=kind, axis=axis)


_LOCK_TIMEOUT_ENV = "ML_REGISTRY_LOCK_TIMEOUT"
_DEFAULT_LOCK_TIMEOUT_SECONDS = 900.0


def _lock_timeout_seconds(override: float | None = None) -> float:
    """How long to wait for the space lock before refusing, in seconds.

    900s (15 minutes) because the two populations are orders of magnitude apart and the default
    only has to separate them. Every ordinary mutation -- register, adjudicate, acknowledge,
    complete -- is a load, an in-memory edit and a save of a JSON file: milliseconds, and it is
    bounded by no external work at all, so no legitimate short mutation comes within three orders
    of magnitude of tripping this. The thing on the other side is ``supervise-campaign``, which
    holds the lock across real training runs and can hold it for hours; waiting a quarter of an
    hour before concluding "something else owns this" is generous even against a slow dispatch,
    and still returns an answer inside one coffee break rather than never.

    ``ML_REGISTRY_LOCK_TIMEOUT`` overrides it (seconds; ``0`` means fail immediately on
    contention), and an explicit argument to :func:`_load_mutate_save` overrides that.
    """
    if override is not None:
        return float(override)
    raw = os.environ.get(_LOCK_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(
            f"{_LOCK_TIMEOUT_ENV}={raw!r} is not a number of seconds") from None
    if seconds < 0:
        raise ValueError(f"{_LOCK_TIMEOUT_ENV}={raw!r} must not be negative")
    return seconds


def _stamp_the_holder(lock) -> None:  # noqa: ANN001 - an open file object
    """Record who holds the lock, so a waiter that times out can name it rather than guess.

    Written only while the lock is HELD, so there is exactly one writer and the line a waiter
    reads is either the current holder's or absent. It is best-effort by construction: a holder
    killed with SIGKILL leaves its line behind, so :func:`_describe_the_holder` presents it as
    the last known holder rather than as fact. Cheap, and it does not touch the locking itself.
    """
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()} {' '.join(sys.argv)}\n")
        lock.flush()
    except OSError:  # pragma: no cover - a stamp is a nicety, never a reason to refuse a mutation
        pass


def _describe_the_holder(lock_path: Path) -> str:
    """The holder's pid and command line, as recorded by :func:`_stamp_the_holder`."""
    try:
        stamp = lock_path.read_text().strip()
    except OSError:  # pragma: no cover - we just failed to lock it, so it exists
        stamp = ""
    return f"last recorded holder: {stamp}" if stamp else "the holder did not record itself"


def _acquire_or_refuse(lock, lock_path: Path, timeout_seconds: float | None) -> None:  # noqa: ANN001
    """Take LOCK_EX, polling, and refuse by name once the budget is spent.

    Polling a non-blocking ``flock`` rather than arming ``SIGALRM``: the CLI runs inside other
    people's processes (``main`` is imported and called directly by tests and by the supervising
    skill), and installing a process-wide signal handler there would be a far larger blast radius
    than a 50ms poll. Contention is rare and the loser is about to wait minutes anyway.
    """
    import fcntl

    budget = _lock_timeout_seconds(timeout_seconds)
    deadline = time.monotonic() + budget
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            waited = time.monotonic() - (deadline - budget)
            if time.monotonic() >= deadline:
                raise RegistryValidationError(
                    f"could not lock {lock_path} after waiting {waited:.0f}s: another process "
                    f"still holds it ({_describe_the_holder(lock_path)}). A supervise-campaign "
                    "run holds this lock for the WHOLE campaign, so this is expected while one is "
                    "in flight -- wait for it to finish, or give each campaign a separate space "
                    f"file so they do not contend. Raise {_LOCK_TIMEOUT_ENV} (seconds, currently "
                    f"{budget:g}) to wait longer.",
                    field="space_file",
                ) from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _load_mutate_save(space_file: str, fn: Callable[[RegistrySpace], _T],
                      timeout_seconds: float | None = None) -> _T:
    """Load, apply ONE mutation, save — holding an exclusive lock for the whole cycle.

    THE LOCK IS THE POINT. Without it this is a textbook lost update: two commands each load the
    space, each mutate their own in-memory copy, and whichever saves last silently discards the
    other's work. Nothing errors and nothing warns; the write simply is not there afterwards.

    Measured on a live campaign. A supervising loop registered trials while an operator command
    acknowledged a diagnosis against the same space file. Afterwards the acknowledgement was
    absent, and four adjudicated trials -- including the campaign's only ADOPTION -- had no record
    at all, despite the loop having printed their verdicts. The ledger rows existed; the trials did
    not. That is the worst possible shape for a registry whose entire purpose is to be the record
    of what was decided: it kept reporting verdicts it had already lost, and the loop then re-ran
    those arms because from the registry's view they had never happened.

    The lock is a separate `.lock` file rather than the space itself, because the save path
    replaces the file and a lock held on a replaced inode protects nothing.

    The save still happens in a ``finally``: a refusal raised part-way through a MULTI-write run
    (``supervise-campaign`` is forty dispatches, not one) must not discard everything the run
    already durably decided.

    The wait is BOUNDED (:func:`_lock_timeout_seconds`). ``supervise-campaign`` holds this lock
    for an entire campaign -- forty dispatches, potentially hours -- so a second command against
    the same space file used to block on a plain blocking ``flock`` with no timeout and no output.
    Contention was then indistinguishable from a crash: the operator sees a command that prints
    nothing and never returns, and the only way to tell the two apart is to go reading /proc. The
    scope of the lock is correct and is deliberately unchanged; what is fixed is that exceeding
    the budget now REFUSES loudly, naming the lock file, the holder, and the remedy.
    """
    space_path = Path(space_file)
    lock_path = space_path.with_suffix(space_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # NOT "w": truncating on open would erase the CURRENT holder's identity line before we even
    # ask for the lock, which is precisely the information a waiter needs to report on timeout.
    with open(lock_path, "a+") as lock:
        _acquire_or_refuse(lock, lock_path, timeout_seconds)
        try:
            _stamp_the_holder(lock)
            space = RegistrySpace.load(space_path)
            try:
                return fn(space)
            finally:
                space.save(space_path)
        finally:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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


def _idea_bridge_main(argv: list[str] | None = None) -> int:
    """Permanent RegistrySpace bridge for IDEA inventory, claims, citations, and staging."""
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    register_p = sub.add_parser("register-idea", help="register an idea fact")
    register_p.add_argument("--space-file", required=True)
    register_p.add_argument("--meta-json", required=True)
    resolve_p = sub.add_parser("resolve-citation", help="resolve a registered idea's reference (R7)")
    resolve_p.add_argument("--space-file", required=True)
    resolve_p.add_argument("--idea-id", required=True)
    resolve_p.add_argument("--reference", required=True)
    resolve_p.add_argument("--test-resolver", action="store_true")
    resolve_p.add_argument("--outcome", choices=["resolved", "non-existent", "unreachable"])
    resolve_p.add_argument("--title", default="")
    resolve_p.add_argument("--author", action="append", default=[])
    readback_p = sub.add_parser("readback", help="read back every fact in the space")
    readback_p.add_argument("--space-file", required=True)
    readback_p.add_argument("--category", choices=["model", "idea", "trial"])

    claim_p = sub.add_parser("claim-idea", help="claim (or reclaim, if stale) an idea's lease")
    claim_p.add_argument("--space-file", required=True)
    claim_p.add_argument("--idea-id", required=True)
    claim_p.add_argument("--owner", required=True)
    claim_p.add_argument("--ttl", type=int)
    claim_p.add_argument("--now", type=float)
    heartbeat_p = sub.add_parser("heartbeat-idea-claim", help="bump a held idea claim's heartbeat")
    heartbeat_p.add_argument("--space-file", required=True)
    heartbeat_p.add_argument("--idea-id", required=True)
    heartbeat_p.add_argument("--owner", required=True)
    heartbeat_p.add_argument("--now", type=float)

    for name, option, help_text in (
        ("adopt-idea", "--trial-id", "adopt an idea from one of its own succeeded trials"),
        ("park-idea", "--trigger", "park an idea with a reactivation trigger"),
        ("reject-idea", "--reason", "reject an idea, naming a reason"),
        ("invalidate-adoption", "--reason", "revert an adoption, naming a reason"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--space-file", required=True)
        command.add_argument("--idea-id", required=True)
        command.add_argument(option, required=True)
    reopen_p = sub.add_parser("reopen-idea", help="return an idea to UNTRIED after an unfair verdict")
    reopen_p.add_argument("--space-file", required=True)
    reopen_p.add_argument("--idea-id", required=True)
    reopen_p.add_argument("--reason", required=True)
    reopen_p.add_argument("--json", action="store_true")

    backlog_p = sub.add_parser("backlog", help="the untried-idea backlog")
    backlog_p.add_argument("--space-file", required=True)
    backlog_p.add_argument("--model-id")
    backlog_p.add_argument("--now", type=float)
    rejection_p = sub.add_parser("rejection-memory", help="every rejected idea, with its reason")
    rejection_p.add_argument("--space-file", required=True)
    rejection_p.add_argument("--model-id")
    retriable_p = sub.add_parser("retriable-ideas", help="parked ideas whose trigger has fired")
    retriable_p.add_argument("--space-file", required=True)
    retriable_p.add_argument("--fired-trigger", action="append", default=[])

    seed_p = sub.add_parser("seed-campaign", help="seed a model's starting idea set")
    seed_p.add_argument("--space-file", required=True)
    seed_p.add_argument("--model-id", required=True)
    seed_p.add_argument("--mode", choices=["batch", "interactive"], default="batch")
    seed_p.add_argument("--generator-script", required=True)
    seed_p.add_argument("--retriever-script", required=True)
    seed_p.add_argument("--confirm-script")

    args = parser.parse_args(argv)
    try:
        if args.command == "register-idea":
            fact_id = _load_mutate_save(
                args.space_file, lambda space: register_idea(space, _json_arg(args.meta_json))
            )
            print(f"OK: registered idea {fact_id}")
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
            print(json.dumps([fact.to_json() for fact in space.list_facts(args.category)]))
            return 0
        if args.command == "claim-idea":
            kwargs = {key: value for key, value in (("ttl", args.ttl), ("now", args.now))
                      if value is not None}
            claimed = _load_mutate_save(
                args.space_file, lambda space: claim_idea(space, args.idea_id, args.owner, **kwargs)
            )
            print(json.dumps({"claimed": claimed}))
            return 0 if claimed else 1
        if args.command == "heartbeat-idea-claim":
            kwargs = {"now": args.now} if args.now is not None else {}
            heartbeat = _load_mutate_save(
                args.space_file,
                lambda space: heartbeat_idea_claim(space, args.idea_id, args.owner, **kwargs),
            )
            print(json.dumps({"heartbeat": heartbeat}))
            return 0 if heartbeat else 1
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
        if args.command == "reopen-idea":
            outcome = _load_mutate_save(
                args.space_file, lambda space: reopen_idea(space, args.idea_id, args.reason)
            )
            print(json.dumps(outcome) if args.json
                  else f"OK: reopened {outcome['idea_id']} (was {outcome['previous_status']})")
            return 0
        if args.command == "backlog":
            space = RegistrySpace.load(Path(args.space_file))
            kwargs = {"model_id": args.model_id} if args.model_id is not None else {}
            if args.now is not None:
                kwargs["now"] = args.now
            print(json.dumps([fact.to_json() for fact in untried_backlog(space, **kwargs)]))
            return 0
        if args.command == "rejection-memory":
            space = RegistrySpace.load(Path(args.space_file))
            kwargs = {"model_id": args.model_id} if args.model_id is not None else {}
            print(json.dumps([
                {"idea": fact.to_json(), "reason": reason}
                for fact, reason in rejection_memory(space, **kwargs)
            ]))
            return 0
        if args.command == "retriable-ideas":
            space = RegistrySpace.load(Path(args.space_file))
            fired = set(args.fired_trigger)
            print(json.dumps([
                fact.to_json() for fact in space.list_facts(IDEA) if is_retriable(fact, fired)
            ]))
            return 0
        if args.command == "seed-campaign":
            generator_script = json.loads(Path(args.generator_script).read_text())
            retriever_script = json.loads(Path(args.retriever_script).read_text())
            confirmations = (list(json.loads(Path(args.confirm_script).read_text()))
                             if args.confirm_script else [])
            confirmation_iter = iter(confirmations)

            def generator(axis: str, model_meta: dict[str, object]) -> list[dict[str, object]]:
                return [dict(candidate) for candidate in generator_script.get(axis, [])]

            def retriever(axis: str, model_meta: dict[str, object]) -> RetrievalResult:
                entry = retriever_script.get(axis, {})
                return RetrievalResult(str(entry.get("query", "")),
                                       tuple(dict(row) for row in entry.get("rows", [])))

            def interactive_confirm(axis: str, candidate: dict[str, object]) -> bool:
                try:
                    return bool(next(confirmation_iter))
                except StopIteration as exc:
                    raise RegistryValidationError(
                        "confirm-script exhausted before every candidate was confirmed",
                        field="confirm_script",
                    ) from exc

            confirm = always_confirm if args.mode == "batch" else interactive_confirm

            def seed_space(space: RegistrySpace) -> dict[str, object]:
                run = seed_campaign(space, args.model_id, generator=generator,
                                    retriever=retriever, confirm=confirm)
                return {"written": run.written,
                        "receipts": [receipt.to_meta() for receipt in run.receipts],
                        "generative_axes": list(GENERATIVE_AXES),
                        "retrieval_axes": list(RETRIEVAL_AXES)}

            print(json.dumps(_load_mutate_save(args.space_file, seed_space)))
            return 0
    except RegistryValidationError as exc:
        print(f"REFUSED [{exc.field}]: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover


def _legacy_main(argv: list[str] | None = None) -> int:
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
    bootstrap_p.add_argument(
        "--sigmas", type=float, default=DEFAULT_SIGMAS,
        help="multiples of the measured dispersion the noise floor is (default: "
             "%(default)s). One sigma is the standing default and a deliberate trade -- a null "
             "arm clears it 15.9%% of the time one-sided vs 2.3%% at two sigma; see "
             "knowledge.ml_registry.bootstrap for what that buys. Pass 2 for the tighter bar.")
    bootstrap_p.add_argument(
        "--sigmas-reason", default=None,
        help="why this campaign chose that bar. Optional, never required -- but it is what "
             "campaign-status shows an operator who finds the campaign running a loose one.")
    bootstrap_p.add_argument(
        "--noise-floor-sigma", type=float, default=None,
        help="the ONE-SIGMA dispersion a --noise-floor was multiplied up from. Supplying it is "
             "what lets the registry CHECK that the stored floor really is the sigmas it "
             "declares; without it the declaration is stored and stamped unverified.")
    # REQUIRED, and deliberately so. The former default was the bare adoption string, which
    # `parse_win_condition` maps to WIN_ON_ADOPTION -- `win_condition_met` then returns True
    # unconditionally (supervisor.py:262) and the campaign closes as WON on its FIRST adopted
    # trial (supervisor.py:698), with every other declared stage untouched. An automatic default
    # is a guess about what winning means for a campaign the registry knows nothing about, so
    # there is no safe one to pick. Accepts a JSON object -- {"metric_at_least": 0.92} or
    # {"metric_at_most": 0.80}.
    bootstrap_p.add_argument(
        "--win-condition", required=True,
        help='JSON object naming an explicit numeric target, e.g. \'{"metric_at_least": 0.92}\' '
             'or \'{"metric_at_most": 0.80}\'. Required: the old default closed a campaign on '
             'its first adoption.')
    bootstrap_p.add_argument(
        "--noise-floor-method", default=None,
        help="How --noise-floor was measured, e.g. 'bootstrap'. A DECLARED method lets a supplied "
             "floor stand where recomputing from repeated baseline runs would give a degenerate "
             "one -- a deterministic incumbent repeats bit-identically, so its stdev is exactly 0.")
    bootstrap_p.add_argument(
        "--noise-floor-varies", default=None,
        choices=["eval_sample", "run_repeat", "paired_delta"],
        help="WHAT VARIED when the floor was measured: which eval items were scored "
             "(eval_sample), repeats of one fixed config (run_repeat), or arm-minus-baseline "
             "deltas on identical data (paired_delta). Paired trials cancel eval_sample noise, "
             "so that pairing is refused at registration.")
    bootstrap_p.add_argument(
        "--trial-comparison", default=None, choices=["paired", "unpaired"],
        help="whether each arm is scored on the SAME eval draw as the registered baseline row "
             "(paired) or its own (unpaired). praxis cannot infer this from the ledger.")
    bootstrap_p.add_argument("--noise-floor", type=float, default=None,
                             help="override the floor measured from the ledger; use when it was "
                                  "measured over MORE runs than the ledger holds")
    bootstrap_p.add_argument("--skip-ids", default="",
                             help="comma-separated backlog ids to omit (settled losers)")
    bootstrap_p.add_argument("--notes", default=None)
    bootstrap_p.add_argument(
        "--void-throughput-fraction", type=float, default=None,
        help="override the VOID gate (default 0.05). 0 disables it -- for campaigns whose "
             "throughput is not training speed and must not discard a slower winner")
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
    supersede_p = sub.add_parser(
        "supersede-trial",
        help="mark an IN-FLIGHT trial superseded so its idea can accept a new one -- the "
             "deliberate escape hatch for the one-trial-in-flight rule, for a run that died "
             "without resolving. Refuses to touch a trial that already has a verdict.",
    )
    supersede_p.add_argument("--space-file", required=True)
    supersede_p.add_argument("--trial-id", required=True)
    supersede_p.add_argument(
        "--reason", required=True,
        help="why this trial is being abandoned. REQUIRED: a trial dropped without a stated "
             "reason is indistinguishable from one quietly discarded for losing, and the "
             "dead-ideas register depends on telling those apart")

    register_trial_p.add_argument(
        "--json", action="store_true",
        help="emit {\"trial_id\": ...} instead of prose. The trial id is MINTED here and it is "
             "what resolve-verdict --trial-id needs -- it is NOT the idea id, and scraping the "
             "prose line was the only other way to learn it")

    status_p = sub.add_parser(
        "campaign-status",
        help="read-only summary of a campaign: baseline, per-status idea counts, IN-FLIGHT "
             "trials, and the ratchet. Safe against a live run -- it never mutates the space.",
    )
    status_p.add_argument("--space-file", required=True)
    status_p.add_argument("--model-id", required=True)
    status_p.add_argument("--json", action="store_true")

    ack_p = sub.add_parser(
        "acknowledge-diagnosis",
        help="record that a blocking diagnosis has been REMEDIATED so the loop may proceed. NOT a "
             "mute: it fires again the moment a NEW void of that kind appears, so acknowledging "
             "a cause you did not fix buys exactly one more arm.",
    )
    ack_p.add_argument("--space-file", required=True)
    ack_p.add_argument("--model-id", required=True)
    ack_p.add_argument("--kind", required=True,
                       help="e.g. budget_too_small, void_gate_too_tight")
    ack_p.add_argument("--reason", required=True,
                       help="what was actually changed. REQUIRED: an acknowledgement without one "
                            "is indistinguishable from silencing an inconvenient blocker")
    ack_p.add_argument("--json", action="store_true")

    complete_p = sub.add_parser(
        "campaign-complete",
        help="is the campaign FINISHED? Exits 0 when every declared phase is populated and "
             "closed, no arm awaits a re-run, and the winner has been trained to convergence. "
             "Exits 1 listing every reason it is not. An empty queue is not a finished campaign.",
    )
    complete_p.add_argument("--space-file", required=True)
    complete_p.add_argument("--model-id", required=True)
    complete_p.add_argument(
        "--stages", required=True,
        help="comma-separated phase names IN ORDER, e.g. "
             "representation,architecture,augmentation,training,tuning,capacity")
    complete_p.add_argument("--min-measured", type=int, default=3)
    complete_p.add_argument(
        "--artifact-store",
        help="canonical ArtifactStore root containing the PromotionRecord; required for "
             "production completion unless convergence is explicitly waived",
    )
    complete_p.add_argument(
        "--no-require-convergence", action="store_true",
        help="a campaign that only ever meant to SELECT, never to ship. Deliberate, not a default")
    complete_p.add_argument("--json", action="store_true")

    reopen_p = sub.add_parser(
        "reopen-idea",
        help="return an idea to UNTRIED after a verdict that was never fairly earned -- a run "
             "truncated by a wall clock, killed, or otherwise unfair. NOT for relitigating a "
             "verdict you dislike; the prior verdict is preserved, not erased.",
    )
    reopen_p.add_argument("--space-file", required=True)
    reopen_p.add_argument("--idea-id", required=True)
    reopen_p.add_argument(
        "--reason", required=True,
        help="why the prior verdict was not fairly earned. REQUIRED: without it, reopening is "
             "indistinguishable from quietly deleting an inconvenient result")
    reopen_p.add_argument("--json", action="store_true")

    reset_ratchet_p = sub.add_parser(
        "reset-ratchet",
        help="clear a model's rejection streak WITHOUT touching its baseline or any verdict -- "
             "for a stage boundary, where later arms vary something the adoption never competed "
             "against. The registry cannot see stage boundaries; this is the caller's call.",
    )
    reset_ratchet_p.add_argument("--space-file", required=True)
    reset_ratchet_p.add_argument("--model-id", required=True)
    reset_ratchet_p.add_argument(
        "--reason", required=True,
        help="why the streak no longer bears on the last adoption. REQUIRED: a ratchet cleared "
             "without a stated reason is indistinguishable from one cleared to protect a result "
             "someone liked")
    reset_ratchet_p.add_argument("--json", action="store_true")

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
    adjudicate_p.add_argument("--json", action="store_true", help="emit the result as JSON instead of prose. Prefer this from any program: the prose line is for humans and its wording is not a contract, so a caller that scrapes it (rsplit on the last token, say) breaks the day it is reworded")

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
    verdict_p.add_argument("--json", action="store_true", help="emit the result as JSON instead of prose. Prefer this from any program: the prose line is for humans and its wording is not a contract, so a caller that scrapes it (rsplit on the last token, say) breaks the day it is reworded")

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

            backlog = [json.loads(line) for line in _P(args.backlog).read_text().splitlines()
                       if line.strip()]
            report = _bootstrap(
                ledger=_P(args.ledger), backlog=backlog, model_id=args.model_id,
                metric=args.metric, direction=args.direction,
                diff_size_limit=args.diff_size_limit, baseline_prefix=args.baseline_prefix,
                sigmas=args.sigmas, sigmas_reason=args.sigmas_reason,
                noise_floor_sigma=args.noise_floor_sigma,
                noise_floor_override=args.noise_floor,
                noise_floor_method=args.noise_floor_method,
                noise_floor_varies=args.noise_floor_varies,
                trial_comparison=args.trial_comparison,
                win_condition=_json_arg(args.win_condition),
                skip_ids={i for i in args.skip_ids.split(",") if i}, notes=args.notes,
                void_throughput_fraction=args.void_throughput_fraction)
            if args.out_dir and report.ready:
                out = _P(args.out_dir)
                out.mkdir(parents=True, exist_ok=True)
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
            meta = _json_arg(args.meta_json)
            try:
                rows = load_ledger_rows(Path(args.ledger))
            except RegistryValidationError:
                ledger_commits = load_ledger_commits(Path(args.ledger))
            else:
                ledger_commits = frozenset(rows)
                row = rows.get(str(meta.get("commit")))
                if row is not None:
                    meta.setdefault("throughput", row.throughput)
                    meta.setdefault("diff_lines", row.diff_lines)
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_trial(space, meta, ledger_commits),
            )
            if getattr(args, "json", False):
                print(json.dumps({"trial_id": fact_id}))
            else:
                print(f"OK: registered trial {fact_id}")
            return 0
        if args.command == "supersede-trial":
            trial_id = _load_mutate_save(
                args.space_file,
                lambda space: supersede_trial(space, args.trial_id, args.reason),
            )
            print(f"OK: superseded trial {trial_id}")
            return 0
        if args.command == "campaign-status":
            # Load WITHOUT the mutate-and-save wrapper: this is read-only by design, so it is
            # safe to run against a campaign that is mid-arm. Saving here would race the loop.
            status = campaign_status(RegistrySpace.load(Path(args.space_file)), args.model_id)
            print(json.dumps(status, indent=2) if args.json else format_status(status))
            return 0
        if args.command == "acknowledge-diagnosis":
            out = _load_mutate_save(
                args.space_file,
                lambda space: acknowledge_diagnosis(space, args.model_id, args.kind, args.reason),
            )
            print(json.dumps(out) if args.json
                  else f"OK: acknowledged {out['kind']} at {out['void_count_at_ack']} void(s); "
                       f"it will fire again on the next NEW void of this kind")
            return 0
        if args.command == "campaign-complete":
            from knowledge.ml_registry.storage import ArtifactStore

            out = campaign_completeness(
                RegistrySpace.load(Path(args.space_file)), args.model_id,
                tuple(x.strip() for x in args.stages.split(",") if x.strip()),
                min_measured=args.min_measured,
                require_convergence=not args.no_require_convergence,
                promotion_source=(ArtifactStore(args.artifact_store)
                                  if args.artifact_store else None))
            if args.json:
                print(json.dumps(out, indent=2))
            elif out["done"]:
                if args.no_require_convergence:
                    print("CAMPAIGN COMPLETE: every phase populated and closed, nothing awaiting "
                          "re-run; convergence waived (--no-require-convergence)")
                else:
                    print("CAMPAIGN COMPLETE: every phase populated and closed, nothing awaiting "
                          "re-run, winner trained to convergence")
            else:
                for b in out["blocking"]:
                    where = f" [{b['stage']}]" if b["stage"] else ""
                    print(f"NOT DONE ({b['kind']}){where}: {b['detail']}")
            return 0 if out["done"] else 1
        if args.command == "reopen-idea":
            out = _load_mutate_save(
                args.space_file,
                lambda space: reopen_idea(space, args.idea_id, args.reason),
            )
            print(json.dumps(out) if args.json
                  else f"OK: reopened {out['idea_id']} (was {out['previous_status']})")
            return 0
        if args.command == "reset-ratchet":
            cleared = _load_mutate_save(
                args.space_file,
                lambda space: reset_ratchet(space, args.model_id, args.reason),
            )
            if args.json:
                print(json.dumps(cleared))
            else:
                print(f"OK: cleared ratchet_count {cleared['ratchet_count']} "
                      f"({len(cleared['rejection_streak_ideas'])} idea(s)) for {args.model_id}")
            return 0
        if args.command == "register-model-with-baseline":
            meta = _checked_model_budgets(_json_arg(args.meta_json), fill_missing=True)
            try:
                rows = load_ledger_rows(Path(args.ledger))
            except RegistryValidationError:
                ledger_values = load_ledger_values(Path(args.ledger))
                ledger_throughputs = None
            else:
                ledger_values = {commit: row.value for commit, row in rows.items()}
                ledger_throughputs = {commit: row.throughput for commit, row in rows.items()}
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_model_with_baseline(
                    space, meta, ledger_values, model_id=args.model_id,
                    ledger_throughputs=ledger_throughputs,
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
            if getattr(args, "json", False):
                print(json.dumps({"trial_id": args.trial_id, "status": status}))
            else:
                print(f"OK: trial {args.trial_id} adjudicated {status}")
            return 0
        if args.command == "resolve-verdict":
            ledger_rows = load_ledger_rows(Path(args.ledger))
            kwargs = {"reactivation_trigger": args.reactivation_trigger} if args.reactivation_trigger else {}
            verdict = _load_mutate_save(
                args.space_file,
                lambda space: adjudicate_verdict(space, args.trial_id, ledger_rows, **kwargs),
            )
            if getattr(args, "json", False):
                print(json.dumps({"trial_id": args.trial_id, "verdict": verdict}))
            else:
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


_IDEA_BRIDGE_COMMANDS = frozenset({
    "register-idea", "resolve-citation", "claim-idea", "heartbeat-idea-claim",
    "adopt-idea", "park-idea", "reject-idea", "invalidate-adoption", "reopen-idea",
    "backlog", "rejection-memory", "retriable-ideas", "seed-campaign", "readback",
})


def _object(value: str, *, noun: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{noun} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{noun} must be a JSON object")
    return parsed


def _semantic_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge.ml_registry.cli",
        description=("Canonical experiment/run/artifact/registered-model/model-version/alias registry. "
                     "Live registry commands use --registry-root; IDEA and adjudication inventory "
                     "remain in RegistrySpace and use --space-file."),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def registry_command(name: str, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--registry-root", required=True,
                             help="root of the canonical SQLite registry, blob store, and event log")
        return command

    experiment = registry_command("create-experiment", "create an immutable experiment")
    experiment.add_argument("--experiment-json", required=True)
    run = registry_command("create-run", "create a trainer-owned running run")
    run.add_argument("--run-json", required=True)
    complete = registry_command("complete-run", "record trainer measurements for a run")
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--metrics-json", required=True)
    artifact = registry_command("create-artifact", "store an immutable run artifact")
    artifact.add_argument("--run-id", required=True)
    artifact.add_argument("--kind", required=True,
                          choices=["checkpoint", "oof_predictions", "split_manifest",
                                   "dataset_manifest", "report"])
    artifact.add_argument("--content-file", required=True)
    artifact.add_argument("--schema-version", required=True)
    model = registry_command("register-model", "create an immutable registered model")
    model.add_argument("--model-json", required=True)
    lineage = registry_command("create-lineage", "link model versions in the artifact lineage")
    lineage.add_argument("--lineage-json", required=True)
    adjudicate = registry_command(
        "adjudicate-run", "derive and record a run verdict against the champion alias")
    adjudicate.add_argument("--run-id", required=True)
    adjudicate.add_argument("--model-id", required=True)
    adjudicate.add_argument("--reason", required=True)
    adjudicate.add_argument("--promotion-json")

    status = registry_command(
        "registry-status", "show experiments, runs, artifacts, registered models, model versions, and aliases")
    status.add_argument("--experiment-id")
    status.add_argument("--model-id")
    status.add_argument("--json", action="store_true")

    finalize = registry_command(
        "finalize", "verify a champion model version and atomically move its production alias")
    finalize.add_argument("--space-file", required=True,
                          help="RegistrySpace IDEA inventory used only to build the adjudication view")
    finalize.add_argument("--experiment-id", required=True)
    finalize.add_argument("--model-id", required=True)
    finalize.add_argument("--model-fact-id", required=True)
    finalize.add_argument("--version", required=True, type=int)
    finalize.add_argument("--reason", required=True)
    finalize.add_argument("--compatibility-command-json", required=True,
                          help="JSON argv; receives artifact path and HEAD sha as its final two arguments")
    finalize.add_argument("--min-measured", type=int, default=3)

    archive = registry_command(
        "import-historical-archive", "explicitly import one sealed pre-registry archive")
    archive.add_argument("--archive", required=True)
    archive.add_argument("--archive-root")
    archive.add_argument("--mappings-json", default="{}")
    freeze = registry_command(
        "import-historical-evidence-freeze", "explicitly import one noncanonical evidence freeze")
    freeze.add_argument("--freeze", required=True)
    freeze.add_argument("--archive-root")
    ledger = registry_command(
        "import-historical-ledger", "explicitly import a sealed historical results export")
    ledger.add_argument("--input", required=True)
    ledger.add_argument("--experiment-id", required=True)
    ledger.add_argument("--spec-digest", required=True)
    ledger.add_argument("--metric", required=True)
    ledger.add_argument("--direction", required=True, choices=["maximize", "minimize"])
    export = registry_command(
        "export-runs", "export canonical runs in the historical tabular interchange format")
    export.add_argument("--experiment-id", required=True)
    export.add_argument("--output")

    for name in sorted(_IDEA_BRIDGE_COMMANDS):
        bridge = sub.add_parser(
            name, add_help=False,
            help="RegistrySpace IDEA/adjudication bridge; use --space-file (run COMMAND --help for details)",
        )
        bridge.add_argument("--space-file", required=False, help=argparse.SUPPRESS)
    return parser


def _registry_status(registry, args: argparse.Namespace) -> dict[str, object]:
    experiments = registry.rows("experiments")
    runs = registry.rows("runs")
    models = registry.rows("registered_models")
    versions = registry.rows("model_versions")
    aliases = registry.rows("aliases")
    if args.experiment_id:
        experiments = [row for row in experiments if row["experiment_id"] == args.experiment_id]
        runs = [row for row in runs if row["experiment_id"] == args.experiment_id]
    if args.model_id:
        models = [row for row in models if row["model_id"] == args.model_id]
        versions = [row for row in versions if row["model_id"] == args.model_id]
        aliases = [row for row in aliases if row["model_id"] == args.model_id]
    run_ids = {row["run_id"] for row in runs}
    artifacts = [row for row in registry.rows("artifacts") if not args.experiment_id
                 or row["run_id"] in run_ids]
    return {"experiments": experiments, "runs": runs, "artifacts": artifacts,
            "registered_models": models, "model_versions": versions, "aliases": aliases}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _semantic_parser()
    args, remainder = parser.parse_known_args(argv)
    if args.command in _IDEA_BRIDGE_COMMANDS:
        # RegistrySpace remains the permanent IDEA/adjudication inventory.  Preserve its
        # mature command contracts without allowing any legacy live-registry command through.
        return _idea_bridge_main(argv)
    if remainder:
        parser.error(f"unrecognized arguments: {' '.join(remainder)}")
    try:
        from knowledge.ml_registry.storage import Registry
        registry = Registry(args.registry_root)
        if args.command == "create-experiment":
            registry.create_experiment(**_object(args.experiment_json, noun="experiment-json"))
        elif args.command == "create-run":
            registry.create_run(**_object(args.run_json, noun="run-json"))
        elif args.command == "complete-run":
            from knowledge.ml_registry.services.registry_runs import complete_run
            complete_run(registry, run_id=args.run_id,
                         metrics=_object(args.metrics_json, noun="metrics-json"))
        elif args.command == "create-artifact":
            artifact_id = registry.create_artifact(
                run_id=args.run_id, kind=args.kind, content=Path(args.content_file).read_bytes(),
                schema_version=args.schema_version,
            )
            print(json.dumps({"artifact_id": artifact_id}))
            return 0
        elif args.command == "register-model":
            registry.register_model(**_object(args.model_json, noun="model-json"))
        elif args.command == "create-lineage":
            registry.create_lineage(**_object(args.lineage_json, noun="lineage-json"))
        elif args.command == "adjudicate-run":
            from knowledge.ml_registry.services.registry_adjudication import adjudicate_against_champion
            promotion = (_object(args.promotion_json, noun="promotion-json")
                         if args.promotion_json else None)
            verdict = adjudicate_against_champion(
                registry, run_id=args.run_id, model_id=args.model_id,
                reason=args.reason, promotion=promotion,
            )
            print(json.dumps({"run_id": args.run_id, "verdict": verdict}))
            return 0
        elif args.command == "registry-status":
            payload = _registry_status(registry, args)
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"experiments: {len(payload['experiments'])}")
                print(f"runs: {len(payload['runs'])}")
                print(f"artifacts: {len(payload['artifacts'])}")
                print(f"registered models: {len(payload['registered_models'])}")
                print(f"model versions: {len(payload['model_versions'])}")
                for alias in payload["aliases"]:
                    print(f"alias {alias['model_id']}:{alias['alias']} -> version {alias['version']}")
            return 0
        elif args.command == "finalize":
            from knowledge.ml_registry.domain import CampaignBinding
            from knowledge.ml_registry.services import build_campaign_view
            from knowledge.ml_registry.services.registry_finalize import RegistryFinalizer
            space = RegistrySpace.load(Path(args.space_file))
            command = json.loads(args.compatibility_command_json)
            if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
                raise ValueError("compatibility-command-json must be a non-empty JSON argv list")
            def compatibility_loader(_version, artifact_path, head_sha):
                return subprocess.run(
                    [*command, str(artifact_path), head_sha], check=False,
                ).returncode == 0
            view = build_campaign_view(
                space, registry,
                CampaignBinding(args.experiment_id, args.model_id, args.model_fact_id),
            )
            result = RegistryFinalizer(
                registry, compatibility_loader=compatibility_loader,
                min_measured=args.min_measured,
            ).finalize(view, version=args.version, reason=args.reason)
            print(json.dumps({"model_id": result.model_version.model_id,
                              "version": result.model_version.version,
                              "alias": result.production_alias.alias}))
            return 0
        elif args.command == "import-historical-archive":
            from knowledge.ml_registry.storage.importers import HistoricalStoreImporter
            result = HistoricalStoreImporter(registry, archive_root=args.archive_root).import_archive(
                args.archive, mappings=_object(args.mappings_json, noun="mappings-json"))
            print(json.dumps(result.__dict__, sort_keys=True))
            return 0
        elif args.command == "import-historical-evidence-freeze":
            from knowledge.ml_registry.storage.importers import HistoricalStoreImporter
            result = HistoricalStoreImporter(registry, archive_root=args.archive_root).import_evidence_freeze(
                args.freeze)
            print(json.dumps(result.__dict__, sort_keys=True))
            return 0
        elif args.command == "import-historical-ledger":
            from knowledge.ml_registry.storage.importers import HistoricalLedgerImporter
            imported_runs = HistoricalLedgerImporter(registry).import_ledger(
                Path(args.input).read_bytes(), experiment_id=args.experiment_id,
                spec_digest=args.spec_digest, metric=args.metric, direction=args.direction,
            )
            print(json.dumps({"imported_runs": imported_runs}, sort_keys=True))
            return 0
        elif args.command == "export-runs":
            from knowledge.ml_registry.contracts import RunsExport
            content = RunsExport(registry).render(experiment_id=args.experiment_id)
            if args.output:
                Path(args.output).write_bytes(content)
            else:
                sys.stdout.buffer.write(content)
            return 0
        else:  # pragma: no cover
            return 2
        print("ok")
        return 0
    except (RegistryValidationError, ValueError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
