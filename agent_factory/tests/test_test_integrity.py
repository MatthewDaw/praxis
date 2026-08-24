"""The integration lens fails closed when a ticket weakens a test."""

from __future__ import annotations

from agent_factory.test_integrity import findings_from_diff, integrity_verdict_errors


def test_deleting_an_assertion_is_a_finding() -> None:
    diff = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -8 +7,0 @@
-    assert call.argv == ["one", "two"]
"""
    assert findings_from_diff(diff) == [{
        "path": "tests/test_x.py",
        "rule": "deleted-assertion",
        "evidence": 'assert call.argv == ["one", "two"]',
    }]


def test_exact_positional_assertion_cannot_become_membership_checks() -> None:
    """Regression: swapped argv 1/2 satisfied both new substring checks."""
    diff = """diff --git a/tests/test_concurrency.py b/tests/test_concurrency.py
--- a/tests/test_concurrency.py
+++ b/tests/test_concurrency.py
@@ -385 +385,2 @@
-    assert call.strip() == EXACT_HEARTBEAT_CALL
+    assert "af_round_heartbeat" in call
+    assert "hb_open" in call
"""
    got = findings_from_diff(diff)
    assert got[0]["rule"] == "exact-comparison-weakened-to-membership"


def test_adding_stronger_assertions_does_not_false_positive() -> None:
    diff = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -8,0 +9 @@
+    assert call.argv == ["one", "two"]
"""
    assert findings_from_diff(diff) == []


def test_integrity_finding_must_regress_the_ticket_that_authored_the_path() -> None:
    finding = [{"path": "tests/test_x.py", "rule": "deleted-assertion", "evidence": "assert x"}]
    authorship = {"R3a": {"paths": ["tests/test_x.py"]}}
    ignored = {"verdict": "pass", "regressed": []}
    assert "was not regressed" in integrity_verdict_errors(ignored, authorship, finding)[0]

    handled = {"verdict": "fail", "regressed": [
        {"id": "R3a", "paths": ["tests/test_x.py"], "reason": "deleted assertion"}
    ]}
    assert integrity_verdict_errors(handled, authorship, finding) == []


def test_integrity_finding_in_an_inherited_path_is_not_billed_to_the_round() -> None:
    finding = [{"path": "tests/inherited.py", "rule": "deleted-assertion", "evidence": "assert x"}]
    authorship = {"R3a": {"paths": ["src/owned.py"]}}
    assert integrity_verdict_errors({"verdict": "pass", "regressed": []},
                                    authorship, finding) == []
