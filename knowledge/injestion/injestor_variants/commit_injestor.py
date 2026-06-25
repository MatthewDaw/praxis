"""Distill one merged PR (or commit) into Insight[] with a single structured call.

A thin variant over :class:`StructuredDistillIngestor` (``structured_distill.py``): it
sets only the PR/commit-framed distillation prompt, the input label, and the schema
name. The shared base owns the ``Llm.complete(..., response_format=...)`` call shape,
the JSON parse, and the precision-first drop-malformed handling — kept there so this
variant and ``SessionIngestor`` cannot drift. It deliberately does NOT adopt
``ingest_dump``'s slot-dedup / conflict reconciliation (out of scope per R4).

``synthesis`` takes the rendered ``PRDocument`` text and the unit's ``source``
(``git/pr:<n>`` | ``git/commit:<sha>``), which it stamps onto every insight.
"""

from __future__ import annotations

from knowledge.injestion.injestor_variants.structured_distill import (
    StructuredDistillIngestor,
)

# R2: durable knowledge only; ignore churn. Scope from a small vocabulary, category
# from a closed set. Return no insights when nothing durable is present.
_DISTILL_PROMPT = (
    "You are distilling durable engineering knowledge from one merged pull request "
    "(or commit) for a coding agent that will work in this repository later.\n"
    "Extract ONLY knowledge that stays true after this change ships: design "
    "decisions and their rationale, gotchas / non-obvious constraints, conventions, "
    "and explicitly rejected approaches.\n"
    "IGNORE churn with no durable lesson: dependency/version bumps, pure renames, "
    "formatting, mechanical refactors, and anything restating WHAT changed without "
    "WHY it matters going forward.\n"
    "For each insight return:\n"
    "- text: one self-contained sentence or two, naming its own subject (no "
    "pronouns, no \"this PR\"), stating the durable fact and — for a decision or "
    "gotcha — why it holds. When the fact is a constraint on HOW code must be "
    "written — something an agent could violate without any error surfacing — fold "
    "in the concrete enforcement form (the guard, signature, or call) as a brief "
    "illustrative example, e.g. \"guard the delete with `AND state IN "
    "('proposed','rejected')`\", not just the rule in prose; frame it as an example "
    "so it stays directionally useful even if the surrounding code later changes.\n"
    "- scope: where the fact applies, as `file:<path>`, `module:<name>`, or `repo`.\n"
    "- category: one of decision | gotcha | convention | rejected.\n"
    "Prefer precision over recall: emit nothing rather than a vague or speculative "
    "fact. Return an empty list when the unit carries no durable knowledge."
)


class CommitIngestor(StructuredDistillIngestor):
    """Distill a PR/commit document into typed insights with one structured call."""

    _DISTILL_PROMPT = _DISTILL_PROMPT
    _INPUT_LABEL = "UNIT INPUT"
    _SCHEMA_NAME = "pr_distillation"
