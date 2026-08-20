"""CLI for executing one scheduler JobSpec through a registered backend."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from knowledge.ml_registry.executor import ExecutorError, create_backend
from knowledge.ml_registry.scheduler import JobSpec, PortfolioError, ResourceProfile


def _job(path: str) -> JobSpec:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise TypeError("job JSON must be an object")
    resources = ResourceProfile.from_mapping(payload.pop("resources", None))
    command = payload.pop("command", None)
    if not isinstance(command, list):
        raise TypeError("job command must be a JSON array")
    return JobSpec(command=tuple(command), resources=resources, **payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowledge.ml_registry.executor_cli")
    parser.add_argument("run-job", choices=["run-job"])
    parser.add_argument("--job", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--backend", default="local")
    parser.add_argument("--allow-env", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        job = _job(args.job)
        allowlist = set(args.allow_env) if args.allow_env else None
        backend = create_backend(args.backend, log_dir=args.log_dir, env_allowlist=allowlist)
        result = backend.execute(job, state_path=Path(args.state), dry_run=args.dry_run)
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0 if result.state in {"completed", "dry_run"} else 1
    except (ExecutorError, PortfolioError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"MALFORMED INPUT: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
