"""Shared scaffold for the factory's two Stop-hook gates (``build_completeness_gate`` /
``plan_completeness_gate``): the identical hook I/O, project resolution, no-op transcript scan,
and PraxisUnreachable classification. Each gate keeps its own ARM/ENFORCE logic, signal tuple,
and all block/allow MESSAGE text — only the mechanical scaffold lives here."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable

#: Above this transcript size, don't fast-path — fall through to the safe (fail-closed) read.
MAX_TRANSCRIPT_SCAN_BYTES = 8 * 1024 * 1024


def allow(advice: str = "") -> None:
    """Emit an ALLOW (optionally with additionalContext) and exit — byte-identical to no hook
    when ``advice`` is empty."""
    if advice:
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": advice}
        }))
    sys.exit(0)


def block(reason: str) -> None:
    """Emit a BLOCK with ``reason`` and exit."""
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def active_project(cwd: str) -> str:
    """Resolve the active ``prd-<project>`` from ``FACTORY_PROJECT`` (with or without a ``prd-``
    prefix) else the cwd basename — NEVER a manifest file."""
    raw = os.environ.get("FACTORY_PROJECT", "").strip()
    if not raw:
        raw = os.path.basename(os.path.normpath(cwd or os.getcwd()))
    raw = raw.strip()
    if not raw:
        return ""
    return raw if raw.startswith("prd-") else f"prd-{raw}"


def session_touched(transcript_path: str, signals: Iterable[str]) -> bool | None:
    """``False`` == cleanly read the transcript and found ZERO of ``signals`` (a provable no-op);
    ``True`` == a signal is present; ``None`` == unknown (missing/unreadable/oversized). Only a
    confident ``False`` lets a gate stand down WITHOUT a Praxis read; ``True``/``None`` fall through
    to the hard, fail-closed read, so this can never fail a real session open."""
    if not transcript_path:
        return None
    try:
        p = os.path.expanduser(str(transcript_path))
        if not os.path.isfile(p) or os.path.getsize(p) > MAX_TRANSCRIPT_SCAN_BYTES:
            return None
        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read().lower()
    except Exception:  # noqa: BLE001 — any read problem => unknown, fall through to the safe path
        return None
    return any(sig in text for sig in signals)


def classify_unreachable(exc: Exception) -> tuple[bool, str]:
    """``(is_unreachable, detail)``: whether ``exc`` is a Praxis ``PraxisUnreachable`` (any
    import/transport failure is treated as unreachable too — the truth is unavailable either way),
    plus a human ``detail`` string for the fail-closed block message."""
    try:
        from _praxis import PraxisUnreachable
        is_unreachable = isinstance(exc, PraxisUnreachable)
    except Exception:  # noqa: BLE001
        is_unreachable = True
    detail = str(exc) if is_unreachable else f"{type(exc).__name__}: {exc}"
    return is_unreachable, detail
