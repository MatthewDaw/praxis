"""U9 (policy half) — the ask budget (S1) and scope guardrails (S9), enforced as code, not prompt.

- :class:`Budget` is the ONLY channel that emits a question to the user, and it decrements a hard
  counter. Past the limit it refuses — the agent cannot ask an N+1-th question because the act of
  asking is gated here, not in a system prompt. Inferring / defaulting / covering-from-a-fact is free
  (only ``via=ask`` decrements).
- :class:`Guardrails` gives a typed refusal with a STABLE rule id for out-of-scope input / advice /
  PII, so a hostile turn (a pasted 1099, a tax-advice request, an SSN) gets a named refusal — never a
  fabricated line. The stable ids live in the domain's ``policy.yaml``; the detection predicate for
  each id lives here (the generic runtime enforces; the domain declares which ids apply).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .domain import Domain


class BudgetExhausted(Exception):
    """Raised when the runtime tries to ask past the question budget (S1)."""


class Budget:
    """The hard ask counter. Only :meth:`spend` (an actual question) decrements it."""

    def __init__(self, max_asks: int) -> None:
        self.max = max_asks
        self.asked = 0

    @classmethod
    def from_domain(cls, domain: Domain) -> "Budget":
        budget = (domain.policy.get("budget") or {})
        return cls(int(budget.get("max_asks", 5)))

    @property
    def remaining(self) -> int:
        return max(0, self.max - self.asked)

    def can_ask(self) -> bool:
        return self.remaining > 0

    def spend(self) -> int:
        """Consume one question. Raises :class:`BudgetExhausted` past the limit."""
        if self.asked >= self.max:
            raise BudgetExhausted(
                f"question budget of {self.max} is exhausted; remaining requirements must be "
                f"defaulted with a documented assumption instead of asking again"
            )
        self.asked += 1
        return self.remaining


@dataclass(frozen=True)
class Verdict:
    """A guardrail decision. ``allowed`` False carries a stable ``rule_id`` + ``reason``; ``action``
    is ``refuse`` (block the turn) or ``redact`` (strip + continue, e.g. PII)."""

    allowed: bool
    rule_id: str = ""
    reason: str = ""
    action: str = "refuse"


# Detection predicates keyed by the STABLE rule ids the domain's policy.yaml declares. A declared id
# with no predicate here cannot be enforced (skipped) — predicates are code, ids are data.
_PREDICATES: dict[str, re.Pattern] = {
    "OUT_OF_SCOPE_1099": re.compile(r"\b1099(-[A-Z]+)?\b", re.I),
    "SCOPE_NO_ADVICE": re.compile(
        r"\b(should i|what.?s the best way to|how (do|can) i (avoid|reduce|minimi[sz]e)|"
        r"is it (legal|ok) to|help me (avoid|evade)|tax (advice|strategy|loophole)|evade|"
        r"write.?off|deduct my)\b",
        re.I,
    ),
    "PII_REJECT": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # a full SSN
}
_PII_SUB = _PREDICATES["PII_REJECT"]


class Guardrails:
    """Data-driven scope guardrails (S9). Reads the declared ids from ``policy.yaml``; enforces them
    with the code predicates above."""

    def __init__(self, domain: Domain) -> None:
        guard = (domain.policy.get("guardrails") or {})
        self.rules: list[dict] = list(guard.get("out_of_scope") or [])

    def scope_check(self, text: str) -> Verdict:
        """Return the first guardrail the text trips (naming its stable rule id), or an allow verdict.

        A rule with ``action: redact`` (PII) is a SOFT block: the caller strips the match and
        continues rather than refusing the whole turn."""
        for rule in self.rules:
            rule_id = str(rule.get("id") or "")
            pat = _PREDICATES.get(rule_id)
            if pat is None:
                continue
            if pat.search(text):
                action = str(rule.get("action") or "refuse")
                return Verdict(False, rule_id, str(rule.get("reason") or ""), action)
        return Verdict(True)

    @staticmethod
    def redact(text: str) -> str:
        """Redact SSNs from text (the PII soft-block)."""
        return _PII_SUB.sub("[REDACTED-SSN]", text)
