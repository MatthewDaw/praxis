"""Acceptance test for the GitHub token audit log (ticket R12).

Given a productivity request (simulated by one call into the audit module —
the sole write path a productivity-endpoint handler is expected to use), an
audit entry must exist carrying ``timestamp``, ``endpoint`` and
``repo_count``, and a search of the audit log for a ``github_pat_``-shaped
token must return nothing — even when a caller's ``endpoint`` string
accidentally contains one.
"""

from __future__ import annotations

from knowledge.serve import github_audit


def test_productivity_request_records_audit_entry_without_leaking_token(tmp_path):
    log_path = tmp_path / "github_audit.log"
    # Built from parts (never a literal token-shaped string in source) so this test
    # file itself never trips the repo-wide token-leak scan.
    fake_token = "github_pat_" + "11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz01234567890"

    entry = github_audit.record_github_token_use(
        endpoint=f"GET /productivity?token={fake_token}",
        repo_count=7,
        log_path=log_path,
    )

    # The returned entry carries exactly the required fields.
    assert entry["repo_count"] == 7
    assert "timestamp" in entry and entry["timestamp"]
    assert "endpoint" in entry and entry["endpoint"]

    # The persisted log has one entry with the same shape.
    entries = github_audit.read_audit_log(log_path)
    assert len(entries) == 1
    assert entries[0]["repo_count"] == 7
    assert "timestamp" in entries[0]
    assert "endpoint" in entries[0]

    # A literal search of the raw log file for the token prefix finds nothing.
    raw = log_path.read_text(encoding="utf-8")
    assert "github_pat_" not in raw
    assert fake_token not in raw
    assert not github_audit.contains_token_leak(log_path)


def test_audit_log_never_records_the_token_value_itself(tmp_path):
    log_path = tmp_path / "github_audit.log"
    real_looking_token = "ghp_" + "z" * 36

    github_audit.record_github_token_use(
        endpoint="GET /productivity", repo_count=3, log_path=log_path
    )
    # The function has no parameter to accept a token at all, so a caller
    # cannot pass one in even by accident via the normal call shape; this
    # guards the invariant explicitly for anyone extending the signature.
    raw = log_path.read_text(encoding="utf-8")
    assert real_looking_token not in raw
    assert not github_audit.contains_token_leak(log_path)


def test_read_audit_log_empty_when_file_absent(tmp_path):
    missing = tmp_path / "does-not-exist.log"
    assert github_audit.read_audit_log(missing) == []
    assert github_audit.contains_token_leak(missing) is False
