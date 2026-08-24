"""Deterministic ticket-to-path attribution for post-merge verification.

The integration range can contain commits inherited from a moving default branch. Only commits
whose subject ends in the factory's exact ``(TICKET-ID)`` provenance marker belong to that ticket;
everything else is integration context and may be reviewed, but may not be billed to a ticket.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def collect_authorship(repo: Path, base: str, head: str,
                       tickets: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Return commits and paths authored by each ticket in ``base..head``.

    Exact trailing provenance is deliberately stricter than path overlap. A default-branch commit
    can touch the same file as a ticket without becoming that ticket's work.
    """
    wanted = {str(ticket) for ticket in tickets}
    out = {ticket: {"commits": [], "paths": []} for ticket in sorted(wanted)}
    for commit in _git(repo, "rev-list", "--reverse", f"{base}..{head}").splitlines():
        subject = _git(repo, "show", "-s", "--format=%s", commit).strip()
        for ticket in wanted:
            if not subject.endswith(f"({ticket})"):
                continue
            paths = _git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", commit
            ).splitlines()
            out[ticket]["commits"].append(commit)
            out[ticket]["paths"] = sorted(set(out[ticket]["paths"]) | set(paths))
            break
    return out


def verdict_authorship_errors(verdict: dict[str, Any],
                              authorship: dict[str, Any]) -> list[str]:
    """Reject any regression finding that is not located in its ticket's authored paths."""
    errors: list[str] = []
    for item in (verdict.get("regressed") or []) + (verdict.get("should_regress") or []):
        if not isinstance(item, dict):
            errors.append(f"bare regression {item!r} has no located authored paths")
            continue
        ticket = str(item.get("id") or item.get("ticket") or "").strip()
        paths = [str(path).strip() for path in (item.get("paths") or []) if str(path).strip()]
        owned = set((authorship.get(ticket) or {}).get("paths") or [])
        if not ticket:
            errors.append("regression object has no ticket id")
        elif not paths:
            errors.append(f"{ticket} regression has no paths; authorship cannot be checked")
        elif not owned:
            errors.append(f"{ticket} has no provenance-marked authored paths in this round")
        else:
            foreign = sorted(set(paths) - owned)
            if foreign:
                errors.append(f"{ticket} was blamed for paths it did not author: {foreign}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("tickets", nargs="+")
    args = parser.parse_args(argv)
    print(json.dumps(collect_authorship(args.repo, args.base, args.head, args.tickets), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the driver and unit tests
    raise SystemExit(main())
