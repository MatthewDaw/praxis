"""Bounded ``/af-clean`` driver for the audited P-8 registry CLI split."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_SPLIT, Finding, Location


P8_RULE = "p8-ml-registry-cli-split"
P8_LOCATIONS = (("knowledge/ml_registry/cli.py", 1),)
P8_DIFF_ALLOWLIST = frozenset({
    "knowledge/ml_registry/cli.py",
    "knowledge/ml_registry/cli/__init__.py",
    "knowledge/ml_registry/cli/__main__.py",
    "knowledge/ml_registry/cli/registry.py",
    "knowledge/ml_registry/cli/portfolio.py",
    "knowledge/ml_registry/cli/manifests.py",
    "knowledge/ml_registry/portfolio_cli.py",
    "knowledge/ml_registry/manifests_cli.py",
    "knowledge/ml_registry/tests/test_cli_split_golden.py",
})
P8_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_cli_split_golden.py",
        "knowledge/ml_registry/tests/test_cli.py",
        "knowledge/ml_registry/tests/test_campaign_path.py",
        "knowledge/ml_registry/tests/test_space_lock.py",
        "knowledge/ml_registry/tests/test_space_lock_timeout.py",
        "knowledge/ml_registry/tests/test_manifests_cli.py",
        "knowledge/ml_registry/tests/test_portfolio_cli.py",
        "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
)


def p8_findings() -> tuple[Finding, ...]:
    """Return the single located structural-split finding."""
    return (
        Finding(
            rule=P8_RULE,
            tier="judgment",
            location=Location(file=P8_LOCATIONS[0][0], line=P8_LOCATIONS[0][1]),
            change_class=CLASS_SPLIT,
            chunks=(
                "public imports", "module entrypoint", "command help", "dispatch and errors",
                "stdout stderr and exit codes", "persistence boundaries",
            ),
            is_dry=True,
            observable="co-change",
            proposal=(
                "split the registry, portfolio, and manifest command groups behind byte-compatible "
                "facades without changing any caller-observable behavior"
            ),
        ),
    )


def read_prebuilt_diff(path: str | Path) -> str:
    diff_path = Path(path)
    if not diff_path.is_file():
        raise ValueError(f"prebuilt P-8 diff is not a file: {diff_path}")
    try:
        content = diff_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"prebuilt P-8 diff is not UTF-8: {diff_path}") from exc
    if not content.strip():
        raise ValueError(f"prebuilt P-8 diff is empty: {diff_path}")
    return content


def generate_prebuilt_diff(
    repo_root: str | Path, candidate_ref: str, *, base_ref: str = "HEAD",
) -> str:
    """Generate a compact, reviewable diff that preserves structural rename metadata."""
    root = Path(repo_root).resolve()
    argv = [
        "git", "diff", "--no-ext-diff", "--find-renames", "--binary",
        base_ref, candidate_ref, "--", *sorted(P8_DIFF_ALLOWLIST),
    ]
    result = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"could not generate prebuilt P-8 diff: {result.stderr.strip() or 'no output'}")
    if not result.stdout.strip():
        raise ValueError("generated P-8 diff is empty")
    return result.stdout


def apply_p8_diff(
    repo_root: str | Path, diff_path: str | Path, **adapter_overrides: object,
) -> ExecutableDiffResult:
    """Admit, witness, blindly verify, and apply an exact external P-8 patch."""
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(adapter_overrides)
    if forbidden:
        raise TypeError(f"P-8 safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=p8_findings(),
        expected_rule=P8_RULE,
        expected_locations=frozenset(P8_LOCATIONS),
        diff_allowlist=P8_DIFF_ALLOWLIST,
        witnesses=P8_WITNESSES,
        change_class=CLASS_SPLIT,
        allow_renames=True,
        **adapter_overrides,
    )
