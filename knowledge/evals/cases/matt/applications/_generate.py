"""Generate application-filling eval cases for Matt.

Each case is a FULL-PIPELINE case (component: null):

  - The raw source documents (resume, LinkedIn, BYU ACME degree, and the Gauntlet
    AI program page) are ingested *raw* through the ingestor
    (``seeded_insight.via_ingestor``) — NOT hand-curated facts. The ingestor
    distils them into the knowledge graph.
  - The application question is the ``seed_prompt`` that drives the boxed Claude
    Code agent to write the answer (to ``answer.md``).
  - Deterministic checks (case-insensitive ``regex_matches`` + ``output_nonempty``)
    assert the filled answer drew the right facts from Matt's ingested profile;
    a rubric grades grounding / relevance / specificity / honesty.

The same three raw sources seed every case, so they live once in ``sources/`` and
this script stamps them into each ``case.yaml``. Add a question to ``COMPANIES``
and re-run:  ``uv run python knowledge/evals/cases/matt/applications/_generate.py``
"""

from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).parent
SRC = HERE / "sources"

SOURCES = [
    (SRC / "resume.txt").read_text(encoding="utf-8"),
    (SRC / "linkedin.txt").read_text(encoding="utf-8"),
    (SRC / "degree.txt").read_text(encoding="utf-8"),
    (SRC / "gauntlet.txt").read_text(encoding="utf-8"),  # Gauntlet AI program context
]


def seed_prompt(company: str, role: str, question: str) -> str:
    return (
        f"You are helping Matthew Daw fill out a job application for the {role} "
        f"role at {company}. Using ONLY the background knowledge you have been given "
        "about Matthew (from his resume, LinkedIn, university degree, and the Gauntlet "
        "AI fellowship he is currently in), write a "
        "concise, truthful, first-person answer to the application question below. "
        "Be specific: cite concrete projects, technologies, and metrics from his "
        "actual background, and do not invent experience he does not have. If his "
        "background only partially fits the question, say so honestly and map the "
        "closest relevant experience. Write the answer to a file named answer.md and "
        "create no other files.\n\n"
        f"Application question: {question}"
    )


# Shared behavioral rubric. `focus` is spliced into the relevance criterion.
def rubric(case_id: str, focus: str) -> dict:
    return {
        "id": f"{case_id}_v1",
        "items": [
            {
                "id": "grounded",
                "criterion": (
                    "Every claim is grounded in Matthew's real background (resume, "
                    "LinkedIn, degree). No fabricated employers, projects, or metrics."
                ),
                "weight": 2.0,
            },
            {
                "id": "relevant",
                "criterion": f"The answer directly addresses the question: {focus}.",
                "weight": 1.5,
            },
            {
                "id": "specific",
                "criterion": (
                    "Cites concrete projects, technologies, and quantified outcomes "
                    "rather than generic claims."
                ),
                "weight": 1.0,
            },
            {
                "id": "honest",
                "criterion": (
                    "Where Matthew's experience only partially matches, it is framed "
                    "honestly (closest-fit / transferable) instead of overclaiming."
                ),
                "weight": 1.5,
            },
        ],
    }


def regex_check(name: str, pattern: str) -> dict:
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
        "params": {"pattern": pattern},
    }


NONEMPTY = {
    "name": "produced_answer",
    "ref": "knowledge.evals.deterministic_checks.builds:output_nonempty",
    "params": {},
}


# company -> (display name, role, folder, [questions])
# each question: (slug, focus, question_text, [(check_name, regex), ...])
COMPANIES = [
    (
        "Hightouch",
        "Software Engineer, AI Agents",
        "hightouch",
        [
            (
                "complex_ai_products_0_to_1",
                "complex AI products built 0 -> 1 (LLMs, search, agents)",
                "What complex AI products/applications have you worked on 0 -> 1? "
                "Could be related to LLMs, multimodal search, agent development, etc.",
                [("mentions_praxis", "(?i)praxis"), ("mentions_rag_or_sql", "(?i)rag|sql")],
            ),
            (
                "production_llm_pipeline",
                "a production LLM pipeline he built (architecture, model, latency, cost)",
                "Describe a production LLM pipeline you've built or contributed to. What "
                "was the architecture, and what tradeoffs did you make around model "
                "choice, latency, and cost?",
                [("mentions_sql", "(?i)sql"), ("mentions_rag", "(?i)rag")],
            ),
            (
                "agentic_systems",
                "agentic systems with tool use / planning / multi-step reasoning",
                "Have you built agentic systems (tool use, planning, multi-step "
                "reasoning)? Walk us through one.",
                [("mentions_agent", "(?i)agent"), ("mentions_sql", "(?i)sql")],
            ),
            (
                "backend_architecture_scaling",
                "a backend system he designed and its scaling/reliability challenges",
                "Describe a backend system you designed and the scaling or reliability "
                "challenges you solved.",
                [("mentions_scale", "(?i)billions"), ("mentions_stack", "(?i)graphql|lambda")],
            ),
            (
                "data_warehouse_experience",
                "building data-intensive systems on a customer's data warehouse",
                "What is your experience building data-intensive systems that connect to "
                "a customer's data warehouse?",
                [("mentions_databricks", "(?i)databricks"), ("mentions_dbt", "(?i)dbt")],
            ),
            (
                "education_background",
                "his education and how it prepared him for AI/backend work",
                "Walk us through your relevant education and how it prepared you for "
                "AI/backend work.",
                [("mentions_math", "(?i)mathematics|acme"), ("mentions_markov", "(?i)markov")],
            ),
            (
                "location_and_visa",
                "location/availability and visa sponsorship",
                "This role asks candidates to occasionally visit the SF Bay Area office. "
                "What is your location and availability, and do you require visa "
                "sponsorship now or in the future?",
                [("mentions_location", "(?i)utah")],
            ),
            (
                "typescript_experience",
                "experience with TypeScript (their stack is TypeScript)",
                "Our tech stack is TypeScript. What is your experience with TypeScript "
                "and the JavaScript ecosystem?",
                [("mentions_react", "(?i)react")],
            ),
        ],
    ),
    (
        "Sekai",
        "Senior Machine Learning Engineer (Search & Recommendations)",
        "sekai",
        [
            (
                "owned_recsys_or_search_system",
                "a production recommendation/search/ranking/discovery system he owned",
                "Please describe one production recommendation, search, ranking, or "
                "discovery system you personally owned for a consumer/prosumer product.",
                [("mentions_retrieval", "(?i)rag|retriev|recommend|rank")],
            ),
            (
                "embedding_retrieval_two_tower",
                "embedding-based retrieval and two-tower models for candidate generation",
                "What is your experience with embedding-based retrieval and two-tower "
                "models for candidate generation, and how have you used embeddings in "
                "production?",
                [("mentions_embeddings", "(?i)embedding")],
            ),
            (
                "production_ml_pipelines",
                "a production ML pipeline end-to-end (training, serving, monitoring)",
                "Walk us through a production ML pipeline you built end-to-end — "
                "training, serving, monitoring, and evaluation.",
                [("mentions_serving", "(?i)bentoml|mlops")],
            ),
            (
                "ml_experiment_end_to_end",
                "an ML/ranking experiment he designed and analyzed end-to-end",
                "Tell us about a recommendation, ranking, or model-quality experiment "
                "you designed and analyzed end-to-end, and how the results drove your "
                "next iteration.",
                [("mentions_metric", "(?i)accuracy|%")],
            ),
            (
                "cold_start_new_users_content",
                "improving recommendation quality for new users and new content (cold start)",
                "How would you approach improving recommendation quality for new users "
                "and brand-new content (the cold-start problem) in a fast-changing "
                "content pool?",
                [],  # design question — graded by rubric + nonempty
            ),
            (
                "why_sekai_remote_fit",
                "why he wants AI-native content discovery at Sekai and his fit (remote-first)",
                "Sekai is a remote-first, fast-moving consumer social product (the "
                "'TikTok of interactive mini-apps'). Why do you want to work on "
                "AI-native content discovery here, and what makes you a fit?",
                [],  # motivation question — graded by rubric + nonempty
            ),
        ],
    ),
]


def main() -> None:
    written = 0
    for company, role, folder, questions in COMPANIES:
        for slug, focus, question, checks in questions:
            case_id = f"matt_{folder}_{slug}"
            case = {
                "id": case_id,
                # vector substrate -> _build_trio_for builds a VectorGraph (real
                # write policy: redact/dedup) instead of the InMemoryGraph stub.
                "substrate": "vector",
                # cached embedder -> committed real vectors (semantic dedup) that
                # replay offline. Usable now that the ingestion cassette stabilizes
                # the distilled text the vectors key on (FR-008). Skips offline only
                # when neither the embedding cache nor the ingestion cassette exists.
                "embedder": "cached",
                # ingest_model -> PromptIngestor.synthesis runs a real LLM (distills
                # the sources into facts) instead of the passthrough line-split.
                "ingest_model": "openai/gpt-4o-mini",
                # ingest_state: active -> the applicant's distilled background is
                # established/endorsed knowledge, so it lands "active" and is
                # retrievable (the default "proposed" would be gated out of the
                # reader, leaving the agent with an empty knowledge prompt).
                "ingest_state": "active",
                # retrieving reader (001 relevance cutoff) with the volume cap OFF
                # (reader_top_k: 0): rank all active background facts against the
                # question, keep whatever the existence floor + relative-to-best admit
                # — no fixed top-N starvation. abs_floor/rel_ratio stay at the tuned
                # defaults (0.30 / 0.60).
                "reader": "retrieving",
                "reader_top_k": 0,
                "seed_prompt": seed_prompt(company, role, question),
                "target_commit": "0" * 40,
                # file_io, not sandbox: the checks grade answer text (output_nonempty
                # + regex_matches over ctx.output) and mount no fixture, so a file-
                # producing runner suffices. This lets the cheaper structured backend
                # grade them, not just full Claude Code.
                "needs": ["file_io"],
                "seeded_insight": {"via_ingestor": SOURCES},
                "deterministic_checks": [NONEMPTY]
                + [regex_check(n, p) for n, p in checks],
                "rubric": rubric(case_id, focus),
            }
            out = HERE / folder / slug / "case.yaml"
            out.parent.mkdir(parents=True, exist_ok=True)
            header = (
                "# GENERATED by _generate.py — edit that script, not this file.\n"
                "# Full-pipeline case: raw resume/LinkedIn/degree are ingested raw via\n"
                "# the ingestor; the application question drives the agent to write answer.md.\n"
            )
            out.write_text(
                header + yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=4096),
                encoding="utf-8",
            )
            written += 1
    print(f"wrote {written} case(s)")


if __name__ == "__main__":
    main()
