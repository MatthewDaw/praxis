---
name: systematic-debugging
author: obra/superpowers
source: https://skills.sh/obra/superpowers/systematic-debugging
---

# systematic-debugging

A structured methodology requiring root-cause investigation before attempting any
fix, to prevent symptom-based patching.

## When to use
- Debugging produces recurring failures.
- Quick fixes keep creating new problems.
- You need the underlying issue, not a band-aid.
- Multi-component systems with complex error traces.

## Workflow
Four phases: (1) mandatory first — evidence gathering, error analysis, data-flow
tracing to find root cause; (2) pattern analysis across the system; (3) hypothesis
testing; (4) implementation, with mandatory test cases, only after Phase 1.

Core principle: "ALWAYS find root cause before attempting fixes. Symptom fixes are
failure." Blocks proposing solutions until Phase 1 is done; mandates architectural
review after three failed fix attempts.
