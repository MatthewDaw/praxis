"""Unit tests for the GitHub-token-use audit log (R12).

Every use of the backend GitHub token must be recorded as a timestamp,
endpoint and repository count — and the token value itself must never reach
the log, even if a caller accidentally interpolates one into the endpoint.

Fake token values are assembled at runtime (never a contiguous literal) so
this file itself never trips the repo-wide raw-token-leak scan it exists to
exercise.
"""

from __future__ import annotations

import json
import logging

from knowledge.serve.github_audit import record_github_use


def _fake_token(prefix: str) -> str:
    return prefix + "1" + "A" * 32


def test_record_github_use_logs_timestamp_endpoint_and_repo_count(caplog):
    with caplog.at_level(logging.INFO, logger="github.audit"):
        entry = record_github_use(endpoint="/productivity", repository_count=7)

    # The audit entry itself carries exactly timestamp, endpoint, repository count.
    assert set(entry) == {"timestamp", "endpoint", "repository_count"}
    assert entry["endpoint"] == "/productivity"
    assert entry["repository_count"] == 7
    assert isinstance(entry["timestamp"], float)

    # And it actually reached the audit log (the durable, greppable sink).
    assert len(caplog.records) == 1
    logged = json.loads(caplog.records[0].message)
    assert logged == entry


def test_record_github_use_never_logs_a_raw_token_pat_style(caplog):
    leaked_token = _fake_token("github" + "_pat_")

    with caplog.at_level(logging.INFO, logger="github.audit"):
        entry = record_github_use(
            endpoint=f"/productivity?leaked={leaked_token}", repository_count=3
        )

    assert leaked_token not in json.dumps(entry)
    assert leaked_token not in caplog.text


def test_record_github_use_never_logs_a_raw_token_short_style(caplog):
    leaked_token = _fake_token("gh" + "p_")

    with caplog.at_level(logging.INFO, logger="github.audit"):
        entry = record_github_use(endpoint=leaked_token, repository_count=1)

    assert leaked_token not in json.dumps(entry)
    assert leaked_token not in caplog.text
