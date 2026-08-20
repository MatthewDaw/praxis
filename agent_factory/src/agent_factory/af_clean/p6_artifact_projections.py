"""Bounded ``/af-clean`` driver for audited P-6 artifact-view consolidation.

This records the consolidation boundary only. It cannot construct or apply a patch on
its own: a separately committed candidate diff must pass the byte-exact projection
witness and the full registry suite before blind consolidation review may admit it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_CONSOLIDATION, Finding, Location


P6_RULE = "p6-artifact-view-consolidation"
P6_LOCATIONS = (
    ("knowledge/ml_registry/manifests.py", 233),
    ("knowledge/ml_registry/artifact_cache.py", 80),
    ("knowledge/ml_registry/portfolio.py", 108),
)
P6_DIFF_ALLOWLIST = frozenset({
    "knowledge/ml_registry/manifests.py",
    "knowledge/ml_registry/artifact_cache.py",
    "knowledge/ml_registry/portfolio.py",
    "knowledge/ml_registry/storage/artifact_store.py",
    "knowledge/ml_registry/storage/projections.py",
    "knowledge/ml_registry/tests/test_artifact_projection_golden.py",
})
P6_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_artifact_projection_golden.py",
        "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
)


def p6_findings() -> tuple[Finding, ...]:
    """Return the three located, mechanically admissible P-6 findings."""
    return tuple(
        Finding(
            rule=P6_RULE,
            tier="judgment",
            location=Location(file=file, line=line),
            pole="fragmentation",
            change_class=CLASS_CONSOLIDATION,
            chunks=("artifact identity", "manifest lineage", "persistence", "projection bytes"),
            is_dry=True,
            observable="co-change",
            proposal=(
                "project the byte-compatible legacy view from the immutable artifact store "
                "without caller-selectable policy"
            ),
        )
        for file, line in P6_LOCATIONS
    )


def read_prebuilt_diff(path: str | Path) -> str:
    diff_path = Path(path)
    if not diff_path.is_file():
        raise ValueError(f"prebuilt P-6 diff is not a file: {diff_path}")
    try:
        content = diff_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"prebuilt P-6 diff is not UTF-8: {diff_path}") from exc
    if not content.strip():
        raise ValueError(f"prebuilt P-6 diff is empty: {diff_path}")
    return content


def generate_prebuilt_diff(
    repo_root: str | Path, candidate_ref: str, *, base_ref: str = "HEAD",
) -> str:
    root = Path(repo_root).resolve()
    argv = [
        "git", "diff", "--no-ext-diff", "--binary", base_ref, candidate_ref, "--",
        *sorted(P6_DIFF_ALLOWLIST),
    ]
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"could not generate prebuilt P-6 diff: {result.stderr.strip() or 'no output'}")
    if not result.stdout.strip():
        raise ValueError("generated P-6 diff is empty")
    return result.stdout


def apply_p6_diff(
    repo_root: str | Path, diff_path: str | Path, **adapter_overrides: object,
) -> ExecutableDiffResult:
    """Admit, witness, blindly verify, and apply an exact external P-6 patch."""
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(adapter_overrides)
    if forbidden:
        raise TypeError(f"P-6 safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=p6_findings(),
        expected_rule=P6_RULE,
        expected_locations=frozenset(P6_LOCATIONS),
        diff_allowlist=P6_DIFF_ALLOWLIST,
        witnesses=P6_WITNESSES,
        change_class=CLASS_CONSOLIDATION,
        **adapter_overrides,
    )
