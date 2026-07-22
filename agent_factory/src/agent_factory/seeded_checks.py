"""Loader for the file-backed seeded generic-check library (U1).

The library lives in ``agent_factory/seeded_checks.toml`` — a single, append-friendly file that
is the sole source of truth for the generic reusable checks the factory offers the
check-authoring agent as opt-in candidates during RESOLVE (U3). Adding a check is a one-block
edit to that file, never a code change.

This module only PARSES and VALIDATES the file into typed records; it makes no Praxis calls and
does not decide applicability (that is U3's candidate lane). A malformed file raises at load
time so a bad check definition fails loudly, never silently.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .rubric import Rubric, rubric_from_dict

BINARY = "binary"
GRADED = "graded"
_KINDS = (BINARY, GRADED)

# agent_factory/seeded_checks.toml, resolved relative to the package root (…/agent_factory).
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "seeded_checks.toml"


@dataclass(frozen=True)
class SeededCheck:
    """One generic check from the library. ``rubric`` is set iff ``kind == 'graded'``."""

    check_id: str
    kind: str
    applies_to: tuple[str, ...]
    criterion: str = ""
    run: str = ""
    promote_universal: bool = False
    rubric: Rubric | None = None


def _parse_check(raw: dict) -> SeededCheck:
    check_id = str(raw.get("check_id") or "").strip()
    if not check_id:
        raise ValueError("seeded check requires a check_id")
    kind = str(raw.get("kind") or BINARY).strip().casefold()
    if kind not in _KINDS:
        raise ValueError(f"check {check_id!r}: kind must be one of {_KINDS}, got {kind!r}")
    applies_to = tuple(str(t).strip() for t in (raw.get("applies_to") or ["*"]) if str(t).strip())
    if not applies_to:
        applies_to = ("*",)

    rubric: Rubric | None = None
    run = str(raw.get("run") or "").strip()
    if kind == GRADED:
        try:
            rubric = rubric_from_dict(raw)
        except ValueError as exc:
            raise ValueError(f"check {check_id!r}: {exc}") from exc
    elif not run:
        raise ValueError(f"check {check_id!r}: binary check requires a run command")

    return SeededCheck(
        check_id=check_id,
        kind=kind,
        applies_to=applies_to,
        criterion=str(raw.get("criterion") or ""),
        run=run,
        promote_universal=bool(raw.get("promote_universal", False)),
        rubric=rubric,
    )


def load_seeded_checks(path: str | Path | None = None) -> list[SeededCheck]:
    """Parse and validate the seeded-check library. Raises ``ValueError`` on any malformed entry
    or duplicate ``check_id``; raises ``FileNotFoundError`` if the file is missing.
    """
    p = Path(path) if path is not None else _DEFAULT_PATH
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    checks = [_parse_check(raw) for raw in (data.get("check") or [])]
    seen: set[str] = set()
    for c in checks:
        if c.check_id in seen:
            raise ValueError(f"duplicate check_id in seeded library: {c.check_id!r}")
        seen.add(c.check_id)
    return checks
