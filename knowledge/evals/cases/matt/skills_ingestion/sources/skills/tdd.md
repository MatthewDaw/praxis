---
name: tdd
author: mattpocock/skills
source: https://skills.sh/mattpocock/skills/tdd
---

# tdd

Test-driven development emphasizing behavior-focused tests through public APIs,
with vertical slicing and incremental red-green-refactor cycles.

## When to use
- New features where tests serve as specifications.
- Refactoring safely while keeping confidence in behavior.
- Systems where implementation details change frequently.

## Workflow
1. Planning — confirm interface changes, prioritize behaviors, design for testability.
2. Vertical slicing — write one test → implement → repeat (don't write all tests upfront).
3. Behavior-focused testing — verify behavior through public interfaces, not implementation details.
4. Refactor only after green: extraction, deeper modules, SOLID.
5. Avoid horizontal slicing (all tests first), which produces brittle suites coupled to implementation.
