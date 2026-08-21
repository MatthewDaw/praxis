from agent_factory.af_clean.findings import CLASS_CODE_DELETION, admit_finding
from agent_factory.af_clean.p3_legacy_shells import (
    P3_DIFF_ALLOWLIST,
    P3_LOCATIONS,
    P3_WITNESSES,
    p3_findings,
)


def test_p3_findings_are_exact_located_code_deletions() -> None:
    findings = p3_findings()
    assert len(findings) == len(P3_LOCATIONS) == 6
    assert {(item.location.file, item.location.line) for item in findings} == set(P3_LOCATIONS)
    assert {item.change_class for item in findings} == {CLASS_CODE_DELETION}
    assert all(admit_finding(item).admitted for item in findings)
    assert P3_DIFF_ALLOWLIST == {path for path, _line in P3_LOCATIONS}
    assert len(P3_WITNESSES) == 2
