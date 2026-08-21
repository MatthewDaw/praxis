"""Typed projection of the canonical ``[progress]`` transport."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable


_STEP = re.compile(
    r"^\[progress\]\s+(?P<label>.+?)\s+(?P<current>\d+)/(?P<total>\d+)\s+"
    r"(?P<percent>\d+(?:\.\d+)?)%\s+elapsed\s+(?P<elapsed>\S+)\s+eta\s+(?P<eta>\S+)"
    r"(?:\s+last=(?P<metric>-?\d+(?:\.\d+)?))?"
)
_DONE = re.compile(
    r"^\[progress\]\s+(?P<label>.+?)\s+COMPLETE\s+(?P<current>\d+)\s+unit\(s\)\s+in\s+(?P<elapsed>\S+)"
)


@dataclass(frozen=True)
class ProgressSnapshot:
    label: str
    current: int
    total: int | None
    percent: float | None
    elapsed: str
    eta: str | None
    latest_metric: float | None
    complete: bool
    raw: str

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def parse_progress_line(line: str) -> ProgressSnapshot | None:
    """Parse one canonical line; warning and ordinary output are not progress state."""
    text = line.rstrip("\r\n")
    match = _STEP.match(text)
    if match:
        total = int(match.group("total"))
        current = int(match.group("current"))
        if total <= 0 or current < 0 or current > total:
            return None
        metric = match.group("metric")
        return ProgressSnapshot(
            match.group("label"), current, total, float(match.group("percent")),
            match.group("elapsed"), match.group("eta"),
            None if metric is None else float(metric), current == total, text,
        )
    match = _DONE.match(text)
    if match:
        return ProgressSnapshot(
            match.group("label"), int(match.group("current")), None, 100.0,
            match.group("elapsed"), None, None, True, text,
        )
    return None


def latest_progress(lines: Iterable[str]) -> ProgressSnapshot | None:
    latest = None
    for line in lines:
        parsed = parse_progress_line(line)
        if parsed is not None:
            latest = parsed
    return latest


def read_latest_progress(path: str | Path) -> ProgressSnapshot | None:
    try:
        with Path(path).open(errors="replace") as handle:
            return latest_progress(handle)
    except OSError:
        return None


def read_progress_snapshot(path: str | Path) -> ProgressSnapshot | None:
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            return None
        return ProgressSnapshot(**value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_progress_snapshot(path: str | Path, snapshot: ProgressSnapshot) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(snapshot.to_mapping(), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
