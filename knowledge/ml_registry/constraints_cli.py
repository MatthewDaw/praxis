"""Read-only CLI for constrained ML promotion gates.

Run with ``python -m knowledge.ml_registry.constraints_cli evaluate-constraints``.
Inputs may be inline JSON objects or paths to files containing JSON objects.  Exit 0
means every gate passed, 1 means measured evidence failed a hard gate, and 2 means
the contract/evidence was unavailable or invalid.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Mapping

from knowledge.ml_registry.constraints import (
    STATUS_FAILED,
    STATUS_PASSED,
    evaluate_metric_contract,
    metric_contract_from_dict,
)
from knowledge.ml_registry.schema import RegistryValidationError


def _json_object(raw: str, *, field: str) -> dict[str, object]:
    stripped = raw.strip()
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
    else:
        path = Path(stripped)
        if not path.is_file():
            raise ValueError(
                f"{field} is neither an inline JSON object nor an existing JSON file"
            )
        parsed = json.loads(path.read_text())
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{field} must contain a JSON object")
    return dict(parsed)


def _refusal(reason: str, *, field: str) -> int:
    print(
        json.dumps({"status": "refused", "field": field, "reasons": [reason]}, indent=2)
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="knowledge.ml_registry.constraints_cli",
        description="Evaluate one primary metric and optional hard secondary/slice gates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser(
        "evaluate-constraints",
        help="read-only evaluation; exits 0 passed, 1 failed, or 2 refused/invalid",
        description="Read-only evaluation; exits 0 passed, 1 failed, or 2 refused/invalid.",
    )
    evaluate.add_argument(
        "--contract-json",
        required=True,
        help="inline JSON object or JSON file path; requires primary_metric and primary_direction",
    )
    evaluate.add_argument(
        "--metrics-json",
        required=True,
        help="inline JSON object or JSON file path containing overall metrics and optional slices",
    )
    args = parser.parse_args(argv)

    try:
        contract = metric_contract_from_dict(
            _json_object(args.contract_json, field="contract_json")
        )
        metrics = _json_object(args.metrics_json, field="metrics_json")
        result = evaluate_metric_contract(contract, metrics)
    except RegistryValidationError as exc:
        return _refusal(str(exc), field=exc.field)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return _refusal(str(exc), field="input")

    print(json.dumps(result.to_dict(), indent=2))
    if result.status == STATUS_PASSED:
        return 0
    if result.status == STATUS_FAILED:
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
