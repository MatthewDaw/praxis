"""Runnable entrypoint for the af-ml-research registry (R1 schema/guards, R2 write path,
R3 idea lifecycle, R4 query surface, R5 cross-project model linkage).

``python -m knowledge.ml_registry.cli <subcommand> ...`` -- exit 0 on acceptance, 1 on a
named registry refusal, 2 on malformed input. This is the real entrypoint later tickets
call into for their own decisions; it also gives the registry a runnable surface an
automated check can invoke rather than merely import.

The R2 ``register-*``/``readback`` subcommands persist a :class:`RegistrySpace` as JSON
at ``--space-file`` across separate process invocations, so a CLI-driven test can
register a model, then an idea against it, then a trial against that idea, then read
all three back -- the same sequence the write API supports in-process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from typing import Callable, TypeVar

from knowledge.ml_registry.citation import Resolver, ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.cross_project import TicketIndex, model_to_projects, project_to_models
from knowledge.ml_registry.floor import adjudicate_trial, load_ledger_values, register_model_with_baseline, retire_harness
from knowledge.ml_registry.guards import guard_baseline_move, guard_model_mutation
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
from knowledge.ml_registry.schema import IDEA, RegistryValidationError, validate_fact
from knowledge.ml_registry.supervisor import Intervention, supervise_campaign
from knowledge.ml_registry.write_path import (
    RegistrySpace,
    load_ledger_commits,
    register_idea,
    register_model,
    register_trial,
    resolve_idea_citation,
)

_T = TypeVar("_T")


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
    """Load the space at ``space_file``, apply a single mutation, save, and return its result --
    the load/mutate/save sequence every mutating subcommand needs, factored once."""
    space_path = Path(space_file)
    space = RegistrySpace.load(space_path)
    result = fn(space)
    space.save(space_path)
    return result


def _fixed_outcome_resolver(outcome: str, title: str, authors: tuple[str, ...]) -> Resolver:
    """A resolver that always reports the CLI-supplied outcome for this one attempt.

    The CLI has no live network access; a real arXiv/DOI lookup belongs to whatever
    service calls this entrypoint during an ideation pass and can supply its own
    resolver in-process. This keeps the CLI itself deterministic and offline-testable
    while still exercising the real :func:`resolve_idea_citation` write path.
    """

    def resolver(reference: str) -> ResolvedCitation | None:
        if outcome == "unreachable":
            raise ResolverUnreachable(reference)
        if outcome == "non-existent":
            return None
        return ResolvedCitation(title=title, authors=authors)

    return resolver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.cli")
    sub = parser.add_subparsers(dest="command", required=True)

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
        help="re-register (update) an already-registered model instead of creating a new one",
    )

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
        "adjudicate-trial", help="decide a trial's status on a single observed value (R12)"
    )
    adjudicate_p.add_argument("--space-file", required=True)
    adjudicate_p.add_argument("--trial-id", required=True)
    adjudicate_p.add_argument("--observed-value", type=float, required=True)

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
        "--outcome",
        required=True,
        choices=["resolved", "non-existent", "unreachable"],
        help="what the (test-controlled) resolver reports for this attempt",
    )
    resolve_p.add_argument("--title", default="", help="resolved title, required when --outcome=resolved")
    resolve_p.add_argument(
        "--author", action="append", default=[], help="resolved author, repeatable, used when --outcome=resolved"
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

    args = parser.parse_args(argv)

    try:
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
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_model(space, _json_arg(args.meta_json), model_id=args.model_id),
            )
            print(f"OK: registered model {fact_id}")
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
            fact_id = _load_mutate_save(
                args.space_file,
                lambda space: register_model_with_baseline(
                    space, _json_arg(args.meta_json), ledger_values, model_id=args.model_id
                ),
            )
            print(f"OK: registered model {fact_id}")
            return 0
        if args.command == "adjudicate-trial":
            status = _load_mutate_save(
                args.space_file, lambda space: adjudicate_trial(space, args.trial_id, args.observed_value)
            )
            print(f"OK: trial {args.trial_id} adjudicated {status}")
            return 0
        if args.command == "retire-harness":
            fact = _load_mutate_save(
                args.space_file, lambda space: retire_harness(space, args.model_id, _json_arg(args.patch_json))
            )
            print(json.dumps(fact.to_json()))
            return 0
        if args.command == "resolve-citation":
            space_path = Path(args.space_file)
            space = RegistrySpace.load(space_path)
            resolver = _fixed_outcome_resolver(args.outcome, args.title, tuple(args.author))
            meta = resolve_idea_citation(space, args.idea_id, args.reference, resolver)
            space.save(space_path)
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
            ledger_values = load_ledger_values(Path(args.ledger))
            dispatch_script = list(json.loads(Path(args.dispatch_script).read_text()))
            idea_script = (
                list(json.loads(Path(args.idea_script).read_text())) if args.idea_script else []
            )
            interventions = tuple(_parse_intervention(raw) for raw in args.intervention)

            dispatch_iter = iter(dispatch_script)
            idea_iter = iter(idea_script)

            def dispatcher(space, model, idea):  # noqa: ANN001 -- matches supervisor.Dispatcher
                try:
                    return next(dispatch_iter)
                except StopIteration as exc:
                    raise RegistryValidationError(
                        "dispatch-script exhausted before the campaign closed", field="dispatch_script"
                    ) from exc

            def idea_generator(space, model_id, forced_axis, permitted_axes):  # noqa: ANN001
                return next(idea_iter, None)

            outcome = _load_mutate_save(
                args.space_file,
                lambda space: supervise_campaign(
                    space, args.model_id, ledger_values, dispatcher,
                    interventions=interventions, idea_generator=idea_generator,
                    max_dispatches=args.max_dispatches,
                ),
            )
            print(json.dumps(outcome, default=lambda o: {"kind": o.kind, "axis": o.axis}))
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
