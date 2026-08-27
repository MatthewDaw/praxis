"""Single-campaign runtime owned by the portfolio controller.

This module deliberately does not adjudicate.  A project adapter may claim and run
one arm, but Praxis's registry services remain the only writers of verdicts and the
``champion`` alias.  The controller subsequently treats ``COMPLETE`` as a claim and
asks :class:`RegistryFinalizer` to verify the ``production`` alias.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import importlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Protocol

from knowledge.ml_registry.contracts import (
    CampaignOutcome,
    CampaignOutcomeRecord,
    ProductionAliasRef,
)
from knowledge.ml_registry.runtime.progress import parse_progress_line, write_progress_snapshot


EXIT_BY_OUTCOME = {
    CampaignOutcome.PROMOTED: 0,
    CampaignOutcome.MEASURED: 0,
    CampaignOutcome.REFUTED: 0,
    CampaignOutcome.ABANDONED: 3,
    CampaignOutcome.COMPLETE: 0,
    CampaignOutcome.BLOCKED: 3,
    CampaignOutcome.STALLED: 4,
    CampaignOutcome.RETRYABLE: 5,
    CampaignOutcome.FAILED: 6,
    CampaignOutcome.QUOTA: 8,
    CampaignOutcome.CANCELLED: 130,
}

# Fifty GiB leaves room for ordinary checkpoints while making the default finite and visible.
# Projects with larger declared corpora must opt in to a larger campaign-local budget.
DEFAULT_CAMPAIGN_DISK_BUDGET_BYTES = 50 * 1024**3
DEFAULT_ARM_TIMEOUT_S = 60 * 60
ARM_STARTUP_GRACE_S = 1.0
_OUTPUT_TAIL_LINES = 25
_OUTPUT_TAIL_CHARS = 900


class CampaignJobError(ValueError):
    pass


@dataclass(frozen=True)
class CampaignJobContext:
    campaign_id: str
    attempt: int
    state_root: Path
    progress_path: Path
    progress_heartbeat_cadence_s: float | None = None


class CampaignLifecycle(Protocol):
    """Project adapter boundary.  Methods may read state; only ``dispatch_one`` runs an arm."""

    def preflight(self, context: CampaignJobContext) -> str | None: ...
    def complete(self, context: CampaignJobContext) -> ProductionAliasRef | None: ...
    def terminal_outcome(
        self, context: CampaignJobContext,
    ) -> tuple[CampaignOutcome, str] | None: ...
    def blocking_diagnosis(self, context: CampaignJobContext) -> str | None: ...
    def trial_count(self, context: CampaignJobContext) -> int: ...
    def dispatch_one(self, context: CampaignJobContext) -> Sequence[str] | CampaignOutcomeRecord: ...
    def heartbeat(self, context: CampaignJobContext) -> None: ...
    def void_arm(self, context: CampaignJobContext, reason: str) -> None: ...


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
    required = (
        "preflight", "complete", "blocking_diagnosis", "trial_count", "dispatch_one",
        "heartbeat", "void_arm",
    )
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise CampaignJobError("supervision adapter is missing methods: " + ", ".join(missing))
    return adapter


class _ArmProcess:
    def __init__(self, *, progress_path: Path, heartbeat: Callable[[], None], heartbeat_s: float,
                 timeout_s: float) -> None:
        self.progress_path = progress_path
        self.heartbeat = heartbeat
        self.heartbeat_s = heartbeat_s
        self.timeout_s = timeout_s
        self.process: subprocess.Popen[str] | None = None
        self.failure_reason: str | None = None
        # An arm that exits non-zero streams its traceback through record_line and nowhere else:
        # the controller writes it to ITS stdout, which the portfolio does not retain.  On
        # 2026-08-27 that cost a01_person_model four full attempts and its whole retry budget --
        # every artifact of the failure was the string "arm exited 1", which names no cause.
        # Keep the tail so the recorded outcome can say what actually broke.
        self.output_tail: deque[str] = deque(maxlen=_OUTPUT_TAIL_LINES)

    def _terminate(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()

    def cancel(self) -> None:
        self._terminate()

    def failure_context(self) -> str:
        """The last lines the arm printed, for an outcome reason that names its cause."""
        if not self.output_tail:
            return ""
        joined = " | ".join(self.output_tail)
        if len(joined) > _OUTPUT_TAIL_CHARS:
            joined = "..." + joined[-(_OUTPUT_TAIL_CHARS - 3):]
        return joined

    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        if not command or not all(isinstance(item, str) and item for item in command):
            raise CampaignJobError("dispatch_one must return a non-empty argv sequence")
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            list(command), cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        reader_done = threading.Event()

        def read_output() -> None:
            try:
                assert self.process is not None and self.process.stdout is not None
                for line in self.process.stdout:
                    lines.put(line)
            finally:
                reader_done.set()

        reader = threading.Thread(target=read_output, name="campaign-output", daemon=True)
        reader.start()
        started = time.monotonic()
        last_progress = started
        progress_seen = False

        def record_line(line: str) -> None:
            nonlocal last_progress, progress_seen
            sys.stdout.write(line)
            sys.stdout.flush()
            stripped = line.rstrip()
            if stripped:
                self.output_tail.append(stripped)
            snapshot = parse_progress_line(line)
            if snapshot is not None:
                progress_seen = True
                last_progress = time.monotonic()
                write_progress_snapshot(self.progress_path, snapshot)
                self.heartbeat()

        try:
            while True:
                # Consume an already-arrived line before declaring its cadence missed. Otherwise
                # scheduling the monitor exactly on the boundary can kill a healthy reporter.
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    line = None
                if line is not None:
                    record_line(line)
                    continue

                now = time.monotonic()
                if self.process.poll() is None:
                    if now - started > self.timeout_s:
                        self.failure_reason = (
                            "VOIDED on throughput: arm exceeded wall-clock cap of "
                            f"{self.timeout_s:g} seconds"
                        )
                        self._terminate()
                    elif now - last_progress > (
                        self.heartbeat_s if progress_seen
                        else max(self.heartbeat_s, ARM_STARTUP_GRACE_S)
                    ):
                        self.failure_reason = (
                            "VOIDED on throughput: arm emitted no progress heartbeat inside its "
                            f"declared {self.heartbeat_s:g}-second cadence"
                        )
                        self._terminate()
                if self.process.poll() is not None and reader_done.is_set() and lines.empty():
                    break
                try:
                    line = lines.get(timeout=min(.05, self.heartbeat_s / 2, self.timeout_s / 2))
                except queue.Empty:
                    continue
                record_line(line)
        finally:
            if self.process.poll() is None:
                self._terminate()
            reader.join(timeout=1)
        return self.process.wait()


def _with_arm_output(reason: str, arm_output: str) -> str:
    """A refusal names its cause.  An exit code alone does not."""
    return f"{reason}; last arm output: {arm_output}" if arm_output else reason


def _disk_usage_bytes(root: Path) -> int:
    """Count one configured root and fail closed when any part cannot be inspected."""
    resolved = root.resolve(strict=True)
    if resolved.is_file():
        return resolved.stat().st_size
    total = 0

    def unreadable(error: OSError) -> None:
        raise error

    for directory, _subdirs, files in os.walk(resolved, onerror=unreadable, followlinks=False):
        for name in files:
            total += (Path(directory) / name).stat(follow_symlinks=False).st_size
    return total


class CampaignJob:
    """Port of the composing shell loop with one-arm nesting and typed outcomes."""

    def __init__(self, *, context: CampaignJobContext, adapter: CampaignLifecycle,
                 outcome_path: Path, max_iterations: int = 40, heartbeat_s: float = 300,
                 arm_timeout_s: float = DEFAULT_ARM_TIMEOUT_S,
                 disk_budget_bytes: int = DEFAULT_CAMPAIGN_DISK_BUDGET_BYTES,
                 corpus_cache_root: Path | None = None,
                 disk_roots: Sequence[Path] = (),
                 working_directory: Path | None = None) -> None:
        if max_iterations < 1:
            raise CampaignJobError("max_iterations must be positive")
        if heartbeat_s <= 0:
            raise CampaignJobError("heartbeat_s must be positive")
        if arm_timeout_s <= 0:
            raise CampaignJobError("arm_timeout_s must be positive")
        if (isinstance(disk_budget_bytes, bool) or not isinstance(disk_budget_bytes, int)
                or disk_budget_bytes <= 0):
            raise CampaignJobError("disk_budget_bytes must be a positive integer")
        self.context = replace(context, progress_heartbeat_cadence_s=heartbeat_s)
        self.adapter = adapter
        self.outcome_path = outcome_path
        self.max_iterations = max_iterations
        self.heartbeat_s = heartbeat_s
        self.arm_timeout_s = arm_timeout_s
        self.disk_budget_bytes = disk_budget_bytes
        external_roots = tuple(Path(root) for root in disk_roots)
        if corpus_cache_root is not None:
            external_roots = (Path(corpus_cache_root), *external_roots)
        self.disk_roots = (self.context.state_root, *external_roots)
        self.working_directory = working_directory or self.context.state_root
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

    def _disk_failure(self) -> str | None:
        roots: list[Path] = []
        for configured in self.disk_roots:
            try:
                resolved = configured.resolve(strict=True)
            except OSError as exc:
                return f"cannot read disk usage for {configured}: {exc}"
            if resolved not in roots:
                roots.append(resolved)
        total = 0
        for root in roots:
            try:
                total += _disk_usage_bytes(root)
            except OSError as exc:
                return f"cannot read disk usage for {root}: {exc}"
        if total > self.disk_budget_bytes:
            return (
                f"campaign disk budget exceeded: {total} bytes used across "
                f"{len(roots)} root(s), budget {self.disk_budget_bytes} bytes"
            )
        return None

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
                    CampaignOutcome.PROMOTED,
                    "campaign reports canonical completion",
                    production_alias,
                )
            if hasattr(self.adapter, "terminal_outcome"):
                declared = self.adapter.terminal_outcome(self.context)
                if declared is not None:
                    outcome, reason = declared
                    if outcome not in {
                        CampaignOutcome.MEASURED,
                        CampaignOutcome.REFUTED,
                        CampaignOutcome.ABANDONED,
                    }:
                        raise CampaignJobError(
                            "terminal_outcome must declare MEASURED, REFUTED, or ABANDONED"
                        )
                    return self._record(outcome, reason)
            blocker = self.adapter.blocking_diagnosis(self.context)
            if blocker:
                return self._record(CampaignOutcome.BLOCKED, blocker)
            disk_failure = self._disk_failure()
            if disk_failure:
                return self._record(CampaignOutcome.ABANDONED, disk_failure)
            before = self.adapter.trial_count(self.context)
            dispatch = self.adapter.dispatch_one(self.context)
            if isinstance(dispatch, CampaignOutcomeRecord):
                _atomic_json(self.outcome_path, dispatch.to_mapping())
                return dispatch
            self._arm = _ArmProcess(
                progress_path=self.context.progress_path,
                heartbeat=lambda: self.adapter.heartbeat(self.context),
                heartbeat_s=self.heartbeat_s,
                timeout_s=self.arm_timeout_s,
            )
            returncode = self._arm.run(dispatch, cwd=self.working_directory)
            void_reason = self._arm.failure_reason
            arm_output = self._arm.failure_context()
            self._arm = None
            if void_reason is not None:
                self.adapter.void_arm(self.context, void_reason)
                after = self.adapter.trial_count(self.context)
                if after <= before:
                    return self._record(
                        CampaignOutcome.STALLED,
                        f"iteration {iteration} was killed ({void_reason}) but its VOIDED "
                        "trial was not recorded",
                    )
                continue
            if self.cancelled or returncode in {130, 143, -signal.SIGTERM, -signal.SIGKILL}:
                return self._record(CampaignOutcome.CANCELLED, "arm process group was cancelled")
            if returncode != 0:
                return self._record(
                    CampaignOutcome.RETRYABLE,
                    _with_arm_output(f"arm exited {returncode}", arm_output),
                )
            after = self.adapter.trial_count(self.context)
            if after <= before:
                return self._record(
                    CampaignOutcome.STALLED,
                    _with_arm_output(
                        f"iteration {iteration} produced no new registry run", arm_output
                    ),
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
            arm_timeout_s=float(config.get("arm_timeout_s", DEFAULT_ARM_TIMEOUT_S)),
            disk_budget_bytes=int(config.get(
                "disk_budget_bytes", DEFAULT_CAMPAIGN_DISK_BUDGET_BYTES,
            )),
            corpus_cache_root=(
                None if config.get("corpus_cache_root") in (None, "")
                else Path(str(config["corpus_cache_root"]))
            ),
            disk_roots=tuple(Path(str(root)) for root in config.get("disk_roots", ())),
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
