"""Turn a merged PR (or a commit) into a compact, distiller-ready document.

The slice's extraction step (the ``CommitIngestor``) distills *text*, so this
module's only job is to render one bounded text blob per unit: title + body +
review-thread comments + a files-changed summary + a truncated unified diff.

The thing that shells out to ``gh`` / ``git`` is **injected** (a ``Fetcher``
callable, argv -> stdout) — mirroring ``ClaudeCodeRunner.run_cli`` — so tests run
offline against fixture JSON and never hit the network or a real repo. ``gh``
auth (``gh auth login`` / ``GH_TOKEN``) is a *backfill* prerequisite, not a test
dependency.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable

# A fetcher runs one external command and returns its stdout. The full argv is
# passed (e.g. ["gh", "pr", "view", "48", "--json", "title,body,reviews"]) so the
# default just execs it and a fake can switch on argv.
Fetcher = Callable[[list[str]], str]

# Truncate the unified diff to roughly this many lines before feeding the distiller.
DIFF_LINE_CAP = 300

# File paths whose diff hunks are low-signal for durable knowledge — dropped first
# when the diff is over the cap.
_DEPRIORITIZED = ("tests/", "test_", ".lock", "lock.json", "package-lock",
                  "yarn.lock", "/generated/", ".snap", "__snapshots__/")


@dataclass
class PRDocument:
    """One merged PR (or commit) rendered to distiller-ready text."""

    unit_source: str  # "git/pr:<n>" | "git/commit:<sha>"
    title: str
    body: str
    reviews: list[str] = field(default_factory=list)
    diff_summary: str = ""  # files-changed list (the "--stat" surrogate)
    diff: str = ""  # truncated unified diff
    truncated: bool = False  # True when the diff was capped/trimmed

    def render(self) -> str:
        """Assemble the bounded text blob the distiller consumes."""
        parts = [f"UNIT: {self.unit_source}", f"TITLE: {self.title}", "", "BODY:", self.body]
        if self.reviews:  # omit the header entirely when there are none (no empty noise)
            parts += ["", "REVIEW COMMENTS:"]
            parts += [f"- {r}" for r in self.reviews]
        if self.diff_summary:
            parts += ["", "FILES CHANGED:", self.diff_summary]
        if self.diff:
            note = " (truncated)" if self.truncated else ""
            parts += ["", f"DIFF{note}:", self.diff]
        return "\n".join(parts)


def default_fetcher(argv: list[str]) -> str:
    """Run ``argv`` and return stdout; raise on a non-zero exit (never a silent empty)."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {detail}")
    return proc.stdout


def _is_deprioritized(path: str) -> bool:
    return any(token in path for token in _DEPRIORITIZED)


def _split_hunks(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into ``(path, hunk_text)`` per file (at ``diff --git``)."""
    hunks: list[tuple[str, str]] = []
    cur_path: str | None = None
    cur: list[str] = []

    def flush() -> None:
        if cur_path is not None and cur:
            hunks.append((cur_path, "\n".join(cur)))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            cur = [line]
            # "diff --git a/path b/path" -> take the b/ path (post-rename target)
            tail = line.split(" b/", 1)
            cur_path = tail[1].strip() if len(tail) == 2 else line[len("diff --git "):].strip()
        elif cur_path is None:
            continue  # preamble before the first file header (rare) — ignore
        else:
            cur.append(line)
    flush()
    return hunks


def summarize_diff(diff: str, *, cap: int = DIFF_LINE_CAP) -> tuple[str, str, bool]:
    """Render ``(files_summary, capped_diff, truncated)`` from a raw unified diff.

    High-signal file hunks come first; ``tests/`` / lockfile / generated hunks are
    de-prioritized to the tail and the whole thing is capped at ``cap`` lines.
    ``files_summary`` always lists *every* changed path (even ones dropped from the
    diff) so the distiller still sees the full file set.
    """
    hunks = _split_hunks(diff)
    if not hunks:  # diff had no per-file headers (e.g. empty or unparseable)
        lines = diff.splitlines()
        if len(lines) > cap:
            return "", "\n".join(lines[:cap]), True
        return "", diff, False

    files_summary = "\n".join(f"- {path}" for path, _ in hunks)

    signal = [h for h in hunks if not _is_deprioritized(h[0])]
    low = [h for h in hunks if _is_deprioritized(h[0])]
    ordered = signal + low

    out: list[str] = []
    truncated = bool(low) and len(signal) < len(hunks)  # dropping low-signal counts as trimming
    for _path, text in ordered:
        htext = text.splitlines()
        if len(out) + len(htext) > cap:
            out.extend(htext[: max(0, cap - len(out))])
            truncated = True
            break
        out.extend(htext)
    # Only the diff body was de-prioritized; if everything fit, low-signal hunks are
    # present too, so don't claim truncation in that case.
    if len(out) >= sum(len(t.splitlines()) for _p, t in hunks):
        truncated = False
    return files_summary, "\n".join(out), truncated


def build_pr_document(number: int, *, fetch: Fetcher = default_fetcher) -> PRDocument:
    """Fetch PR ``number`` via ``gh`` and render it to a ``PRDocument``."""
    raw = fetch(["gh", "pr", "view", str(number), "--json", "title,body,reviews"])
    data = json.loads(raw)
    reviews = [
        str(r.get("body", "")).strip()
        for r in data.get("reviews", [])
        if str(r.get("body", "")).strip()
    ]
    diff = fetch(["gh", "pr", "diff", str(number)])
    summary, capped, truncated = summarize_diff(diff)
    return PRDocument(
        unit_source=f"git/pr:{number}",
        title=str(data.get("title", "")).strip(),
        body=str(data.get("body", "")).strip(),
        reviews=reviews,
        diff_summary=summary,
        diff=capped,
        truncated=truncated,
    )


def build_commit_document(sha: str, *, fetch: Fetcher = default_fetcher) -> PRDocument:
    """Fetch commit ``sha`` via ``git`` and render it to a ``PRDocument``.

    The commit-fallback unit (for high-signal single-parent commits that didn't
    land as a fetchable PR). Subject -> title, body -> body, patch -> diff.
    """
    message = fetch(["git", "show", "-s", "--format=%s%n%x00%n%b", sha])
    subject, _, body = message.partition("\n\x00\n")
    diff = fetch(["git", "show", sha, "--format=", "--no-color"])
    summary, capped, truncated = summarize_diff(diff)
    return PRDocument(
        unit_source=f"git/commit:{sha}",
        title=subject.strip(),
        body=body.strip(),
        reviews=[],
        diff_summary=summary,
        diff=capped,
        truncated=truncated,
    )


def list_merged_prs(limit: int, *, fetch: Fetcher = default_fetcher) -> list[int]:
    """Return the numbers of the last ``limit`` merged PRs (newest first)."""
    raw = fetch(["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
                 "--json", "number,state"])
    data = json.loads(raw)
    # `--state merged` already filters, but re-check defensively so a fixture with a
    # stray open/draft entry is still excluded.
    return [int(d["number"]) for d in data if str(d.get("state", "MERGED")).upper() == "MERGED"]
