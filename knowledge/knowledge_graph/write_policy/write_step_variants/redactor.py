"""Redact secrets and PII before a candidate is stored.

Regex baseline (no heavy deps): emails, and high-signal secret patterns
(provider-key prefixes + long high-entropy tokens). Microsoft Presidio is the
natural richer variant behind this same ``WriteStep`` seam later.
"""

from __future__ import annotations

import re

from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision

_PLACEHOLDER = "[REDACTED]"

# Order matters: most specific first.
_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # email
    re.compile(r"\b(?:sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9-_]{8,}\b"),  # provider keys
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),  # long high-entropy token
]


def redact_text(text: str) -> str:
    """Replace secret/PII-shaped substrings with ``[REDACTED]``.

    Module-level so callers that need to scrub a raw string *before* it leaves the
    process (e.g. before sending a session narrative to a third-party LLM) can reuse
    the exact patterns the write-time :class:`Redactor` applies, rather than
    duplicating them.
    """
    for pattern in _PATTERNS:
        text = pattern.sub(_PLACEHOLDER, text)
    return text


class Redactor(WriteStep):
    def apply(self, decision: WriteDecision) -> None:
        decision.text = redact_text(decision.text)
