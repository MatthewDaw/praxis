"""Runnable entrypoint for the af-ml-research registry (R1 schema/guards, R2 write path).

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

from knowledge.ml_registry.guards import guard_baseline_move, guard_model_mutation
from knowledge.ml_registry.lifecycle import (
    adopt_idea,
    claim_idea,
    flagged_trials,
    heartbeat_idea_claim,
    invalidate_adoption,
    is_retriable,
    park_idea,
    reject_idea,
    rejection_memory,
    untried_backlog,
)
from knowledge.ml_registry.schema import IDEA, RegistryValidationError, validate_fact
from knowledge.ml_registry.write_path import (
    RegistrySpace,
    load_ledger_commits,
    register_idea,
    register_model,
    register_trial,
)

_T = TypeVar("_T")


def _json_arg(raw: str) -> dict[str, object]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _load_mutate_save(space_file: str, fn: Callable[[RegistrySpace], _T]) -> _T:
    """Load the space at ``space_file``, apply a single mutation, save, and return its result --
    the load/mutate/save sequence every mutating subcommand needs, factored once."""
    space_path = Path(space_file)
    space = RegistrySpace.load(space_path)
    result = fn(space)
    space.save(space_path)
    return result


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

    register_idea_p = sub.add_parser("register-idea", help="register an idea fact")
    register_idea_p.add_argument("--space-file", required=True)
    register_idea_p.add_argument("--meta-json", required=True)

    register_trial_p = sub.add_parser("register-trial", help="register a trial fact")
    register_trial_p.add_argument("--space-file", required=True)
    register_trial_p.add_argument("--meta-json", required=True)
    register_trial_p.add_argument(
        "--ledger", required=True, help="path to the autoresearch loop's results.tsv"
    )

    readback_p = sub.add_parser("readback", help="read back every fact in the space")
    readback_p.add_argument("--space-file", required=True)
    readback_p.add_argument("--category", choices=["model", "idea", "trial"], default=None)

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

    retriable_p = sub.add_parser(
        "retriable-ideas", help="parked ideas whose reactivation_trigger is among the fired triggers"
    )
    retriable_p.add_argument("--space-file", required=True)
    retriable_p.add_argument("--fired-trigger", action="append", default=[])

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
                args.space_file, lambda space: register_model(space, _json_arg(args.meta_json))
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
        if args.command == "readback":
            space = RegistrySpace.load(Path(args.space_file))
            facts = space.list_facts(args.category)
            print(json.dumps([f.to_json() for f in facts]))
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
    except RegistryValidationError as exc:
        print(f"REFUSED [{exc.field}]: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2

    return 2  # pragma: no cover - argparse's `required=True` makes this unreachable


if __name__ == "__main__":
    sys.exit(main())
