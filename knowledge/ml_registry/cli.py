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

from knowledge.ml_registry.citation import ResolvedCitation, ResolverUnreachable
from knowledge.ml_registry.guards import guard_baseline_move, guard_model_mutation
from knowledge.ml_registry.schema import RegistryValidationError, validate_fact
from knowledge.ml_registry.write_path import (
    RegistrySpace,
    load_ledger_commits,
    register_idea,
    register_model,
    register_trial,
    resolve_idea_citation,
)


def _json_arg(raw: str) -> dict[str, object]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _fixed_outcome_resolver(outcome: str, title: str, authors: tuple[str, ...]):
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

    register_idea_p = sub.add_parser("register-idea", help="register an idea fact")
    register_idea_p.add_argument("--space-file", required=True)
    register_idea_p.add_argument("--meta-json", required=True)

    register_trial_p = sub.add_parser("register-trial", help="register a trial fact")
    register_trial_p.add_argument("--space-file", required=True)
    register_trial_p.add_argument("--meta-json", required=True)
    register_trial_p.add_argument(
        "--ledger", required=True, help="path to the autoresearch loop's results.tsv"
    )

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
            space_path = Path(args.space_file)
            space = RegistrySpace.load(space_path)
            fact_id = register_model(space, _json_arg(args.meta_json))
            space.save(space_path)
            print(f"OK: registered model {fact_id}")
            return 0
        if args.command == "register-idea":
            space_path = Path(args.space_file)
            space = RegistrySpace.load(space_path)
            fact_id = register_idea(space, _json_arg(args.meta_json))
            space.save(space_path)
            print(f"OK: registered idea {fact_id}")
            return 0
        if args.command == "register-trial":
            space_path = Path(args.space_file)
            space = RegistrySpace.load(space_path)
            ledger_commits = load_ledger_commits(Path(args.ledger))
            fact_id = register_trial(space, _json_arg(args.meta_json), ledger_commits)
            space.save(space_path)
            print(f"OK: registered trial {fact_id}")
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
    except RegistryValidationError as exc:
        print(f"REFUSED [{exc.field}]: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2

    return 2  # pragma: no cover - argparse's `required=True` makes this unreachable


if __name__ == "__main__":
    sys.exit(main())
