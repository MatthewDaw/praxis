"""JSON command-line interface for the ML portfolio registry.

Run as ``python -m knowledge.ml_registry.portfolio_cli``.  This is intentionally
separate from the single-campaign CLI: portfolio state has its own persistence and
lifecycle, and can therefore be managed before a campaign is registered for fitting.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from knowledge.ml_registry.portfolio import (
    ArtifactDependency,
    Portfolio,
    PortfolioValidationError,
)
from knowledge.ml_registry.file_lock import exclusive_file_lock

EXIT_VALIDATION_ERROR = 3
EXIT_MALFORMED_INPUT = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage an ML campaign portfolio")
    parser.add_argument("--file", required=True, help="portfolio JSON file")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create an empty portfolio")

    add_artifact = commands.add_parser("add-artifact", help="register an immutable artifact")
    add_artifact.add_argument("--artifact-id", required=True)
    add_artifact.add_argument("--model-id", required=True)
    add_artifact.add_argument("--verdict", required=True)
    add_artifact.add_argument("--dataset-hash", required=True)
    add_artifact.add_argument("--split-hash", required=True)
    add_artifact.add_argument("--prediction-hash", required=True)
    add_artifact.add_argument("--coverage", required=True, type=float)

    add_campaign = commands.add_parser("add-campaign", help="declare a planned campaign")
    add_campaign.add_argument("--campaign-id", required=True)
    add_campaign.add_argument("--model-id", required=True)
    add_campaign.add_argument(
        "--dependency",
        action="append",
        default=[],
        metavar="JSON",
        help=(
            "repeatable ArtifactDependency JSON object; keys are upstream_model_id, "
            "artifact_id, required_verdict, dataset_manifest_hash, split_manifest_hash, "
            "prediction_manifest_hash, minimum_coverage"
        ),
    )

    readiness = commands.add_parser("readiness", help="evaluate and persist campaign readiness")
    readiness.add_argument("--campaign-id", required=True)

    start = commands.add_parser("start-seeding", help="transition ACTIVATABLE to SEEDING")
    start.add_argument("--campaign-id", required=True)

    ready = commands.add_parser("mark-ready", help="transition SEEDING to READY")
    ready.add_argument("--campaign-id", required=True)

    supersede = commands.add_parser("supersede", help="supersede an artifact and stale dependents")
    supersede.add_argument("--artifact-id", required=True)
    supersede.add_argument("--replacement-id", required=True)

    show = commands.add_parser("show", help="print the complete portfolio or one campaign")
    show.add_argument("--campaign-id")

    commands.add_parser("validate", help="validate persistence and dependency graph")
    return parser


def _load(path: Path, *, allow_new: bool = False) -> Portfolio:
    if path.exists():
        return Portfolio.load(path)
    if allow_new:
        return Portfolio(path)
    raise PortfolioValidationError(f"portfolio file {str(path)!r} does not exist")


def _mutate(path: Path, operation: Callable[[Portfolio], Any], *, allow_new: bool = False) -> Any:
    with exclusive_file_lock(path):
        portfolio = _load(path, allow_new=allow_new)
        result = operation(portfolio)
        portfolio.save()
        return result


def _campaign_json(portfolio: Portfolio, campaign_id: str) -> dict[str, Any]:
    try:
        campaign = portfolio.campaigns[campaign_id]
    except KeyError as exc:
        raise PortfolioValidationError(f"unknown campaign {campaign_id!r}") from exc
    return asdict(campaign)


def _portfolio_json(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "schema_version": portfolio.SCHEMA_VERSION,
        "campaigns": [asdict(value) for _, value in sorted(portfolio.campaigns.items())],
        "artifacts": [asdict(value) for _, value in sorted(portfolio.artifacts.items())],
    }


def _dependencies(values: list[str]) -> list[ArtifactDependency]:
    dependencies = []
    for value in values:
        document = json.loads(value)
        if not isinstance(document, dict):
            raise ValueError("--dependency must decode to a JSON object")
        try:
            dependencies.append(ArtifactDependency(**document))
        except TypeError as exc:
            raise ValueError(f"invalid --dependency fields: {exc}") from exc
    return dependencies


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.file)
    if args.command == "init":
        if path.exists():
            raise PortfolioValidationError(f"portfolio file {str(path)!r} already exists")
        _mutate(path, lambda portfolio: None, allow_new=True)
        return {"ok": True, "file": str(path), "schema_version": Portfolio.SCHEMA_VERSION}

    if args.command == "add-artifact":
        artifact = _mutate(
            path,
            lambda portfolio: portfolio.register_artifact(
                args.artifact_id,
                args.model_id,
                verdict=args.verdict,
                dataset_manifest_hash=args.dataset_hash,
                split_manifest_hash=args.split_hash,
                prediction_manifest_hash=args.prediction_hash,
                coverage=args.coverage,
            ),
            allow_new=True,
        )
        return {"ok": True, "artifact": asdict(artifact)}

    if args.command == "add-campaign":
        dependencies = _dependencies(args.dependency)
        campaign = _mutate(
            path,
            lambda portfolio: portfolio.add_campaign(args.campaign_id, args.model_id, dependencies),
            allow_new=True,
        )
        return {"ok": True, "campaign": asdict(campaign)}

    if args.command == "readiness":
        def refresh(portfolio: Portfolio) -> dict[str, Any]:
            result = portfolio.refresh(args.campaign_id)
            return {
                "campaign": _campaign_json(portfolio, args.campaign_id),
                "readiness": asdict(result),
            }

        return {"ok": True, **_mutate(path, refresh)}

    if args.command == "start-seeding":
        def start(portfolio: Portfolio) -> dict[str, Any]:
            portfolio.start_seeding(args.campaign_id)
            return _campaign_json(portfolio, args.campaign_id)

        return {"ok": True, "campaign": _mutate(path, start)}

    if args.command == "mark-ready":
        def ready(portfolio: Portfolio) -> dict[str, Any]:
            portfolio.mark_ready(args.campaign_id)
            return _campaign_json(portfolio, args.campaign_id)

        return {"ok": True, "campaign": _mutate(path, ready)}

    if args.command == "supersede":
        def supersede(portfolio: Portfolio) -> list[str]:
            return sorted(portfolio.supersede_artifact(args.artifact_id, args.replacement_id))

        return {"ok": True, "affected_campaigns": _mutate(path, supersede)}

    portfolio = _load(path)
    if args.command == "show":
        return {
            "ok": True,
            **(
                {"campaign": _campaign_json(portfolio, args.campaign_id)}
                if args.campaign_id
                else {"portfolio": _portfolio_json(portfolio)}
            ),
        }
    if args.command == "validate":
        portfolio.validate()
        return {
            "ok": True,
            "campaign_count": len(portfolio.campaigns),
            "artifact_count": len(portfolio.artifacts),
        }
    raise ValueError(f"unknown command {args.command!r}")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(_parser().parse_args(argv))
        print(json.dumps(result, sort_keys=True))
        return 0
    except PortfolioValidationError as exc:
        print(json.dumps({"ok": False, "error": "validation", "message": str(exc)}))
        return EXIT_VALIDATION_ERROR
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": "malformed_input", "message": str(exc)}))
        return EXIT_MALFORMED_INPUT


if __name__ == "__main__":
    sys.exit(main())
