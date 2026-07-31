"""af-clean's self-bootstrapping, isolated Praxis space (R40 / R15 / B39 / S3 / S4).

af-clean must assume **nothing** about what already lives in Praxis — it creates its own space per
target repository and never depends on ``prd-<project>``, ``planning-validation``,
``building-validation``, or any surface bindings existing (R15). Unlike the factory build hooks
(``hooks/_praxis.py``), which are a HARD dependency and fail closed, af-clean treats an unreachable or
unauthenticated Praxis backend as the **common case for a repo that never went through af-build** and
degrades instead: the findings ledger, liar ledger, and job inventory fall back to an on-disk store
kept **outside the target repo**, with a machine-readable ``degraded=true`` marker naming exactly which
capabilities are unavailable (§3.2).

Each target repo gets a space **namespaced to its own identity, with no cross-space read access**
(S4) — a run against repo B cannot read a fact (or a degraded-mode file) written by a run against repo
A. Anything this module writes to disk has common secret shapes redacted first (S3), matching the
redaction the in-Praxis findings/ledger path is expected to apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

_SPACE_PREFIX = "af-clean-"
_DEGRADED_STORE_DIRNAME = ".af-clean"
_IDENTITY_LEN = 16


def _git_remote_url(repo_root: Path) -> str | None:
    """The repo's ``origin`` remote URL, or ``None`` if there isn't one (no git, no remote)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = out.stdout.strip()
    return url or None


def repo_identity(repo_root: Path) -> str:
    """A short, stable identity for ``repo_root``, keyed to repo IDENTITY rather than checkout path.

    Prefers the git remote URL (so two checkouts of the same repo resolve to the same identity, and
    therefore the same isolated space/store); falls back to the repo's real absolute path when there
    is no remote (a local-only repo, or no git at all)."""
    remote = _git_remote_url(repo_root)
    basis = remote if remote else str(Path(repo_root).resolve())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_IDENTITY_LEN]


def space_name(repo_root: Path) -> str:
    """The Praxis space id af-clean uses for ``repo_root`` — a valid, namespaced slug (S4)."""
    return f"{_SPACE_PREFIX}{repo_identity(repo_root)}"


class BackendStatus(NamedTuple):
    """Whether a live, authenticated Praxis backend is available for THIS invocation.

    ``degraded`` is the single decision af-clean's caller needs: True means run in degraded local
    mode (§3.2) instead of failing closed. ``reasons`` names exactly why, never a silent flag."""

    reachable: bool
    authenticated: bool
    degraded: bool
    reasons: tuple[str, ...]


def backend_status() -> BackendStatus:
    """Probe whether af-clean can use a live Praxis backend, WITHOUT ever raising.

    Unlike the factory build hooks' fail-closed contract, an unreachable/unauthenticated backend is
    an ordinary, expected outcome here (the common case for a repo that never went through af-build),
    so every failure mode is captured as a reason and reported rather than raised."""
    reasons: list[str] = []

    if os.environ.get("PRAXIS_AUTH_DISABLED") == "1":
        return BackendStatus(reachable=True, authenticated=True, degraded=False, reasons=())

    api_key = os.environ.get("PRAXIS_API_KEY", "").strip()
    if not api_key:
        reasons.append("PRAXIS_API_KEY is unset and no dev auth-disabled seam is active")
        return BackendStatus(reachable=False, authenticated=False, degraded=True,
                             reasons=tuple(reasons))

    try:
        from hooks import _praxis  # local import: keep af-clean usable even if hooks/ isn't on sys.path
    except ImportError:
        reasons.append("factory hook client (hooks/_praxis.py) is not importable")
        return BackendStatus(reachable=False, authenticated=False, degraded=True,
                             reasons=tuple(reasons))

    try:
        who = _praxis.whoami()
    except _praxis.PraxisUnreachable as exc:
        reasons.append(f"Praxis unreachable: {exc}")
        return BackendStatus(reachable=False, authenticated=False, degraded=True,
                             reasons=tuple(reasons))

    if not who.ok:
        reasons.append(who.detail or "authentication failed")
        return BackendStatus(reachable=True, authenticated=False, degraded=True,
                             reasons=tuple(reasons))

    return BackendStatus(reachable=True, authenticated=True, degraded=False, reasons=())


def bootstrap_space(repo_root: Path) -> str:
    """Idempotently ensure ``repo_root``'s namespaced Praxis space exists; return its space id.

    A pre-existing space (this repo has run af-clean before) is a no-op, never an error — af-clean
    "assumes no existing space or snapshot" (B39) but must not fail when one is already there."""
    from hooks import _praxis  # local import, mirrors backend_status()

    sid = space_name(repo_root)
    try:
        _praxis._request("POST", "/spaces", body={"spaceId": sid, "name": f"af-clean: {sid}"})
    except _praxis.PraxisUnreachable as exc:
        if "HTTP 409" not in str(exc):
            raise
        # 409 == the space already exists; that is exactly the idempotent "already bootstrapped" case.
    return sid


# --------------------------------------------------------------------------- degraded local mode

def degraded_store_root(repo_root: Path) -> Path:
    """The on-disk fallback store for ``repo_root``'s degraded-mode run — always OUTSIDE the target
    repo, and namespaced per repo identity so repo B's degraded run cannot see repo A's files (S4)."""
    return Path.home() / _DEGRADED_STORE_DIRNAME / repo_identity(repo_root)


# Common secret shapes (S3): cloud access keys, bearer/API tokens, PEM key blocks, and credentials
# embedded in a connection-string URL. Deliberately broad (over-redacting a false positive is cheap;
# leaking a real secret into a findings ledger is not).
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),                                    # AWS access key id
    re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[abpr])-[A-Za-z0-9_-]{10,}"),  # common vendor token shapes
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s:@]+(?=@)"),                     # user:pass@ in a connection URL
)
_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace common secret shapes with ``[REDACTED]`` before anything is quoted into the corpus,
    findings, or ledger (S3). Never mutates text that doesn't match a known secret shape."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _redact_json_strings(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_json_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_json_strings(v) for k, v in value.items()}
    return value


def write_degraded_marker(repo_root: Path, *, unavailable: list[str],
                          reasons: list[str]) -> Path:
    """Write the machine-readable ``degraded=true`` marker naming which af-clean capabilities are
    unavailable and why, into the isolated on-disk store for ``repo_root`` (§3.2). Every string value
    is redacted (S3) before it touches disk — a reason may legitimately quote the offending env value.

    ``reasons`` must be non-empty: a degraded run always has a nameable cause (never a silent flag).
    """
    if not reasons:
        raise ValueError("write_degraded_marker requires at least one named reason")

    root = degraded_store_root(repo_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "degraded": True,
        "unavailable": list(unavailable),
        "reasons": list(reasons),
        "at": time.time(),
    }
    redacted = _redact_json_strings(payload)
    path = root / "degraded.json"
    path.write_text(json.dumps(redacted, indent=2, sort_keys=True), encoding="utf-8")
    return path
