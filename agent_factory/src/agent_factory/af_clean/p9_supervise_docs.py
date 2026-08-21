"""Bounded ``/af-clean`` driver for the P-9 supervisor skill rewrite."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .executable_diff import ExecutableDiffResult, WitnessCommand, apply_bounded_executable_diff
from .findings import CLASS_DOCS_REWRITE, Finding, Location


P9_RULE = "p9-canonical-supervisor-skill"
P9_LOCATIONS = (("agent_factory/skills/af-ml-supervise/SKILL.md", 1),)
P9_DIFF_ALLOWLIST = frozenset({
    "agent_factory/skills/af-ml-supervise/SKILL.md",
    "agent_factory/tests/test_af_ml_supervise_skill_docs.py",
})
P9_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "agent_factory/tests/test_af_ml_supervise_skill_docs.py",
        "knowledge/ml_registry/tests/test_standard_campaign_fixture.py",
        "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "uvx", "ruff@0.15.20", "check",
        "agent_factory/tests/test_af_ml_supervise_skill_docs.py",
    )),
)


def p9_findings() -> tuple[Finding, ...]:
    return (
        Finding(
            rule=P9_RULE,
            tier="judgment",
            location=Location(file=P9_LOCATIONS[0][0], line=P9_LOCATIONS[0][1]),
            change_class=CLASS_DOCS_REWRITE,
            chunks=(
                "standard registry vocabulary",
                "write authorities",
                "run and verdict state machine",
                "arm commit lifecycle",
                "idea dependency semantics",
                "ratchet and finalization",
                "operator safety and reporting",
                "executable generic fixture",
            ),
            proposal=(
                "replace the obsolete supervisor instructions with the canonical standard-registry "
                "lifecycle while preserving adjudication, staging, dependency, ratchet, safety, and "
                "reporting invariants"
            ),
        ),
    )


def generate_prebuilt_diff(
    repo_root: str | Path, candidate_ref: str, *, base_ref: str = "HEAD",
) -> str:
    root = Path(repo_root).resolve()
    result = subprocess.run(
        [
            "git", "diff", "--no-ext-diff", "--binary", base_ref, candidate_ref,
            "--", *sorted(P9_DIFF_ALLOWLIST),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"could not generate P-9 diff: {result.stderr.strip() or 'no output'}")
    if not result.stdout.strip():
        raise ValueError("generated P-9 diff is empty")
    return result.stdout


def read_prebuilt_diff(path: str | Path) -> str:
    diff_path = Path(path)
    if not diff_path.is_file():
        raise ValueError(f"prebuilt P-9 diff is not a file: {diff_path}")
    try:
        content = diff_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"prebuilt P-9 diff is not UTF-8: {diff_path}") from exc
    if not content.strip():
        raise ValueError("prebuilt P-9 diff is empty")
    return content


def apply_p9_diff(
    repo_root: str | Path, diff_path: str | Path, **adapter_overrides: object,
) -> ExecutableDiffResult:
    forbidden = {
        "diff", "findings", "expected_rule", "expected_locations", "diff_allowlist",
        "witnesses", "change_class",
    }.intersection(adapter_overrides)
    if forbidden:
        raise TypeError(f"P-9 safety boundary cannot be overridden: {sorted(forbidden)!r}")
    return apply_bounded_executable_diff(
        repo_root=repo_root,
        diff=read_prebuilt_diff(diff_path),
        findings=p9_findings(),
        expected_rule=P9_RULE,
        expected_locations=frozenset(P9_LOCATIONS),
        diff_allowlist=P9_DIFF_ALLOWLIST,
        witnesses=P9_WITNESSES,
        change_class=CLASS_DOCS_REWRITE,
        **adapter_overrides,
    )
