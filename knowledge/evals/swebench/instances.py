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

The leakage screen is deliberately conservative (see :func:`screen_leakage`): it records
a verdict on *every* chosen instance and never silently drops a candidate — flagged
instances are kept and marked so the manifest stays a complete, auditable set (R2).
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
    leak_flag: bool = False
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
            "leak_flag": self.leak_flag,
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


def screen_leakage(inst: Instance) -> tuple[bool, str]:
    """Conservative solution-in-issue leakage heuristic → ``(leak_flag, reason)``.

    Two overlaps flag an instance, in priority order:

    1. A changed *symbol name* introduced by the gold diff also appears (word-bounded)
       in the issue text — the issue likely names the fix's new identifier.
    2. A substantive *added line* of the gold diff (a stripped code line ≥12 chars,
       skipping comments/blank/import noise) appears verbatim in the issue — the issue
       likely pastes the fix.

    Conservative by design: it harvests symbols only from *added* lines, requires word
    boundaries, and ignores short/trivial lines, so it under-flags rather than over-flags.
    A clean instance returns ``(False, "no problem_statement / gold overlap")``. The
    verdict is always recorded (R2 — no silent inclusion); selection keeps flagged
    instances and marks them.
    """
    issue = inst.problem_statement
    if not issue or not inst.patch:
        return False, "no problem_statement / gold overlap"

    added = [m.group(1) for line in inst.patch.splitlines() if (m := _ADDED.match(line))]

    # (1) a symbol introduced by the diff is named in the issue
    issue_idents = set(_IDENT.findall(issue))
    for line in added:
        for sym in _IDENT.findall(line):
            if sym in issue_idents and not _common_word(sym):
                return True, f"changed symbol '{sym}' appears in problem_statement"

    # (2) a substantive added code line is pasted verbatim in the issue
    for line in added:
        stripped = line.strip()
        if len(stripped) >= 12 and not _trivial_line(stripped) and stripped in issue:
            return True, f"gold added line in problem_statement: {stripped[:60]!r}"

    return False, "no problem_statement / gold overlap"


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


def select(candidates: Iterable[Instance], n: int) -> list[Instance]:
    """Filter to supported versions, sort by ``created_at`` desc, screen each, keep top ``n``.

    Flagged instances are **not** dropped — every kept instance gets its screen verdict
    written onto it so the manifest is a complete, auditable set (R2). Sort is
    deterministic: ``created_at`` desc, then ``instance_id`` asc as a stable tiebreak.
    """
    supported = [c for c in candidates if version_supported(c.version)]
    supported.sort(key=lambda c: (c.created_at, c.instance_id), reverse=True)
    chosen = supported[:n]
    for inst in chosen:
        inst.leak_flag, inst.screen_reason = screen_leakage(inst)
    return chosen


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
