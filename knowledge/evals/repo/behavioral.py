"""Run the target tests in the checkout's venv and grade the outcome.

The behavioral oracle (SWE-bench style): the case passes iff every FAIL_TO_PASS
node passes AND every PASS_TO_PASS node stays green. The gold commit is never an
answer-key — only its tests are.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Matches pytest's short-summary "FAILED <nodeid> - ..." / "ERROR <nodeid>" lines.
_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


@dataclass
class RunOutcome:
    failed: set[str]
    raw: str


def run_tests(python: Path, dest: Path, node_ids: list[str]) -> RunOutcome:
    """Run the given pytest node ids; return the set that failed/errored."""
    if not node_ids:
        return RunOutcome(failed=set(), raw="")
    proc = subprocess.run(
        [str(python), "-m", "pytest", *node_ids, "--tb=no", "-q", "-p", "no:cacheprovider"],
        cwd=str(dest),
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    # Match reported failures back to the requested node ids (pytest may print
    # the path with forward slashes; compare on the test-name tail too).
    reported = set(_FAILED.findall(out))
    failed = {
        nid
        for nid in node_ids
        if nid in reported or any(r.endswith(nid.split("::", 1)[-1]) for r in reported)
    }
    return RunOutcome(failed=failed, raw=out)


def grade(
    python: Path,
    dest: Path,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
) -> tuple[bool, str]:
    """Return (passed, evidence). Passed iff no target node failed."""
    result = run_tests(python, dest, [*fail_to_pass, *pass_to_pass])
    f2p_fail = sorted(n for n in fail_to_pass if n in result.failed)
    p2p_fail = sorted(n for n in pass_to_pass if n in result.failed)
    if not f2p_fail and not p2p_fail:
        return True, f"all {len(fail_to_pass) + len(pass_to_pass)} target tests pass"
    parts = []
    if f2p_fail:
        parts.append(f"FAIL_TO_PASS still failing: {f2p_fail}")
    if p2p_fail:
        parts.append(f"PASS_TO_PASS regressed: {p2p_fail}")
    return False, "; ".join(parts)
