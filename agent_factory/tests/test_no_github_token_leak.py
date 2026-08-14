"""building-validation check `no-github-token-leak` (project: praxis).

A GitHub-token-shaped literal (``github_pat_`` or ``ghp_`` followed by 10+ word characters)
must never appear in git-tracked source. Wired as a pytest test (rather than a machine-
authored shell one-liner) because ``ingestion_api``'s run-body validator only allows a
machine-drafted `python3` command in the form ``python3 -m pytest ...`` — this test file is
that form's natural home, and it exercises the reusable scanner in
``agent_factory/tools/check_no_github_token_leak.py`` directly.

``agent_factory/tests/test_ingestion_api_seam_fixes.py`` is excluded deliberately by the
scanner itself: its ``test_the_lesson_body_and_the_ticket_finding_are_both_redacted`` test
needs a real token-shaped literal in source to prove ``redact_secrets()`` actually redacts a
leak -- that literal is load-bearing, not a leak.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.check_no_github_token_leak import find_leaks


def test_no_github_token_leak() -> None:
    hits = find_leaks()
    assert not hits, "GitHub-token-shaped literal(s) found in git-tracked source:\n" + "\n".join(hits)
