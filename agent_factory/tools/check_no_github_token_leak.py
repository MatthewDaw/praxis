"""building-validation check `no-github-token-leak` (project: praxis).

Fails (exit 1) if a GitHub-token-shaped literal (``github_pat_`` or ``ghp_`` followed by
10+ word characters) appears in tracked source under ``knowledge/``, ``frontend-react/``,
``infra/``, or ``agent_factory/``.

Rewritten as a script (rather than an inline ``grep``/``!`` shell one-liner) because the
run-body validator (``ingestion_api._validate_run_body``) rejects both the negation verb
``!`` (outside the declared verb allowlist) and regex metacharacters like ``|``/``{``/``}``
even inside quotes — a plain ``python3 <this file>`` invocation carries neither.

``agent_factory/tests/test_ingestion_api_seam_fixes.py`` is excluded deliberately: its
``test_the_lesson_body_and_the_ticket_finding_are_both_redacted`` test needs a real
token-shaped literal in source to prove ``redact_secrets()`` actually redacts a leak — that
literal is load-bearing, not a leak.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("knowledge", "frontend-react", "infra", "agent_factory")
INCLUDE_SUFFIXES = (".py", ".ts", ".tsx", ".json", ".yml")
EXCLUDE_NAMES = {"test_ingestion_api_seam_fixes.py"}
TOKEN_RE = re.compile(r"(?:github_pat_|ghp_)[A-Za-z0-9_]{10,}")


def tracked_files() -> list[Path]:
    """Git-tracked files under SCAN_DIRS — never a gitignored build artifact (e.g.
    infra/cdk.out/, which the file's own original grep-based predecessor scanned blindly
    and false-positived on stale bundled copies of this very test file)."""
    out = subprocess.run(
        ["git", "ls-files", *SCAN_DIRS], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def find_leaks() -> list[str]:
    hits = []
    for path in tracked_files():
        if path.suffix not in INCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if TOKEN_RE.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{line.strip()}")
    return hits


def main() -> int:
    hits = find_leaks()
    if hits:
        print("no-github-token-leak: FOUND token-shaped literal(s):", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
