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
    heartbeat_idea_claim,
    invalidate_adoption,
    is_retriable,
    park_idea,
    reopen_idea,
    reject_idea,
    rejection_memory,
    untried_backlog,
)
from knowledge.ml_registry.schema import IDEA, RegistryValidationError
from knowledge.ml_registry.verdict import LedgerRow
from knowledge.ml_registry.write_path import (
    RegistrySpace,
    register_idea,
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

    telemetry_p = sub.add_parser(
        "campaign-telemetry", help="publish the durable Trackio per-arm campaign record"
    )
    telemetry_p.add_argument("--space-file", required=True)
    telemetry_p.add_argument("--model-id", required=True)
    telemetry_p.add_argument("--ledger", required=True)
    telemetry_p.add_argument("--store-root", required=True)
    telemetry_p.add_argument("--project", required=True)

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
        if args.command == "campaign-telemetry":
            from knowledge.ml_registry.telemetry import publish_campaign_telemetry

            receipt = publish_campaign_telemetry(
                RegistrySpace.load(Path(args.space_file)),
                args.model_id,
                load_ledger_rows(Path(args.ledger)),
                store_root=Path(args.store_root),
                project=args.project,
            )
            print(json.dumps({
                "project": receipt.project,
                "database": str(receipt.database),
                "experiments": receipt.experiment_count,
                "dead_ends": receipt.dead_end_count,
            }, sort_keys=True))
            return 0
    except RegistryValidationError as exc:
        print(f"REFUSED [{exc.field}]: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover




_IDEA_BRIDGE_COMMANDS = frozenset({
    "register-idea", "resolve-citation", "claim-idea", "heartbeat-idea-claim",
    "adopt-idea", "park-idea", "reject-idea", "invalidate-adoption", "reopen-idea",
    "backlog", "rejection-memory", "retriable-ideas", "seed-campaign", "readback",
    "campaign-telemetry",
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
    adjudicate.add_argument(
        "--paired-evidence-json",
        help="same-unit candidate/champion evidence matching the frozen CampaignSpec policy",
    )

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
            paired_evidence = (_object(args.paired_evidence_json, noun="paired-evidence-json")
                               if args.paired_evidence_json else None)
            verdict = adjudicate_against_champion(
                registry, run_id=args.run_id, model_id=args.model_id,
                reason=args.reason, promotion=promotion, paired_evidence=paired_evidence,
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
