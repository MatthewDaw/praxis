"""Generate intent-gating (distractor) eval cases for Matt.

These are the ACCEPTANCE TEST for intent-aware ingestion + gated retrieval
(see knowledge/injestion/INTENT_ENCODING.md). They are deliberately different
from the application-filling cases: those grade the *final answer*; these grade
the *retrieval set* — did the right facts surface and the wrong ones stay hidden?

Each case is a ``component: graph_reader`` case (no agent, no sandbox):

  - A small MIXED corpus is ingested raw via ``seeded_insight.via_ingestor``:
    autobiographical experience facts (gauntlet / employers / Praxis) PLUS
    general-knowledge "distractor" facts on the same TOPICS (two-tower models,
    RAG, embeddings). Same topic, different situation-of-use — that overlap is
    the whole point.
  - ``seed_prompt`` is the *situation/query* handed to ``reader.read``.
  - Deterministic checks assert presence/absence of autobiographical tokens in
    the retrieved context.

Two situations, opposite expectations:

  * PERSONAL situation (application / "tell me your experience") -> experience
    SHOULD surface (recall floor).
  * GENERAL-KNOWLEDGE situation ("explain how X works") -> experience should
    NOT surface, even though the topic matches (precision / no-leak). The
    general fact on that topic should still be available.

Every case is marked ``xfail``: today's reader has no situation gating, so the
no-leak cases fail (experience leaks into general-knowledge queries). When the
tagger + two-tier gated reader land, these flip to XPASS — the harness's signal
that the capability arrived. Promote them to real assertions then.

Re-generate:  uv run python knowledge/evals/cases/matt/intent_gating/_generate.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).parent

XFAIL_REASON = "intent-gated retrieval not yet built (no situation tagging at ingest, no gating at read)"

# A compact corpus deliberately built so TOPIC and SITUATION pull apart: each
# autobiographical fact shares its topic with a general-knowledge fact, so pure
# similarity cannot tell them apart — only situation can.
CORPUS = [
    # --- autobiographical / lived_action (should be situation-gated) ---
    "Matthew Daw built Praxis, a knowledge-graph retrieval system, during the Gauntlet AI fellowship.",
    "Matthew is a Cohort V AI Challenger in the Gauntlet AI fellowship in 2026.",
    "At BENlabs, Matthew built a production RAG creative assistant that served 65% of user activity.",
    "Matthew built a text-to-SQL LLM agent at a stealth AI startup with 90% accuracy.",
    # --- general knowledge / world_fact (should always be eligible) ---
    "Two-tower models encode queries and candidates into a shared embedding space for candidate generation.",
    "RAG augments a language model by retrieving relevant documents into the prompt at inference time.",
    "Cosine similarity measures the angle between two vectors and is a common metric for embedding search.",
]

# Autobiographical tokens that must NOT appear when the situation is general
# knowledge. Kept distinct from generic topic words (no "RAG"/"embedding" here —
# those legitimately appear via the general-knowledge facts).
EXPERIENCE_TOKENS = ["Praxis", "Gauntlet", "BENlabs", "Matthew"]


def case(slug: str, situation: str, query: str, checks: list[dict], *, xfail: bool) -> dict:
    data = {
        "id": f"matt_intent_{slug}",
        "component": "graph_reader",
        "substrate": "in_memory",
        "seed_prompt": query,  # the situation/query the reader gates on
        "seeded_insight": {"via_ingestor": CORPUS},
        "deterministic_checks": checks,
    }
    # Only the no-leak (precision) cases are xfail: they assert a capability that
    # does not exist yet, so they are expected red until gating lands. The recall
    # cases already pass today and stay green as guardrails — marking them xfail
    # would just report perpetual XPASS noise.
    if xfail:
        data["xfail"] = XFAIL_REASON
    return data


def forbids_each(tokens: list[str]) -> list[dict]:
    """A no-leak check per autobiographical token (case-insensitive)."""
    return [
        {
            "name": f"no_leak_{t.lower()}",
            "ref": "knowledge.evals.deterministic_checks.text:forbids_substring",
            "params": {"text": t, "case_insensitive": True},
        }
        for t in tokens
    ]


def requires(pattern: str, name: str) -> dict:
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
        "params": {"pattern": pattern},
    }


# slug -> (situation, query, checks)
CASES = [
    # RECALL: a clearly personal situation must surface experience.
    (
        "application_surfaces_experience",
        "job_application",
        "You are helping Matthew fill out a job application. Describe a production "
        "AI system you personally built, with concrete projects.",
        [requires(r"(?i)praxis|gauntlet|benlabs", "surfaces_experience")],
    ),
    (
        "narrative_surfaces_experience",
        "personal_narrative",
        "Tell me about your background and the things you've built.",
        [requires(r"(?i)praxis|gauntlet|benlabs", "surfaces_experience")],
    ),
    # PRECISION / NO-LEAK: a general-knowledge situation on the SAME topic must
    # not pull autobiographical facts — but must still surface the general fact.
    (
        "general_two_tower_hides_experience",
        "general_knowledge",
        "Explain how two-tower models work for candidate generation in general.",
        forbids_each(EXPERIENCE_TOKENS)
        + [requires(r"(?i)two-tower|embedding space", "keeps_general_fact")],
    ),
    (
        "general_rag_hides_experience",
        "general_knowledge",
        "What is retrieval-augmented generation and how does it work, in general terms?",
        forbids_each(EXPERIENCE_TOKENS)
        + [requires(r"(?i)retriev", "keeps_general_fact")],
    ),
    (
        "general_embeddings_hides_experience",
        "general_knowledge",
        "How does cosine similarity work for embedding search, conceptually?",
        forbids_each(EXPERIENCE_TOKENS)
        + [requires(r"(?i)cosine", "keeps_general_fact")],
    ),
]


def main() -> None:
    written = 0
    for slug, situation, query, checks in CASES:
        # No-leak cases assert an unbuilt capability -> xfail. Recall cases pass
        # today -> plain guardrails.
        data = case(slug, situation, query, checks, xfail=situation == "general_knowledge")
        out = HERE / slug / "case.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# GENERATED by _generate.py — edit that script, not this file.\n"
            "# Intent-gating acceptance test (graph_reader component): a mixed\n"
            "# experience+general corpus is ingested, then queried under a given\n"
            "# situation; checks assert experience surfaces (recall) or stays\n"
            "# hidden (no-leak). xfail until intent tagging + gated read land.\n"
        )
        out.write_text(
            header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=4096),
            encoding="utf-8",
        )
        written += 1
    print(f"wrote {written} case(s)")


if __name__ == "__main__":
    main()
