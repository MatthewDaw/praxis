"""R11 (B10/idea 12) — af-clean's per-comment triage by information gain, never by density.

Acceptance: a comment restating its function signature is proposed for deletion; a comment
stating a reason, invariant, cost or rejected alternative is never proposed for deletion; and an
ambiguous comment survives by default.
"""

from __future__ import annotations

import pytest

from agent_factory.af_clean_comment_triage import classify_comment, signature_tokens


def test_acceptance_signature_restating_comment_is_eligible_for_deletion():
    tokens = signature_tokens("get_user_by_id", ["user_id"])

    finding = classify_comment("Get the user by id.", tokens)

    assert finding.verdict == "eligible"
    assert "restat" in finding.reason.lower()


@pytest.mark.parametrize(
    "comment",
    [
        "Retry because the upstream API is flaky under load.",
        "Invariant: the queue is never empty at this point.",
        "O(n log n) here because we sort the whole batch up front.",
        "Rejected a hash map here since collisions were too costly at this scale.",
    ],
    ids=["reason", "invariant", "cost", "rejected-alternative"],
)
def test_acceptance_why_comments_are_never_proposed_for_deletion(comment):
    tokens = signature_tokens("process", ["batch"])

    finding = classify_comment(comment, tokens)

    assert finding.verdict == "protected"


def test_acceptance_ambiguous_comment_survives_by_default():
    tokens = signature_tokens("run", ["config"])

    # Half the content words are covered by the signature ("run", "config"), half are novel
    # ("mind", "widget"), without tripping any WHY marker -- neither a clean restatement nor a
    # clear rationale.
    finding = classify_comment("Run config, mind the widget.", tokens)

    assert finding.verdict == "ambiguous"


def test_classify_comment_empty_content_is_ambiguous():
    tokens = signature_tokens("run", ["config"])

    finding = classify_comment("---", tokens)

    assert finding.verdict == "ambiguous"


def test_signature_tokens_includes_name_and_param_words():
    tokens = signature_tokens("get_user_by_id", ["user_id"])

    assert {"get", "user", "by", "id"}.issubset(tokens)
