"""Bounded agent-mode campaign session without tmux or shell lifecycle authority."""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import sys
import tempfile
from typing import Callable, Mapping, Sequence

from knowledge.ml_registry.contracts import (
    CampaignOutcome,
    CampaignOutcomeRecord,
    ProductionAliasRef,
)
from knowledge.ml_registry.runtime.progress import parse_progress_line, write_progress_snapshot


QUOTA_MARKERS = (
    "weekly limit", "buy more credits", "usage limit", "out of credits",
    "quota exceeded", "rate limit exceeded", "authentication failed", "not authenticated",
)


@dataclass(frozen=True)
class AgentSessionResult:
    outcome: CampaignOutcome
    reason: str
    launches: int
    returncode: int | None
    production_alias: ProductionAliasRef | None = None


class AgentSession:
    """Run an arm-authoring agent under the campaign job's process group.

    The agent remains the lifecycle decision maker under ``/af-ml-supervise``.  This
    class only owns liveness, progress transport, quota classification, and a bounded
    relaunch when durable registry state says the campaign is still unfinished.
    """

    def __init__(self, *, command: Sequence[str], cwd: Path, progress_path: Path,
                 completion: Callable[[], ProductionAliasRef | None],
                 max_relaunches: int = 2) -> None:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("agent command must be a non-empty argv sequence")
        if max_relaunches < 0:
            raise ValueError("max_relaunches cannot be negative")
        self.command = tuple(command)
        self.cwd = cwd
        self.progress_path = progress_path
        self.completion = completion
        self.max_relaunches = max_relaunches
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        if self._process is None or self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def run(self) -> AgentSessionResult:
        launches = 0
        last_returncode = None
        while launches <= self.max_relaunches:
            if self._cancelled.is_set():
                return AgentSessionResult(CampaignOutcome.CANCELLED, "operator cancelled agent session",
                                          launches, last_returncode)
            production_alias = self.completion()
            if production_alias is not None:
                if not isinstance(production_alias, ProductionAliasRef):
                    return AgentSessionResult(CampaignOutcome.FAILED,
                                              "completion adapter returned no production alias reference",
                                              launches, last_returncode)
                return AgentSessionResult(CampaignOutcome.COMPLETE,
                                          "canonical production alias is present", launches,
                                          last_returncode, production_alias)
            launches += 1
            environment = dict(os.environ)
            environment["PYTHONUNBUFFERED"] = "1"
            self._process = subprocess.Popen(
                self.command, cwd=self.cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            quota = None
            assert self._process.stdout is not None
            for line in iter(self._process.stdout.readline, ""):
                print(line, end="", flush=True)
                snapshot = parse_progress_line(line)
                if snapshot is not None:
                    write_progress_snapshot(self.progress_path, snapshot)
                lowered = line.lower()
                if quota is None and any(marker in lowered for marker in QUOTA_MARKERS):
                    quota = line.strip()
            last_returncode = self._process.wait()
            if self._cancelled.is_set():
                return AgentSessionResult(CampaignOutcome.CANCELLED, "operator cancelled agent session",
                                          launches, last_returncode)
            if quota:
                return AgentSessionResult(CampaignOutcome.QUOTA, quota, launches, last_returncode)
            production_alias = self.completion()
            if production_alias is not None:
                if not isinstance(production_alias, ProductionAliasRef):
                    return AgentSessionResult(CampaignOutcome.FAILED,
                                              "completion adapter returned no production alias reference",
                                              launches, last_returncode)
                return AgentSessionResult(CampaignOutcome.COMPLETE,
                                          "canonical production alias is present", launches,
                                          last_returncode, production_alias)
            if last_returncode not in {0, 75}:
                return AgentSessionResult(CampaignOutcome.RETRYABLE,
                                          f"agent exited {last_returncode}", launches,
                                          last_returncode)
        return AgentSessionResult(CampaignOutcome.STALLED,
                                  "agent relaunch budget exhausted without registry completion",
                                  launches, last_returncode)


def _atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _completion(reference: str, options: Mapping[str, object]):
    module, name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    probe = factory(dict(options))
    if not callable(probe):
        raise ValueError("completion adapter factory must return a callable")
    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded arm-authoring agent session")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text())
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise ValueError("agent-session config schema_version must be 1")
        command = config.get("command")
        if not isinstance(command, list):
            raise ValueError("agent-session command must be an argv list")
        outcome_path = Path(str(config["outcome_path"]))
        session = AgentSession(
            command=command, cwd=Path(str(config["working_directory"])),
            progress_path=Path(str(config["progress_path"])),
            completion=_completion(
                str(config["completion_adapter"]), config.get("completion_options", {}),
            ),
            max_relaunches=int(config.get("max_relaunches", 2)),
        )
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_args: session.cancel())
        try:
            result = session.run()
        finally:
            signal.signal(signal.SIGTERM, previous)
        if result.outcome is CampaignOutcome.COMPLETE and result.production_alias is None:
            raise ValueError("completion adapter must return ProductionAliasRef, not a boolean")
        record = CampaignOutcomeRecord(
            CampaignOutcomeRecord.VERSION, str(config["campaign_id"]), result.outcome,
            result.reason, int(config.get("attempt", 1)), result.production_alias,
        )
        _atomic(outcome_path, record.to_mapping())
        return {
            CampaignOutcome.COMPLETE: 0,
            CampaignOutcome.BLOCKED: 3,
            CampaignOutcome.STALLED: 4,
            CampaignOutcome.RETRYABLE: 5,
            CampaignOutcome.FAILED: 6,
            CampaignOutcome.QUOTA: 8,
            CampaignOutcome.CANCELLED: 130,
        }[result.outcome]
    except (OSError, ImportError, AttributeError, KeyError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
