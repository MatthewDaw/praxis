"""Single-campaign runtime owned by the portfolio controller.

This module deliberately does not adjudicate.  A project adapter may claim and run
one arm, but Praxis's registry services remain the only writers of verdicts and the
``champion`` alias.  The controller subsequently treats ``COMPLETE`` as a claim and
asks :class:`RegistryFinalizer` to verify the ``production`` alias.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Callable, Mapping, Protocol, Sequence

from knowledge.ml_registry.contracts import (
    CampaignOutcome,
    CampaignOutcomeRecord,
    ProductionAliasRef,
)
from knowledge.ml_registry.runtime.progress import parse_progress_line, write_progress_snapshot


EXIT_BY_OUTCOME = {
    CampaignOutcome.COMPLETE: 0,
    CampaignOutcome.BLOCKED: 3,
    CampaignOutcome.STALLED: 4,
    CampaignOutcome.RETRYABLE: 5,
    CampaignOutcome.FAILED: 6,
    CampaignOutcome.QUOTA: 8,
    CampaignOutcome.CANCELLED: 130,
}


class CampaignJobError(ValueError):
    pass


@dataclass(frozen=True)
class CampaignJobContext:
    campaign_id: str
    attempt: int
    state_root: Path
    progress_path: Path


class CampaignLifecycle(Protocol):
    """Project adapter boundary.  Methods may read state; only ``dispatch_one`` runs an arm."""

    def preflight(self, context: CampaignJobContext) -> str | None: ...
    def complete(self, context: CampaignJobContext) -> ProductionAliasRef | None: ...
    def blocking_diagnosis(self, context: CampaignJobContext) -> str | None: ...
    def trial_count(self, context: CampaignJobContext) -> int: ...
    def dispatch_one(self, context: CampaignJobContext) -> Sequence[str] | CampaignOutcomeRecord: ...
    def heartbeat(self, context: CampaignJobContext) -> None: ...


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
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


def _load_adapter(reference: str, options: Mapping[str, object]) -> CampaignLifecycle:
    try:
        module_name, attribute = reference.split(":", 1)
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ValueError, ImportError, AttributeError) as exc:
        raise CampaignJobError(f"cannot load supervision adapter {reference!r}: {exc}") from exc
    adapter = factory(dict(options)) if callable(factory) else factory
    required = ("preflight", "complete", "blocking_diagnosis", "trial_count", "dispatch_one", "heartbeat")
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise CampaignJobError("supervision adapter is missing methods: " + ", ".join(missing))
    return adapter


class _ArmProcess:
    def __init__(self, *, progress_path: Path, heartbeat: Callable[[], None], heartbeat_s: float) -> None:
        self.progress_path = progress_path
        self.heartbeat = heartbeat
        self.heartbeat_s = heartbeat_s
        self.process: subprocess.Popen[str] | None = None
        self._stop = threading.Event()

    def cancel(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()

    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise CampaignJobError("dispatch_one must return a non-empty argv sequence")
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            list(command), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=False,
        )

        def beat() -> None:
            while not self._stop.wait(self.heartbeat_s):
                if self.process is None or self.process.poll() is not None:
                    return
                self.heartbeat()

        thread = threading.Thread(target=beat, name="campaign-heartbeat", daemon=True)
        thread.start()
        try:
            assert self.process.stdout is not None
            for line in iter(self.process.stdout.readline, ""):
                sys.stdout.write(line)
                sys.stdout.flush()
                snapshot = parse_progress_line(line)
                if snapshot is not None:
                    write_progress_snapshot(self.progress_path, snapshot)
            return self.process.wait()
        finally:
            self._stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_s + .1))


class CampaignJob:
    """Port of the composing shell loop with one-arm nesting and typed outcomes."""

    def __init__(self, *, context: CampaignJobContext, adapter: CampaignLifecycle,
                 outcome_path: Path, max_iterations: int = 40, heartbeat_s: float = 300,
                 working_directory: Path | None = None) -> None:
        if max_iterations < 1:
            raise CampaignJobError("max_iterations must be positive")
        if heartbeat_s <= 0:
            raise CampaignJobError("heartbeat_s must be positive")
        self.context = context
        self.adapter = adapter
        self.outcome_path = outcome_path
        self.max_iterations = max_iterations
        self.heartbeat_s = heartbeat_s
        self.working_directory = working_directory or context.state_root
        self._arm: _ArmProcess | None = None
        self.cancelled = False

    def _record(self, outcome: CampaignOutcome, reason: str,
                production_alias: ProductionAliasRef | None = None) -> CampaignOutcomeRecord:
        record = CampaignOutcomeRecord(
            CampaignOutcomeRecord.VERSION, self.context.campaign_id, outcome, reason,
            self.context.attempt, production_alias,
        )
        _atomic_json(self.outcome_path, record.to_mapping())
        return record

    def cancel(self) -> None:
        self.cancelled = True
        if self._arm is not None:
            self._arm.cancel()

    def run(self) -> CampaignOutcomeRecord:
        blocker = self.adapter.preflight(self.context)
        if blocker:
            return self._record(CampaignOutcome.BLOCKED, blocker)
        setup = getattr(self.adapter, "setup", None)
        if callable(setup):
            setup_result = setup(self.context)
            if isinstance(setup_result, CampaignOutcomeRecord):
                _atomic_json(self.outcome_path, setup_result.to_mapping())
                return setup_result
        for iteration in range(1, self.max_iterations + 1):
            if self.cancelled:
                return self._record(CampaignOutcome.CANCELLED, "operator cancelled campaign job")
            production_alias = self.adapter.complete(self.context)
            if production_alias is not None:
                return self._record(
                    CampaignOutcome.COMPLETE,
                    "campaign reports canonical completion",
                    production_alias,
                )
            blocker = self.adapter.blocking_diagnosis(self.context)
            if blocker:
                return self._record(CampaignOutcome.BLOCKED, blocker)
            before = self.adapter.trial_count(self.context)
            dispatch = self.adapter.dispatch_one(self.context)
            if isinstance(dispatch, CampaignOutcomeRecord):
                _atomic_json(self.outcome_path, dispatch.to_mapping())
                return dispatch
            self._arm = _ArmProcess(
                progress_path=self.context.progress_path,
                heartbeat=lambda: self.adapter.heartbeat(self.context),
                heartbeat_s=self.heartbeat_s,
            )
            returncode = self._arm.run(dispatch, cwd=self.working_directory)
            self._arm = None
            if self.cancelled or returncode in {130, 143, -signal.SIGTERM, -signal.SIGKILL}:
                return self._record(CampaignOutcome.CANCELLED, "arm process group was cancelled")
            if returncode != 0:
                return self._record(CampaignOutcome.RETRYABLE, f"arm exited {returncode}")
            after = self.adapter.trial_count(self.context)
            if after <= before:
                return self._record(
                    CampaignOutcome.STALLED,
                    f"iteration {iteration} produced no new registry run",
                )
        return self._record(
            CampaignOutcome.RETRYABLE,
            f"maximum iteration count {self.max_iterations} reached before completion",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one registry campaign under controller ownership")
    parser.add_argument("--config", required=True, help="versioned campaign-job JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text())
        if not isinstance(config, Mapping) or config.get("schema_version") != 1:
            raise CampaignJobError("campaign-job config schema_version must be 1")
        campaign_id = str(config["campaign_id"])
        attempt = int(config.get("attempt", 1))
        state_root = Path(str(config["state_root"]))
        state_root.mkdir(parents=True, exist_ok=True)
        outcome_path = Path(str(config["outcome_path"]))
        progress_path = Path(str(config.get("progress_path", state_root / "progress.json")))
        adapter = _load_adapter(str(config["adapter"]), config.get("adapter_options", {}))
        context = CampaignJobContext(campaign_id, attempt, state_root, progress_path)
        job = CampaignJob(
            context=context, adapter=adapter, outcome_path=outcome_path,
            max_iterations=int(config.get("max_iterations", 40)),
            heartbeat_s=float(config.get("heartbeat_s", 300)),
            working_directory=Path(str(config.get("working_directory", state_root))),
        )
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, lambda *_args: job.cancel())
        signal.signal(signal.SIGINT, lambda *_args: job.cancel())
        try:
            record = job.run()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
        return EXIT_BY_OUTCOME[record.outcome]
    except (CampaignJobError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
