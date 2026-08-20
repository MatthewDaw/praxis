"""Read-only command line entrypoint for ML portfolio scheduling.

Usage::

    python -m knowledge.ml_registry.scheduler_cli schedule-portfolio \
      --campaigns campaigns.json --states states.json --capacity capacity.json

The command never writes registry state or launches the returned jobs.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Any

from knowledge.ml_registry.scheduler import PortfolioError, schedule


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text())


def _campaigns(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "campaigns" in payload:
        payload = payload["campaigns"]
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise TypeError("campaigns JSON must be a list of objects or an object with a campaigns list")
    return payload


def _states(payload: Any) -> dict[str, dict[str, Any]]:
    if isinstance(payload, dict) and "states" in payload:
        payload = payload["states"]
    if isinstance(payload, list):
        if not all(isinstance(item, dict) and item.get("campaign_id") for item in payload):
            raise TypeError("each state in a state list requires campaign_id")
        payload = {str(item["campaign_id"]): item for item in payload}
    if not isinstance(payload, dict) or not all(isinstance(item, dict) for item in payload.values()):
        raise TypeError("states JSON must be an object keyed by campaign id or a list of state objects")
    return {
        str(campaign_id): {"campaign_id": str(campaign_id), **state}
        for campaign_id, state in payload.items()
    }


def _capacity(payload: Any) -> tuple[dict[str, Any], int, float | None]:
    if not isinstance(payload, dict):
        raise TypeError("capacity JSON must be an object")
    resources = payload.get("resources", payload)
    if not isinstance(resources, dict):
        raise TypeError("capacity resources must be an object")
    # When resources are top-level, scheduler controls are not ResourceProfile fields.
    if resources is payload:
        resources = {key: value for key, value in resources.items()
                     if key not in {"max_concurrency", "remaining_cost"}}
    concurrency = payload.get("max_concurrency")
    if not isinstance(concurrency, int):
        raise TypeError("capacity JSON requires integer max_concurrency")
    remaining_cost = payload.get("remaining_cost")
    if remaining_cost is not None and not isinstance(remaining_cost, (int, float)):
        raise TypeError("remaining_cost must be numeric or null")
    return resources, concurrency, remaining_cost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.scheduler_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    schedule_p = sub.add_parser("schedule-portfolio", help="compute a read-only ready frontier")
    schedule_p.add_argument("--campaigns", required=True, help="campaign specification JSON")
    schedule_p.add_argument("--states", required=True, help="campaign state snapshot JSON")
    schedule_p.add_argument("--capacity", required=True, help="resource and budget capacity JSON")
    args = parser.parse_args(argv)

    try:
        campaigns = _campaigns(_load(args.campaigns))
        states = _states(_load(args.states))
        resources, concurrency, cost = _capacity(_load(args.capacity))
        decision = schedule(
            campaigns,
            states,
            resources,
            max_concurrency=concurrency,
            remaining_cost=cost,
        )
        output = {
            "available": asdict(decision.available),
            "blocked": dict(sorted(decision.blocked.items())),
            "jobs": [asdict(job) for job in decision.jobs],
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except PortfolioError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
