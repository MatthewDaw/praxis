"""building-validation check `no-core-derived-cap` (project: af-ml-research).

af-build's concurrency admission (R15) counts a ticket against ONE of two fixed lanes —
``max_cpu_parallel`` / ``max_gpu_parallel`` — named by its ``meta.device`` (R16), never a formula
derived from the host's CPU core count (``os.cpu_count()``, ``multiprocessing.cpu_count()``,
``len(os.sched_getaffinity(0))``, ``nproc``, ...). A cores-minus-N expression makes the cap depend
on whatever box happens to run the loop rather than the two-lane contract R15 fixes, so this scanner
fails (exit 1) if any such expression appears in tracked ``agent_factory/`` source.

Rewritten as a script (rather than an inline shell one-liner) for the same reason
``check_no_github_token_leak.py`` is: the run-body validator rejects negation/regex metacharacters
outside quotes, so a plain ``python3 -m pytest <this test>`` invocation carries neither.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("agent_factory",)
INCLUDE_SUFFIXES = (".py",)
# This scanner's own literals (in this docstring/pattern) would otherwise self-match.
EXCLUDE_NAMES = {"check_no_core_derived_cap.py", "test_no_core_derived_cap.py"}
CORE_COUNT_RE = re.compile(
    r"(os\.cpu_count|multiprocessing\.cpu_count|len\(os\.sched_getaffinity|"
    r"\bnproc\b|psutil\.cpu_count)"
)


def tracked_files() -> list[Path]:
    """Git-tracked files under SCAN_DIRS — never a gitignored build artifact."""
    out = subprocess.run(
        ["git", "ls-files", *SCAN_DIRS], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def find_core_derived_expressions() -> list[str]:
    hits = []
    for path in tracked_files():
        if path.suffix not in INCLUDE_SUFFIXES or path.name in EXCLUDE_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if CORE_COUNT_RE.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{line.strip()}")
    return hits


def main() -> int:
    hits = find_core_derived_expressions()
    if hits:
        print("no-core-derived-cap: FOUND core-count-derived expression(s):", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
