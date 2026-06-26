"""Generate the skill-unification eval case (Matt).

A FULL-PIPELINE case (component: null) that stress-tests ingestion on a large,
overlapping corpus of agent "skills":

  - Every skill file (``SKILL.md``) from two independent toolkits is ingested
    *raw and in full* through the ingestor (``seeded_insight.via_ingestor``):
      * Garry Tan's **gstack** (``sources/gstack/``)
      * Every's **compound-engineering** plugin (``sources/compound-engineering/``)
    The ingestor distils each one into the knowledge graph. We hand-write no
    pre-distilled facts (``direct_to_graph``); the point is to exercise ingestion
    (distillation + semantic dedup) on messy, partly-overlapping real source text.
  - The unification task is the ``seed_prompt``: the boxed Claude Code agent must
    read ONLY the injected knowledge graph and write a single *unified,
    de-duplicated* skill catalog to ``unified-skills.md``. Bash/WebFetch/WebSearch
    are off, so it cannot re-fetch the repos — it works from the ingested graph.
  - Deterministic checks assert the catalog is non-empty and covers the
    capabilities that recur across both toolkits (planning, code review, design
    review, debugging, commit/PR, shipping). A rubric grades coverage, dedup/merge
    quality, grounding, and structural coherence.

The two source repos were vendored once into ``sources/`` (see ``README.md``).
This script reads every file there and stamps it into ``case.yaml``. Re-run after
re-vendoring or editing the prompt/rubric:

    uv run python knowledge/evals/cases/matt/skill_unification/_generate.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).parent
SRC = HERE / "sources"
CASE_ID = "matt_skill_unification"


def load_sources() -> list[str]:
    """Every vendored skill file, each as one raw via_ingestor item.

    Prefixed with a provenance header (``# source: <repo>/<skill>``) so the
    distilled facts can carry which toolkit each skill came from — the unified
    catalog is graded partly on whether it preserves that provenance.
    """
    items: list[str] = []
    for repo in ("gstack", "compound-engineering"):
        for path in sorted((SRC / repo).glob("*.md")):
            body = path.read_text(encoding="utf-8")
            items.append(f"# source: {repo}/{path.stem}\n\n{body}")
    return items


SEED_PROMPT = (
    "You are given a large library of agent SKILLS drawn from two independent "
    "engineering toolkits: Garry Tan's `gstack` and Every's `compound-engineering` "
    "plugin. Each skill has a name, a description of when to use it, and a "
    "procedure. The two toolkits were built separately, so many of their skills "
    "OVERLAP in purpose (for example: planning, code review, design review, "
    "debugging, committing and opening PRs, shipping/releasing, and "
    "brainstorming/ideation each appear in some form in both).\n\n"
    "Using ONLY the skill knowledge you have been given (do not invent skills and "
    "do not look anything up), produce a single UNIFIED, DE-DUPLICATED catalog of "
    "skills that merges the two toolkits into one coherent set. Requirements:\n"
    "- Group skills by capability. Where a gstack skill and a compound-engineering "
    "skill do the same job, MERGE them into one unified skill rather than listing "
    "both, and note which source skills it came from.\n"
    "- Keep genuinely distinct skills separate; do not collapse unrelated skills.\n"
    "- For each unified skill give: a name, a one-line 'use when' description, a "
    "short summary of what it does, and its provenance (which source toolkit(s) and "
    "original skill name(s) it derives from).\n"
    "- Ground every entry in the ingested skills; do not fabricate capabilities "
    "that neither toolkit has.\n\n"
    "Write the unified catalog to a file named `unified-skills.md` and create no "
    "other files."
)


def regex_check(name: str, pattern: str) -> dict:
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
        "params": {"pattern": pattern},
    }


def mentions_any(name: str, patterns: list[str]) -> dict:
    """Pass iff ANY of the regexes matches — synonym/spelling tolerant."""
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:mentions_any",
        "params": {"patterns": patterns},
    }


def line_with_all(name: str, patterns: list[str]) -> dict:
    """Pass iff a single line matches every regex — proves dual-source co-citation."""
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:line_with_all",
        "params": {"patterns": patterns},
    }


NONEMPTY = {
    "name": "produced_catalog",
    "ref": "knowledge.evals.deterministic_checks.builds:output_nonempty",
    "params": {},
}

WROTE_FILE = {
    "name": "wrote_unified_skills",
    "ref": "knowledge.evals.deterministic_checks.builds:writes_file",
    "params": {"path": "unified-skills.md"},
}


# --- Strong, discriminating deterministic checks ---------------------------
#
# The original suite probed for bare English words ("plan", "review", "design",
# ...). Every plausible catalog contains those, so the checks could never fail —
# not even for the raw un-merged concatenation, nor for a catalog built after the
# pipeline silently dropped half the corpus (observed: large skill files 500 on
# ingest, so whole capability areas can vanish while these checks stay green).
#
# These replacements test the eval's actual thesis end-to-end:
#   1. BOTH toolkits reach the catalog (fails if one toolkit's facts never
#      ingested / retrieved — the corpus-truncation failure mode).
#   2. At least one entry is a real cross-toolkit MERGE (one line citing both
#      sources), not two stapled-together lists.
#   3. Each recurring capability is represented by a NAMED skill from BOTH
#      toolkits, so a hallucinated or half-empty catalog can't pass on generic
#      vocabulary alone.

# (1) Both toolkits must be present by name (provenance survived for each).
BOTH_TOOLKITS = [
    mentions_any("references_gstack", ["(?i)\\bgstack\\b"]),
    mentions_any(
        "references_compound_engineering",
        ["(?i)compound[\\s-]?engineering", "(?i)\\bce-[a-z]"],
    ),
]

# (2) A genuine merge: one provenance line that cites BOTH toolkits at once.
# NB: line_with_all applies re.IGNORECASE itself, so these patterns must NOT
# embed inline (?i) flags (a mid-expression (?i) is a regex error in Python).
CROSS_TOOLKIT_MERGE = line_with_all(
    "has_cross_toolkit_merge",
    ["gstack", "compound[\\s-]?engineering|\\bce-[a-z]"],
)

# (3) Each overlapping capability, anchored to NAMED source skills from each
# toolkit (gstack alternative | compound `ce-*` alternative). A capability only
# passes if a real skill from at least one toolkit is named — and the BOTH_*
# checks above force the other toolkit to appear somewhere too. This fails when
# distillation drops the central skills (e.g. ce-code-review, review/devex-review).
COVERAGE_CHECKS_NAMED = [
    mentions_any("covers_planning", ["(?i)\\bce-plan\\b", "(?i)\\bautoplan\\b", "(?i)plan-eng-review", "(?i)\\bspec\\b"]),
    mentions_any("covers_code_review", ["(?i)\\bce-code-review\\b", "(?i)devex-review", "(?i)\\breview\\b"]),
    mentions_any("covers_design_review", ["(?i)design-review", "(?i)ce-doc-review", "(?i)plan-design-review"]),
    mentions_any("covers_debugging", ["(?i)\\bce-debug\\b", "(?i)\\binvestigate\\b"]),
    mentions_any("covers_commit_pr", ["(?i)ce-commit(-push-pr)?", "(?i)ce-resolve-pr-feedback", "(?i)\\bship\\b"]),
    mentions_any("covers_ship_release", ["(?i)\\bship\\b", "(?i)land-and-deploy", "(?i)\\blfg\\b", "(?i)ce-work\\b"]),
]


def rubric() -> dict:
    return {
        "id": f"{CASE_ID}_v1",
        "items": [
            {
                "id": "coverage",
                "criterion": (
                    "The unified catalog accounts for the major capabilities present "
                    "across BOTH toolkits — at minimum planning, code review, design "
                    "review, debugging/investigation, committing/opening PRs, and "
                    "shipping/releasing — without dropping whole capability areas."
                ),
                "weight": 2.0,
            },
            {
                "id": "dedup_merge",
                "criterion": (
                    "Skills that do the same job across the two toolkits are MERGED "
                    "into a single unified skill (e.g. gstack `review`/`devex-review` "
                    "with compound-engineering `ce-code-review`; gstack `ship` with "
                    "`ce-commit`/`ce-commit-push-pr`) rather than listed twice, while "
                    "genuinely distinct skills are kept separate. The result is "
                    "noticeably more unified than the raw concatenation of both sets."
                ),
                "weight": 2.0,
            },
            {
                "id": "provenance",
                "criterion": (
                    "Each unified skill records its provenance — which source "
                    "toolkit(s) (gstack and/or compound-engineering) and which "
                    "original skill name(s) it derives from."
                ),
                "weight": 1.0,
            },
            {
                "id": "grounded",
                "criterion": (
                    "Every entry traces to skills that were actually ingested from "
                    "the two toolkits. No fabricated skills or capabilities that "
                    "neither toolkit provides."
                ),
                "weight": 1.5,
            },
            {
                "id": "coherent_structure",
                "criterion": (
                    "Entries follow one consistent schema (name, use-when, what-it-"
                    "does, provenance) and the catalog reads as a single coherent set "
                    "rather than two stapled-together lists."
                ),
                "weight": 1.0,
            },
        ],
    }


def main() -> None:
    sources = load_sources()
    case = {
        "id": CASE_ID,
        # vector substrate -> real write policy (redact/dedup) via VectorGraph,
        # so semantically-overlapping skills from the two toolkits can be deduped
        # at ingest time, not just by the agent.
        "substrate": "vector",
        # cached embedder -> committed real vectors replayed offline (semantic
        # dedup). Skips offline only until the embedding + ingestion cassettes
        # are recorded.
        "embedder": "cached",
        # ingest_model -> PromptIngestor runs a real LLM to distil each long
        # SKILL.md into facts instead of the passthrough line-split.
        "ingest_model": "openai/gpt-4o-mini",
        # active -> the ingested skill library is established knowledge, so it
        # lands retrievable (the default "proposed" would gate it out of the
        # reader and leave the agent with an empty knowledge prompt).
        "ingest_state": "active",
        # retrieving reader with the volume cap OFF (top_k: 0): rank all active
        # skill facts against the unification prompt, keep whatever the floor +
        # relative-to-best admit — no fixed top-N starvation across ~85 skills.
        "reader": "retrieving",
        "reader_top_k": 0,
        "seed_prompt": SEED_PROMPT,
        "target_commit": "0" * 40,
        # file_io (not sandbox): checks grade the produced catalog text and mount
        # no fixture, so a file-producing runner suffices.
        "needs": ["file_io"],
        "output_file": "unified-skills.md",
        "seeded_insight": {"via_ingestor": sources},
        "deterministic_checks": (
            [NONEMPTY, WROTE_FILE]
            + BOTH_TOOLKITS
            + [CROSS_TOOLKIT_MERGE]
            + COVERAGE_CHECKS_NAMED
        ),
        "rubric": rubric(),
    }

    header = (
        "# GENERATED by _generate.py — edit that script, not this file.\n"
        "# Full-pipeline case: every gstack + compound-engineering SKILL.md is\n"
        "# ingested raw via the ingestor; the agent writes a unified, de-duplicated\n"
        "# skill catalog to unified-skills.md, graded on coverage/dedup/provenance.\n"
    )
    out = HERE / "case.yaml"
    out.write_text(
        header + yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=4096),
        encoding="utf-8",
    )
    print(f"wrote {out.relative_to(HERE.parents[3])} with {len(sources)} ingested skill file(s)")


if __name__ == "__main__":
    main()
