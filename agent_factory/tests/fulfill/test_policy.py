"""U9 (policy) — budget (S1) and guardrail (S9) tests."""

from __future__ import annotations

import pytest

from agent_factory.fulfill.policy import Budget, BudgetExhausted, Guardrails


def test_budget_hard_refuses_past_limit(domain):
    b = Budget(5)
    for _ in range(5):
        b.spend()
    assert b.remaining == 0
    assert not b.can_ask()
    with pytest.raises(BudgetExhausted):
        b.spend()  # the 6th question is refused


def test_budget_from_domain(domain):
    b = Budget.from_domain(domain)
    assert b.max == 5  # policy.yaml budget.max_asks


def test_guardrail_1099_refused(domain):
    v = Guardrails(domain).scope_check("here is my 1099-MISC for some contract work")
    assert not v.allowed
    assert v.rule_id == "OUT_OF_SCOPE_1099"
    assert v.action == "refuse"


def test_guardrail_advice_refused(domain):
    v = Guardrails(domain).scope_check("how do I avoid paying taxes this year?")
    assert not v.allowed
    assert v.rule_id == "SCOPE_NO_ADVICE"


def test_guardrail_pii_is_soft_redact(domain):
    g = Guardrails(domain)
    v = g.scope_check("my ssn is 123-45-6789")
    assert not v.allowed
    assert v.rule_id == "PII_REJECT"
    assert v.action == "redact"
    assert "[REDACTED-SSN]" in g.redact("ssn 123-45-6789")


def test_guardrail_allows_normal_message(domain):
    assert Guardrails(domain).scope_check("my filing status is single").allowed
