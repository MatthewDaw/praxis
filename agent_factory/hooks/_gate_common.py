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


#: A Praxis answer that means "there is no factory project HERE" rather than "Praxis is down".
#: Both are HTTP errors and both arrive as PraxisUnreachable, but they demand opposite behaviour:
#: an outage must fail CLOSED (the truth exists and we cannot see it), while a project/org that
#: does not exist must stand DOWN (there is no truth to check). Conflating them is why a plain
#: `claude` session in ANY unrelated directory got a blocking "PRAXIS UNREACHABLE" Stop-hook
#: error: these gates install at USER scope, so they run everywhere, resolve a project from the
#: cwd basename, and then read a space that was never meant to exist.
_NOT_A_FACTORY_PROJECT = (
    "unknown space",            # 404 — the cwd basename is not a Praxis space
    "is not scoped to org",     # 403 — this key does not own the org we fell back to
    "unknown org",
    "no such space",
)


def not_a_factory_project(exc: Exception) -> bool:
    """True when ``exc`` says the project/space/org does not exist or is not ours.

    This is a CONFIGURATION answer, not an availability one. It is deliberately matched on the
    server's message text rather than a status code because the same 404/403 codes legitimately
    mean other things elsewhere; the phrases above are the ones the Praxis API uses for "that
    space/org is not a thing", and a false positive here can only ever cause a gate to stand down
    in a directory that has no factory project to gate.
    """
    text = str(exc).lower()
    return any(phrase in text for phrase in _NOT_A_FACTORY_PROJECT)


def factory_configured(cwd: str) -> bool:
    """Cheap, LOCAL, network-free test for "is this directory set up for the factory at all?".

    Runs before any Praxis call so an unrelated repo never even opens a socket -- the gates are
    user-scoped and therefore execute in every session, and a hook that phones home from every
    directory is both slow and, when it fails, indistinguishable from a real outage.

    Configured means one of: ``FACTORY_PROJECT`` is pinned in the environment (what a build
    worktree's ``.claude/settings.local.json`` sets), or such a settings file exists at the cwd or
    an ancestor and names the factory/Praxis. Nothing here proves the project EXISTS in Praxis --
    that is still the gates' job -- it only proves someone meant this directory to be one.
    """
    if os.environ.get("FACTORY_PROJECT", "").strip():
        return True
    # `.env` is checked as well as `.claude/settings.local.json` because FACTORY_PROJECT is also
    # supplied that way, and the gates load the dotenv AFTER this point -- so reading only the
    # environment here would stand a genuinely-configured project down (caught by
    # test_factory_project_from_dotenv_wins_over_cwd_basename, not by inspection).
    markers = ("FACTORY_PROJECT", "PRAXIS_API_KEY", "PRAXIS_ORG", "PRAXIS_API_BASE_URL")
    # BOTH the session's directory and the process's own: the hook payload's ``cwd`` is where the
    # user is, while the dotenv the gate loads a few lines later is found relative to the process.
    # Either one carrying factory config means "gate this". In the case that actually matters -- a
    # plain session in an unrelated repo -- the two are the same directory and neither is configured,
    # so this stays silent there while a genuinely-configured project is never stood down.
    roots = [os.path.abspath(cwd or os.getcwd())]
    try:
        proc_cwd = os.path.abspath(os.getcwd())
        if proc_cwd not in roots:
            roots.append(proc_cwd)
    except Exception:  # noqa: BLE001 — a deleted cwd is not a reason to fail the check
        pass
    return any(_configured_at_or_above(root, markers) for root in roots)


def _configured_at_or_above(start: str, markers: tuple[str, ...]) -> bool:
    """Walk from ``start`` to the filesystem root looking for factory/Praxis configuration."""
    here = start
    while True:
        for candidate in (os.path.join(here, ".claude", "settings.local.json"),
                          os.path.join(here, ".env"),
                          os.path.join(here, "agent_factory", ".env")):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8", errors="ignore") as fh:
                    blob = fh.read()
            except Exception:  # noqa: BLE001 — unreadable config proves nothing either way
                continue
            if any(m in blob for m in markers):
                return True
        parent = os.path.dirname(here)
        if parent == here:
            return False
        here = parent


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
