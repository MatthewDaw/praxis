---
name: requesting-code-review
author: obra/superpowers
source: https://skills.sh/obra/superpowers/requesting-code-review
---

# requesting-code-review

Dispatch a code-reviewer subagent to catch issues before they cascade, giving it
focused context for evaluation.

## When to use
- Mandatory: after each task in subagent workflows, after major features, before merging to main.
- Optional: when stuck, before refactoring, or after fixing a complex bug.

## Workflow
Dispatch a specialized review agent with precise implementation details,
requirements, and the commit range — but exclude session history so the reviewer
focuses on the work product, not your thought process. Categorize feedback into
three tiers: Critical (fix now), Important (resolve before proceeding), Minor
(note for later). Uses template-based dispatch with customizable context
placeholders.

Core principle: early and frequent review prevents issues from compounding.
