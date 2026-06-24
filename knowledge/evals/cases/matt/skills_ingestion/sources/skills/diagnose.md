---
name: diagnose
author: mattpocock/skills
source: https://skills.sh/mattpocock/skills/diagnose
---

# diagnose

A structured debugging workflow for reproducing, minimizing, and fixing difficult
bugs and performance regressions.

## When to use
- Hard-to-reproduce bugs, performance regressions, non-deterministic failures.

## Workflow
Core principle: build a fast feedback loop before hypothesis testing.
1. Build a deterministic pass/fail signal (failing test, curl script, CLI fixture, browser automation).
2. Confirm reproduction, rank falsifiable hypotheses, add targeted instrumentation tied to predictions.
3. Specialized techniques: stress-test non-deterministic bugs; baseline + bisection for performance regressions.
4. Quality gates: regression test at the right layer, clean up debug instrumentation, post-mortem.

Once you have a reliable signal, "everything else is mechanical."
