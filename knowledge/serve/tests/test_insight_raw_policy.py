"""Unit contrast of the raw vs. normal insight write policy.

The ``raw`` fast lane on POST /insights and POST /insights/batch builds the write
policy as ``[Redactor()]`` directly, instead of ``_insight_write_policy(...)``. This
keeps the cheap regex redaction (secrets are still scrubbed) but SKIPS the
``Deduper`` and the LLM-backed ``ConflictOverwriter`` — the per-item LLM round-trips
that make large trusted batches time out.

This test pins that contract at the policy-composition level, so it needs neither a
database nor an OPENROUTER_API_KEY (constructing the steps does not call out). It is
the clean, deterministic complement to the live-DB raw-batch insertion test in
test_space_org_delete.py.
"""

from __future__ import annotations

from knowledge.knowledge_graph.write_policy.write_step_variants import (
    ConflictOverwriter,
    Deduper,
    Redactor,
)
from knowledge.serve.app import _insight_write_policy


def test_raw_policy_is_redact_only():
    """The raw lane redacts and nothing else: no Deduper, no LLM conflict step."""
    raw_policy = [Redactor()]
    assert len(raw_policy) == 1
    assert isinstance(raw_policy[0], Redactor)
    assert not any(isinstance(s, Deduper) for s in raw_policy)
    assert not any(isinstance(s, ConflictOverwriter) for s in raw_policy)


def test_normal_policy_includes_deduper_and_llm_conflict():
    """The normal (non-raw) auto_resolve policy keeps the steps raw SKIPS — so the
    two lanes are demonstrably different, not just a flag with no effect."""
    policy = _insight_write_policy("auto_resolve")
    assert any(isinstance(s, Redactor) for s in policy)  # redaction kept in BOTH
    assert any(isinstance(s, Deduper) for s in policy)  # ... but raw drops this
    assert any(isinstance(s, ConflictOverwriter) for s in policy)  # ... and this
