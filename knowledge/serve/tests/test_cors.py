"""CORS origin regex allows localhost + the AWS-generated frontend/backend hosts."""

from __future__ import annotations

import re

import pytest

from knowledge.serve.app import _cors_origin_regex


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "https://praxis-dash.onrender.com",
        "https://abc-123.cloudfront.net",
        "https://praxis-backend-xyz.awsapprunner.com",
        "https://app.praxiskg.com",
        "https://mcp.praxiskg.com",
    ],
)
def test_cors_regex_allows(origin: str) -> None:
    assert re.fullmatch(_cors_origin_regex(), origin)


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "http://cloudfront.net",
        "https://abc.cloudfront.net.evil.com",
        "https://app.praxiskg.com.evil.com",
    ],
)
def test_cors_regex_rejects(origin: str) -> None:
    assert not re.fullmatch(_cors_origin_regex(), origin)
