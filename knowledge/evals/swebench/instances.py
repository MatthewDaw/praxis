"""Select + leakage-screen a recent SWE-rebench sympy slice into a committed manifest.

This is U1 of the PR-knowledge eval pilot. It establishes the :class:`Instance`
data shape that every downstream unit (U2 grader, U3 ingest, U5 runner) consumes,
so the dataclass carries *everything* downstream needs in one place: the canonical
SWE-bench fields, SWE-rebench's self-contained ``install_config`` (verbatim), the
gold-changed file list, and a per-instance leakage verdict.

Two seams keep the unit pure and offline-testable:

* The HuggingFace ``datasets`` load lives behind :func:`fetch_rebench_sympy`, imported
  lazily so this module imports with no network and no ``datasets`` installed. Tests
  feed fixture records straight to :func:`load_candidates` and never hit the network.
* Version support is gated on the official ``MAP_REPO_VERSION_TO_SPECS`` coverage
  (sympy 1.12–1.14), passed as a plain tuple so the test pins it without importing
  swebench.

The committed ``instances.manifest.json`` is **not** hand-authored — it is produced by
a live ``--refresh`` against ``nebius/SWE-rebench`` (real instance ids), then reviewed.
This module provides the read/write/round-trip code; the fixture under ``tests/`` holds
only fake SWE-rebench-shaped records for the offline tests.

The leakage screen (see :func:`screen_leakage`) is two-tier: a STRONG verbatim-fix-line
match (disqualifying) vs a WEAK changed-symbol mention (informational — issues normally
name the broken function, so this fires on ~most instances and is NOT real leakage). Only
verbatim leaks are excluded by :func:`select`; every screened instance keeps its verdict
(R2). :func:`select` also takes ``order="recent"|"hard"`` to bias toward harder bugs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Versions present in the official ``MAP_REPO_VERSION_TO_SPECS["sympy/sympy"]`` that the
# arm64 grader (U2) can build. A candidate whose version is outside this set is dropped.
SUPPORTED_VERSIONS = ("1.12", "1.13", "1.14")

# Identifier-ish tokens, used to harvest "changed symbol names" from the gold diff and to
# match them against the issue text on word boundaries (avoids substring false positives).
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

# A diff added line (single leading '+', not the '+++ ' file header).
_ADDED = re.compile(r"^\+(?!\+\+ )(.*)$")


@dataclass
class Instance:
    """One SWE-rebench instance, carrying everything U2/U3/U5 need plus a screen verdict."""

    instance_id: str
    repo: str
    version: str
    base_commit: str
    created_at: str
    problem_statement: str
    patch: str  # gold fix diff
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    install_config: dict  # verbatim from the record (install, test_cmd, log_parser, python, ...)
    gold_files: list[str]  # b/ paths parsed from the gold ``patch``
    # Two leakage tiers (see screen_leakage). leak_verbatim is the DISQUALIFYING one — a
    # gold fix line pasted into the issue. leak_symbol is INFORMATIONAL — the issue merely
    # names a changed symbol (normal; issues name the broken function). Only leak_verbatim
    # excludes from a run.
    leak_verbatim: bool = False
    leak_symbol: bool = False
    screen_reason: str = ""
    human_reviewed: bool = False

    @classmethod
    def from_record(cls, record: dict) -> "Instance":
        """Build an Instance from a raw SWE-rebench record (no screening applied yet)."""
        patch = str(record.get("patch", ""))
        return cls(
            instance_id=str(record["instance_id"]),
            repo=str(record.get("repo", "")),
            version=str(record.get("version", "")),
            base_commit=str(record.get("base_commit", "")),
            created_at=str(record.get("created_at", "")),
            problem_statement=str(record.get("problem_statement", "")),
            patch=patch,
            test_patch=str(record.get("test_patch", "")),
            fail_to_pass=_as_str_list(record.get("FAIL_TO_PASS")),
            pass_to_pass=_as_str_list(record.get("PASS_TO_PASS")),
            install_config=dict(record.get("install_config") or {}),
            gold_files=gold_files(patch),
        )

    def to_manifest_row(self) -> dict:
        """The lean committed row — enough to reconstruct selection + reruns deterministically."""
        return {
            "instance_id": self.instance_id,
            "version": self.version,
            "base_commit": self.base_commit,
            "created_at": self.created_at,
            "gold_files": list(self.gold_files),
            "leak_verbatim": self.leak_verbatim,
            "leak_symbol": self.leak_symbol,
            "screen_reason": self.screen_reason,
            "human_reviewed": self.human_reviewed,
        }


def _as_str_list(value) -> list[str]:
    """SWE-bench ships FAIL_TO_PASS/PASS_TO_PASS as a list or a JSON-encoded string."""
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else [value]
    return [str(v) for v in value]


def gold_files(patch: str) -> list[str]:
    """Parse the changed file paths (the ``b/`` targets) out of a gold unified diff.

    Reuses the ``diff --git a/… b/…`` convention from ``pr_source._split_hunks``: the
    ``b/`` path is the post-rename target, which is what the grader/agent see on disk.
    """
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            tail = line.split(" b/", 1)
            path = tail[1].strip() if len(tail) == 2 else line[len("diff --git "):].strip()
            if path and path not in files:
                files.append(path)
    return files


def load_candidates(records: Iterable[dict]) -> list[Instance]:
    """Pure: build Instances from raw records (no filtering, no screening)."""
    return [Instance.from_record(r) for r in records]


def version_supported(version: str, supported: tuple[str, ...] = SUPPORTED_VERSIONS) -> bool:
    """True iff ``version`` is one the official spec map (and so the grader) covers."""
    return version in supported


def screen_leakage(inst: Instance) -> tuple[bool, bool, str]:
    """Solution-in-issue leakage screen → ``(leak_verbatim, leak_symbol, reason)``.

    TWO tiers, very different in strength — the whole point of splitting them is that the
    weak one fires constantly and must NOT disqualify an instance:

    * ``leak_verbatim`` (STRONG, disqualifying) — a substantive *added line* of the gold
      diff (stripped code ≥12 chars, skipping comment/import/decorator noise) appears
      verbatim in the issue. The issue literally pastes the fix; running it tests nothing.
    * ``leak_symbol`` (WEAK, informational) — a *symbol* the fix changes is merely named
      in the issue. This is NORMAL: an issue about a broken function names that function.
      It is recorded for auditing but does **not** exclude the instance (empirically ~79%
      of sympy instances trip it, vs ~1% for verbatim — treating it as leakage throws away
      almost the whole pool for no validity gain).

    Verbatim is checked first and wins (an instance that pastes a fix line is verbatim-
    leaked regardless of symbol mentions). A clean instance returns
    ``(False, False, "no problem_statement / gold overlap")``. The verdict is always
    recorded (R2 — no silent inclusion).
    """
    issue = inst.problem_statement
    if not issue or not inst.patch:
        return False, False, "no problem_statement / gold overlap"

    added = [m.group(1) for line in inst.patch.splitlines() if (m := _ADDED.match(line))]

    # (1) STRONG: a substantive added code line is pasted verbatim in the issue.
    for line in added:
        stripped = line.strip()
        if is_substantive_line(stripped) and stripped in issue:
            return True, False, f"gold added line in problem_statement: {stripped[:60]!r}"

    # (2) WEAK: a symbol introduced by the diff is named in the issue (not disqualifying).
    issue_idents = set(_IDENT.findall(issue))
    for line in added:
        for sym in _IDENT.findall(line):
            if sym in issue_idents and not _common_word(sym):
                return False, True, f"changed symbol '{sym}' named in problem_statement (weak)"

    return False, False, "no problem_statement / gold overlap"


def gold_patch_lines(inst: Instance) -> int:
    """Count of changed (+/-) code lines in the gold patch — a rough fix-complexity proxy."""
    return len([
        ln for ln in inst.patch.splitlines()
        if ln[:1] in "+-" and ln[:3] not in ("---", "+++")
    ])


# Identifiers too generic to count as a "changed symbol" leak on their own.
_COMMON = frozenset({
    "self", "None", "True", "False", "return", "import", "from", "def", "class",
    "value", "result", "args", "kwargs", "test", "assert", "raise", "and", "not",
    "for", "the", "this", "with", "type", "list", "dict", "str", "int",
})


def _common_word(sym: str) -> bool:
    return sym in _COMMON


def _trivial_line(stripped: str) -> bool:
    """Comment / import / decorator lines are low-signal for a verbatim-paste match."""
    return stripped.startswith(("#", "import ", "from ", "@", '"""', "'''"))


def is_substantive_line(stripped: str) -> bool:
    """A diff line specific enough that its verbatim reappearance is a real leak signal.

    Long enough to be distinctive (≥12 stripped chars) and not comment/import/decorator/
    docstring noise. Shared by the selection-time screen (:func:`screen_leakage`) and the
    runtime ``ingest.leakage_guard`` so both agree on what a leak-bearing line is — a bare
    ``return`` or other short keyword line is NOT one (matching it aborts runs spuriously).
    """
    return len(stripped) >= 12 and not _trivial_line(stripped)


def select(
    candidates: Iterable[Instance],
    n: int,
    *,
    order: str = "recent",
    exclude_leaked: bool = True,
    since: str | None = None,
) -> list[Instance]:
    """Filter to supported versions, screen every candidate, then order + keep top ``n``.

    Every supported candidate is screened (its leak verdict written on, R2 — no silent
    inclusion). With ``exclude_leaked`` (default), only **verbatim**-leaked instances are
    dropped — NOT the weak symbol-mention tier, which fires on most instances and isn't
    real leakage. ``order`` is ``"recent"`` (created_at desc — the default) or ``"hard"``
    (gold-patch size desc, then files / failing-tests / recency), which biases toward the
    harder bugs where a no-knowledge control plausibly fails and Praxis has headroom.
    ``since`` (a ``YYYY-MM-DD`` date) keeps only instances created on or after it — the
    least-contaminated, nearest-the-training-cutoff slice; it composes with ``order``
    (e.g. ``order="hard", since="2025-02-01"`` = the recent-and-hard corner).
    Deterministic tiebreaks throughout (instance_id).
    """
    supported = [c for c in candidates if version_supported(c.version)]
    for inst in supported:
        inst.leak_verbatim, inst.leak_symbol, inst.screen_reason = screen_leakage(inst)

    pool = [c for c in supported if not (exclude_leaked and c.leak_verbatim)]
    if since is not None:
        # created_at is naive 'YYYY-MM-DD ...' or RFC3339-Z; the date prefix compares
        # lexically in both shapes, so a 10-char slice is enough for a date-only cutoff.
        pool = [c for c in pool if c.created_at[:10] >= since]
    if order == "hard":
        pool.sort(
            key=lambda c: (gold_patch_lines(c), len(c.gold_files), len(c.fail_to_pass),
                           c.created_at, c.instance_id),
            reverse=True,
        )
    else:  # "recent"
        pool.sort(key=lambda c: (c.created_at, c.instance_id), reverse=True)
    return pool[:n]


def write_manifest(instances: Iterable[Instance], path: str | Path) -> None:
    """Write the lean ``to_manifest_row()`` rows as pretty JSON (round-trips via read_manifest)."""
    rows = [inst.to_manifest_row() for inst in instances]
    Path(path).write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_manifest(path: str | Path) -> list[dict]:
    """Read manifest rows back as plain dicts."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fetch_rebench_sympy(limit: int | None = None) -> list[dict]:
    """Load ``nebius/SWE-rebench`` (HF ``datasets``, split ``test``), filter to sympy.

    The only network-touching seam. ``datasets`` is imported lazily so this module — and
    the offline tests — import with neither the package nor a network connection present.
    Returns raw records (dicts); pass them to :func:`load_candidates`.
    """
    from datasets import load_dataset  # lazy: keep module import offline-clean

    ds = load_dataset("nebius/SWE-rebench", split="test")
    rows = [dict(r) for r in ds if r.get("repo") == "sympy/sympy"]
    return rows[:limit] if limit is not None else rows
