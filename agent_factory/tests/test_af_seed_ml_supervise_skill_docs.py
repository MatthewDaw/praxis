"""The af-seed-ml-supervise skill document names the nine-axis closed set,
seed-campaign, origin=seeded, the closed-set enforcement, AND the setup
contract that makes bootstrap-campaign legal.

Cheap structure check: a rewrite that drops an axis, forgets the CLI verb,
stops saying the set is closed, or goes back to 'tell the human to bootstrap
and stop' cannot land as prose that still claims to stand a campaign up.
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


def test_skill_owns_setup_through_bootstrap_not_a_handoff_out() -> None:
    """A rewrite that tells the human to bootstrap-campaign and stop is the
    previous contract. This skill now stands the campaign up itself."""
    text = SKILL_PATH.read_text()
    lowered = text.casefold()
    assert "bootstrap-campaign" in text, (
        "SKILL.md must name bootstrap-campaign as the setup gate"
    )
    assert "version-2" in lowered or "version 2" in lowered, (
        "SKILL.md must name the version-2 ledger the dispatch command writes"
    )
    assert "REQUIRED_BASELINE_RUN_COUNT" in text or "≥4" in text or ">=4" in text, (
        "SKILL.md must require the four incumbent baseline rows"
    )
    assert "dispatch" in lowered, (
        "SKILL.md must require a project-owned dispatch command"
    )
    assert "{sha}:{arm_tag}" in text, (
        "SKILL.md must name the unique join key, not a bare SHA"
    )
    assert "do not invent a model" not in lowered or "finish phase e first" in lowered, (
        "SKILL.md must not bounce an unregistered model back to the human as the "
        "terminal action — setup is this skill's job"
    )
    # The old 'tell the human to run bootstrap-campaign first / stop' exit.
    assert "tell the human to run `bootstrap-campaign` first" not in lowered, (
        "SKILL.md must not stop at 'go bootstrap yourself' — it runs setup"
    )
