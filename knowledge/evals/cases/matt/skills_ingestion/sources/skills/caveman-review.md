---
name: caveman-review
author: juliusbrussee/caveman
source: https://skills.sh/juliusbrussee/caveman/caveman-review
---

# caveman-review

Ultra-compressed code-review comments that strip filler and deliver location,
problem, and fix on one line each.

## When to use
Auto-activates on "review this PR", "code review", "/review", or
"/caveman-review".

## Workflow
Enforce a terse format: `L<line>: <problem>. <fix>.` with optional severity
prefixes (🔴 bug, 🟡 risk, 🔵 nit, ❓ q). Eliminate throat-clearing like "I
noticed" or "you might want to consider"; use exact line numbers, symbol names,
and concrete solutions. Output PR-ready comments while preserving *why* a change
matters when it isn't obvious. For complex issues — security, architectural
disagreements, onboarding — revert to fuller explanations.
