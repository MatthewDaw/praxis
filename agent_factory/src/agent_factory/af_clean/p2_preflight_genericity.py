"""Bounded P-2 finding for removing project knowledge from generic preflight."""

from __future__ import annotations

from .executable_diff import WitnessCommand
from .findings import CLASS_CONSOLIDATION, Finding, Location


P2_RULE = "p2-externalize-preflight-project-knowledge"
P2_LOCATIONS = (("knowledge/ml_registry/preflight.py", 144),)
P2_DIFF_ALLOWLIST = frozenset(path for path, _line in P2_LOCATIONS)
P2_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_preflight_manifest.py",
        "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "uv", "run", "ruff", "check",
        "knowledge/ml_registry/preflight.py",
        "knowledge/ml_registry/tests/test_preflight_manifest.py",
    )),
    WitnessCommand((
        "env", "PRAXIS_ROOT=.", "bash",
        "/Users/matthewdaw/Documents/official_repos/sports_analysis/"
        "scripts/af-ml-campaign-preflight.sh",
        "--help",
    )),
    WitnessCommand((
        "env", "PRAXIS_ROOT=.", "bash",
        "/Users/matthewdaw/Documents/official_repos/sports_analysis/"
        "scripts/af-ml-campaign-preflight-all.sh",
        "--help",
    )),
)


def p2_findings() -> tuple[Finding, ...]:
    return (
        Finding(
            rule=P2_RULE,
            tier="enforce",
            location=Location(file=P2_LOCATIONS[0][0], line=P2_LOCATIONS[0][1]),
            pole="bloat",
            change_class=CLASS_CONSOLIDATION,
            proposal=(
                "remove embedded project roster, paths, probes, policy, and defaults after the "
                "versioned manifest path is characterized; preserve behavior through manifest inputs"
            ),
        ),
    )
