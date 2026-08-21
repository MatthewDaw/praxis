"""Located P-3 findings prepared for deletion only after campaign migration."""

from __future__ import annotations

from .executable_diff import WitnessCommand
from .findings import CLASS_CODE_DELETION, Finding, Location


P3_RULE = "p3-retire-legacy-ml-shell-control-planes"
P3_LOCATIONS = (
    ("agent_factory/scripts/af-ml-campaign-loop.sh", 1),
    ("agent_factory/scripts/af-ml-campaign-queue.sh", 1),
    ("agent_factory/scripts/af-ml-agent-queue.sh", 1),
    ("agent_factory/scripts/af-ml-supervise-keepalive.sh", 1),
    ("agent_factory/scripts/launch-codex-dual-campaigns.sh", 1),
    ("agent_factory/scripts/af-ml-campaign-preflight.sh", 1),
)
P3_DIFF_ALLOWLIST = frozenset(path for path, _line in P3_LOCATIONS)
P3_WITNESSES = (
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests/test_campaign_job_runtime.py",
        "knowledge/ml_registry/tests/test_operator_cli.py",
        "knowledge/ml_registry/tests/test_runtime_process_ownership.py",
        "knowledge/ml_registry/tests/test_controller.py",
        "-q", "-p", "no:cacheprovider",
    )),
    WitnessCommand((
        "env", "PRAXIS_DB_DISABLED=1", "uv", "run", "pytest",
        "knowledge/ml_registry/tests", "-q", "-p", "no:cacheprovider",
    )),
)


def p3_findings() -> tuple[Finding, ...]:
    """Return admitted locations; this function never proposes or applies a diff."""
    return tuple(
        Finding(
            rule=P3_RULE,
            tier="enforce",
            location=Location(file=path, line=line),
            pole="bloat",
            change_class=CLASS_CODE_DELETION,
            proposal=(
                "delete this superseded shell control plane only after every live campaign uses "
                "campaign_job/agent_session and an independent blind verifier approves the exact diff"
            ),
        )
        for path, line in P3_LOCATIONS
    )
