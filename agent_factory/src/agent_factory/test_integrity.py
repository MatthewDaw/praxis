"""Deterministic guard against making a test easier in order to make it pass."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$)|\.(test|spec)\.[^.]+$|_test\.py$")
_ASSERTION = re.compile(r"\bassert\b|\bself\.assert[A-Z]|\bexpect\s*\(")
_EXACT = re.compile(r"\bassert\b.*(?:==|!=)|\.to(?:Equal|StrictEqual|Be)\s*\(")
_MEMBERSHIP = re.compile(r"\bassert\b.*\b(?:in|not in)\b|\.toContain\s*\(")


def _git_diff(repo: Path, base: str, head: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--unified=0", f"{base}..{head}", "--"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def findings_from_diff(diff: str) -> list[dict[str, str]]:
    """Locate deleted assertions and exact-comparison-to-membership weakenings."""
    findings: list[dict[str, str]] = []
    path = ""
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        if not path or not _TEST_PATH.search(path):
            removed.clear()
            added.clear()
            return
        removed_assertions = [line for line in removed if _ASSERTION.search(line)]
        if not removed_assertions:
            removed.clear()
            added.clear()
            return
        weakened = any(_EXACT.search(line) for line in removed_assertions) and any(
            _MEMBERSHIP.search(line) for line in added if _ASSERTION.search(line)
        )
        rule = "exact-comparison-weakened-to-membership" if weakened else "deleted-assertion"
        findings.append({
            "path": path,
            "rule": rule,
            "evidence": removed_assertions[0].strip()[:300],
        })
        removed.clear()
        added.clear()

    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            flush()
            path = line[4:].removeprefix("b/")
        elif line.startswith("@@"):
            flush()
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    flush()
    return findings


def collect_findings(repo: Path, base: str, head: str = "HEAD") -> list[dict[str, str]]:
    return findings_from_diff(_git_diff(repo, base, head))


def integrity_verdict_errors(verdict: dict[str, Any], authorship: dict[str, Any],
                             findings: list[dict[str, str]]) -> list[str]:
    """Every integrity finding must be assigned to the ticket that authored its path."""
    covered: set[tuple[str, str]] = set()
    for item in (verdict.get("regressed") or []) + (verdict.get("should_regress") or []):
        if not isinstance(item, dict):
            continue
        ticket = str(item.get("id") or item.get("ticket") or "")
        for path in item.get("paths") or []:
            covered.add((ticket, str(path)))

    errors: list[str] = []
    for finding in findings:
        path = finding["path"]
        owners = [ticket for ticket, data in authorship.items()
                  if path in set((data or {}).get("paths") or [])]
        if not owners:
            # The merged range can contain default-branch commits inherited through alignment.
            # Review may report them as debt, but this round owns no remediation and no ticket may
            # be regressed for them.
            continue
        elif not any((owner, path) in covered for owner in owners):
            errors.append(
                f"test-integrity {finding['rule']} in {path} was not regressed against its "
                f"author ({', '.join(owners)})"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    print(json.dumps(collect_findings(args.repo, args.base, args.head), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
