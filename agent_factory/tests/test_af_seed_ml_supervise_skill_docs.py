"""Regression guards for the target-ready af-seed-ml-supervise contract."""

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


def test_skill_owns_setup_through_target_host_proof() -> None:
    text = SKILL_PATH.read_text()
    lowered = text.casefold()
    for required in (
        "execution target",
        "durable revision",
        "real adapter data",
        "hardware and disk",
        "canonical baseline",
        "target measurement",
        "campaign registration",
        "one-arm smoke",
        "portfolio proof and handoff",
    ):
        assert required in lowered, f"SKILL.md must require {required!r}"
    assert "git rev-parse HEAD" in text
    assert "register_campaign_for_run" in text
    assert "knowledge.ml_registry.runtime.campaign_job" in text
    assert "Laptop success is not READY" in text


def test_skill_uses_only_the_canonical_registry_lifecycle() -> None:
    text = SKILL_PATH.read_text()
    for command in (
        "create-experiment",
        "create-run",
        "complete-run",
        "create-artifact",
        "register-model",
        "adjudicate-run",
        "finalize",
    ):
        assert command in text, f"SKILL.md must name canonical command {command!r}"
    for retired in (
        "bootstrap-campaign",
        "register-model-with-baseline",
        "results.tsv",
        "version-2 ledger",
        "{sha}:{arm_tag}",
    ):
        assert retired not in text, f"SKILL.md must not revive retired contract {retired!r}"


def test_skill_handoff_uses_current_portfolio_entrypoints() -> None:
    text = SKILL_PATH.read_text()
    assert "agent_factory/scripts/af-ml-portfolio-launch.sh --config <operator.json> run" in text
    portfolio = "python -m knowledge.ml_registry.cli.portfolio --config <operator.json>"
    for action in ("status", "stop --drain", "stop --force", "resume"):
        assert f"{portfolio} {action}" in text


def test_skill_has_a_skip_research_rerun_that_does_not_resweep() -> None:
    """The owner's rerun is: skip research, stand the campaign up, hand off.
    A rewrite that always dispatches the nine-axis fleet cannot satisfy that."""
    text = SKILL_PATH.read_text()
    lowered = text.casefold()
    assert "skip research" in lowered or "--skip-research" in lowered, (
        "SKILL.md must name a skip-research rerun"
    )
    assert "do not re-run research" in lowered or "re-dispatch the nine-axis" in lowered, (
        "SKILL.md must skip the generative/retrieval fleet when skip-research is set"
    )
    assert "missing scripts are a hard stop" in lowered or "hard stop" in lowered, (
        "SKILL.md must refuse skip-research when the existing scripts are missing, "
        "not silently start researching"
    )
