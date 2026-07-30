"""af-clean's scar detection on defensive code (R23 / B21).

Before af-clean removes anything in the defensive family — try/except, null guards, odd special
cases, narrow branches — it blames the lines first. When the introducing commit's message matches
``fix``, ``bug``, ``regression``, ``hotfix``, or ``incident``, or cites an issue/pull-request
reference, the finding is a **scar**: it is demoted to ``advisory`` and the commit is cited in the
reason, rather than left eligible for removal. A construct introduced by an ordinary commit (e.g.
``add feature``) is unaffected and remains ``eligible``.

This module never removes anything itself — it only classifies. The caller (whatever locates
removal candidates) is expected to skip/downgrade a finding whose ``detect_scar`` verdict is
``advisory``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

# Commit-message keywords that mark the introducing change as a bugfix / incident response rather
# than routine feature work (R23's literal list: fix, bug, regression, hotfix, incident). Matched as
# a whole word, case-insensitively, so "prefix" or "debug" do not false-positive.
_SCAR_KEYWORDS = re.compile(
    r"\b(fix(?:e[sd])?|bug|regression|hotfix|incident)\b", re.IGNORECASE,
)

# An issue or pull-request reference: "#123", "GH-123", "gh-123", or the words "issue"/"pull
# request" followed by a number.
_ISSUE_REF = re.compile(
    r"(?:^|[\s(])#\d+\b"
    r"|\bgh-\d+\b"
    r"|\bissue\s*#?\d+\b"
    r"|\bpull\s+request\s*#?\d+\b",
    re.IGNORECASE,
)


class BlamedCommit(NamedTuple):
    """One commit that touched a blamed line: its short hash and full message."""

    sha: str
    subject: str
    body: str


class ScarFinding(NamedTuple):
    """The scar-detection verdict for one defensive-code construct.

    ``verdict`` is ``"advisory"`` (a scar — demoted, keep) or ``"eligible"`` (no scar found, still a
    removal candidate on whatever other grounds located it). ``reason`` is human-readable and, for an
    ``advisory`` verdict, always cites the triggering commit's short hash and subject (R23)."""

    construct: str
    file: str
    line_start: int
    line_end: int
    verdict: str
    commits: tuple[BlamedCommit, ...]
    reason: str


def classify_commit_message(subject: str, body: str = "") -> bool:
    """True if this commit message marks its change as a scar (bugfix/incident response, or citing
    an issue/PR) — i.e. the construct it introduced should be demoted to advisory rather than
    removed outright."""
    text = f"{subject}\n{body}"
    return bool(_SCAR_KEYWORDS.search(text) or _ISSUE_REF.search(text))


def _run_git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return out.stdout


def blame_lines(repo_root: Path, file_path: str, line_start: int, line_end: int | None = None,
                ) -> tuple[BlamedCommit, ...]:
    """The distinct commits that introduced/last-touched lines ``[line_start, line_end]`` of
    ``file_path`` in ``repo_root``, in blame order, deduplicated, each with its full commit message
    so ``classify_commit_message`` can inspect it."""
    end = line_end if line_end is not None else line_start
    porcelain = _run_git(
        repo_root, "blame", "-L", f"{line_start},{end}", "--porcelain", "--", file_path,
    )
    shas: list[str] = []
    for line in porcelain.splitlines():
        if re.match(r"^[0-9a-f]{40}(\s|$)", line):
            sha = line.split()[0]
            if sha not in shas:
                shas.append(sha)

    commits = []
    for sha in shas:
        raw = _run_git(repo_root, "log", "-1", "--format=%s%x1e%b", sha)
        subject, _, body = raw.partition("\x1e")
        commits.append(BlamedCommit(sha=sha[:12], subject=subject.strip(), body=body.strip()))
    return tuple(commits)


def detect_scar(repo_root: Path, file_path: str, line_start: int, line_end: int | None = None, *,
                construct: str = "try/except") -> ScarFinding:
    """Blame ``construct`` at ``file_path:line_start-line_end`` and classify it: ``advisory`` (a
    scar — cite the commit, do not remove) when any blamed commit's message matches
    ``classify_commit_message``, otherwise ``eligible`` for removal on whatever other grounds located
    it (R23 / B21)."""
    end = line_end if line_end is not None else line_start
    commits = blame_lines(repo_root, file_path, line_start, end)

    scar_commits = tuple(c for c in commits if classify_commit_message(c.subject, c.body))
    if scar_commits:
        cited = ", ".join(f'{c.sha} "{c.subject}"' for c in scar_commits)
        span = f"-{line_end}" if line_end and line_end != line_start else ""
        reason = (
            f"{construct} at {file_path}:{line_start}{span} "
            f"was introduced by a fix/incident commit ({cited}); demoted to advisory"
        )
        return ScarFinding(construct, file_path, line_start, end, "advisory", scar_commits, reason)

    reason = (
        f"{construct} at {file_path}:{line_start} shows no scar-marked introducing commit; "
        "eligible for removal"
    )
    return ScarFinding(construct, file_path, line_start, end, "eligible", commits, reason)
