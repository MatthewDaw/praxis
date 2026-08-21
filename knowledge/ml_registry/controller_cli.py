"""Continuous portfolio controller entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from knowledge.ml_registry.controller import ControllerError, ExecutorProcessBackend, PortfolioController
from knowledge.ml_registry.portfolio import Portfolio, PortfolioValidationError
from knowledge.ml_registry.scheduler import PortfolioError
from knowledge.ml_registry.scheduler_cli import _campaigns, _capacity


def _legacy_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.controller_cli")
    parser.add_argument("run-portfolio", choices=["run-portfolio"])
    parser.add_argument("--portfolio", required=True)
    parser.add_argument("--campaigns", required=True)
    parser.add_argument("--capacity", required=True)
    parser.add_argument("--controller-state", required=True)
    parser.add_argument("--dispatch-dir", required=True)
    parser.add_argument("--max-active", type=int, default=2, choices=[1, 2])
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--retry-backoff", type=float, default=60.0)
    parser.add_argument("--one-shot", action="store_true")
    args = parser.parse_args(argv)
    try:
        portfolio = Portfolio.load(args.portfolio)
        campaigns = _campaigns(json.loads(Path(args.campaigns).read_text()))
        resources, configured_max, _ = _capacity(json.loads(Path(args.capacity).read_text()))
        max_active = min(args.max_active, configured_max)
        controller = PortfolioController(
            portfolio=portfolio, campaign_specs=campaigns, capacity=resources,
            backend=ExecutorProcessBackend(args.dispatch_dir), state_path=args.controller_state,
            max_active=max_active, retry_backoff_seconds=args.retry_backoff,
        )
        result = controller.run(poll_interval=args.poll_interval, one_shot=args.one_shot)
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 1 if result.status == "blocked" else 0
    except (ControllerError, PortfolioError, PortfolioValidationError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Expose the canonical operator while refusing the retired command spelling."""
    effective_argv = sys.argv[1:] if argv is None else argv
    if effective_argv and effective_argv[0] == "run-portfolio":
        print(
            "REFUSED: path-owned portfolio controller retired; use the canonical registry portfolio CLI "
            "run command with an operator config",
            file=sys.stderr,
        )
        return 1
    from knowledge.ml_registry.operator_cli import main as operator_main
    return operator_main(effective_argv)


if __name__ == "__main__":
    sys.exit(main())
