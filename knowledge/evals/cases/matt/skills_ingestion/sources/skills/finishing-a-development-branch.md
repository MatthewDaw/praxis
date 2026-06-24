---
name: finishing-a-development-branch
author: obra/superpowers
source: https://skills.sh/obra/superpowers/finishing-a-development-branch
---

# finishing-a-development-branch

Structured workflow for completing development branches with test verification and
merge/PR options.

## When to use
When you've finished coding on a feature branch and need to integrate the work —
merge locally, open a PR, preserve the branch, or discard it.

## Workflow
1. Verify tests pass before proceeding (don't merge/submit broken code).
2. Detect the environment and base branch automatically.
3. Present exactly four options: merge locally, create a pull request, keep the branch, or discard with confirmation.
4. Execute the choice and clean up temporary resources.

Integrates with subagent-driven-development and executing-plans.
