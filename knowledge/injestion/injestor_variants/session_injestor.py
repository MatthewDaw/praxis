"""Distill one solved-problem session into Insight[] with a single structured call.

The session analogue of :class:`CommitIngestor`: a thin variant over
:class:`StructuredDistillIngestor` that sets only the session-framed distillation
prompt, the input label, and the schema name. The shared base owns the structured
``complete`` call, JSON parse, and precision-first drop-malformed handling, so the two
triggers (a merged PR vs. a live solved-problem session) cannot drift apart.

``synthesis`` takes a rendered session narrative and the unit's ``source``
(``session/<id>``), which the inherited ``ingest`` threads onto every written fact.
Insights land ``state="proposed"`` by inheritance — the candidate lifecycle, not the
active store.
"""

from __future__ import annotations

from knowledge.injestion.injestor_variants.structured_distill import (
    StructuredDistillIngestor,
)

# Session-framed twin of CommitIngestor's prompt. Differences from the PR framing:
# the input is a solve narrative (problem / what failed / fix / why / prevention), and
# two extra guidance lines resolved in the proposal's deferred questions — scope is
# where the lesson APPLIES not where it was found, and durable knowledge is preferred
# over in-flight experiment state.
_DISTILL_PROMPT = (
    "You are distilling durable engineering knowledge from one solved-problem coding "
    "session for an agent that will work in this repository later.\n"
    "The session narrative names a problem, what was tried and failed, the fix, why it "
    "works, and how to prevent recurrence. Extract ONLY knowledge that stays true after "
    "the fix ships: the root-cause lesson, the gotcha / non-obvious constraint that "
    "caused it, the decision and its rationale, the convention it established, and "
    "approaches that were tried and explicitly rejected.\n"
    "IGNORE the play-by-play: which file was opened first, transient error text already "
    "resolved, tool-by-tool narration, and anything restating WHAT was done without WHY "
    "it matters going forward.\n"
    "Prefer durable code or architecture knowledge over an in-flight experiment's "
    "current state (e.g. which eval case was added or dropped today): the former stays "
    "true, the latter goes stale.\n"
    "For each insight return:\n"
    "- text: one self-contained sentence or two, naming its own subject (no pronouns, "
    "no \"this session\"), stating the durable fact and — for a decision or gotcha — "
    "why it holds. When the fact is a constraint on HOW code must be written — something "
    "an agent could violate without any error surfacing — fold in the concrete "
    "enforcement form (the guard, signature, or call) as a brief illustrative example, "
    "framed as an example so it stays useful even if the surrounding code later changes.\n"
    "- scope: where the fact applies, as `file:<path>`, `module:<name>`, or `repo`. The "
    "files a session names are where the lesson was FOUND, not necessarily where it "
    "APPLIES — choose scope by where the fact is true, not by which files were touched.\n"
    "- category: one of decision | gotcha | convention | rejected.\n"
    "Prefer precision over recall: emit nothing rather than a vague or speculative fact. "
    "Return an empty list when the session carries no durable knowledge."
)


class SessionIngestor(StructuredDistillIngestor):
    """Distill a solved-problem session narrative into typed insights in one call."""

    _DISTILL_PROMPT = _DISTILL_PROMPT
    _INPUT_LABEL = "SESSION NARRATIVE"
    _SCHEMA_NAME = "session_distillation"
