---
description: Stand up ONE ml_registry campaign (data, harness, four baselines, bootstrap) and seed its starting idea set, then hand off to af-ml-supervise. Pass "skip research" to reuse an existing nine-axis sweep and only do setup. Use when the human says "seed the campaign", "/af-seed-ml-supervise", "run seed-campaign", "get this ready for af-ml-supervise", or "skip research".
argument-hint: [project or model-id] [skip research] [optional --mode interactive|batch]
---

The user invoked `/af-seed-ml-supervise`. Read `agent_factory/skills/af-seed-ml-supervise/SKILL.md` and follow it. Setup is this skill's job — do not stop at "go bootstrap yourself". If `$ARGUMENTS` contains skip research / `--skip-research` / "use the existing ideas", do **not** re-run the nine-axis fleet. Reuse the existing generator/retriever scripts and stand the campaign up.

$ARGUMENTS
