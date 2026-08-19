"""The af-seed-ml-supervise skill document names the nine-axis closed set,
seed-campaign, origin=seeded, and the closed-set enforcement.

Cheap structure check: a rewrite that drops an axis, forgets the CLI verb, or
stops saying the set is closed cannot land as prose that still claims to be
the written af-ml-ideate.
"""

from __future__ import annotations

from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "agent_factory"
    / "skills"
    / "af-seed-ml-supervise"
    / "SKILL.md"
)

# Exact strings from knowledge.ml_registry.ideate — do not paraphrase.
GENERATIVE_AXES = (
    "theoretical_math",
    "ablation",
    "supplements",
    "ml_architectures",
    "non_ml_methods",
    "ce_ideate_breadth",
)
RETRIEVAL_AXES = ("current_code", "prior_trials", "af_learn_lessons")


def test_skill_names_the_nine_closed_axes() -> None:
    text = SKILL_PATH.read_text()
    for axis in GENERATIVE_AXES + RETRIEVAL_AXES:
        assert axis in text, f"SKILL.md must name the closed-set axis {axis!r}"


def test_skill_names_seed_campaign_and_seeded_origin() -> None:
    text = SKILL_PATH.read_text()
    assert "seed-campaign" in text, "SKILL.md must name the CLI verb seed-campaign"
    assert 'origin="seeded"' in text or "origin=seeded" in text, (
        "SKILL.md must document that written ideas carry origin=seeded"
    )


def test_skill_names_the_closed_set() -> None:
    text = SKILL_PATH.read_text()
    lowered = text.casefold()
    assert "closed set" in lowered or "closed-set" in lowered, (
        "SKILL.md must name the nine-axis closed set as closed, not merely list axes"
    )
    assert "require_closed_axis" in text or "closed is enforced" in lowered, (
        "SKILL.md must not paraphrase the closed-set enforcement away"
    )
