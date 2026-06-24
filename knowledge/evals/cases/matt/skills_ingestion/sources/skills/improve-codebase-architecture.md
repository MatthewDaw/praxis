---
name: improve-codebase-architecture
author: mattpocock/skills
source: https://skills.sh/mattpocock/skills/improve-codebase-architecture
---

# improve-codebase-architecture

Analyze a codebase to identify architectural friction and propose refactors that
deepen modules for better testability.

## When to use
- Codebases feel hard to test or navigate.
- Modules have shallow interfaces with tightly-coupled components.
- You want better AI-navigability through clearer boundaries.
- Planning refactors informed by domain-specific design principles.

## Workflow
Explore the codebase organically. Surface "shallow modules" and untested seams,
then apply Ousterhout's "deep module" principle (small interfaces hiding large
implementations) to propose refactors. Process: (1) read the domain glossary and
architecture decisions; (2) explore friction points with sub-agents; (3) generate
multiple radically different interface designs in parallel; (4) document findings
as GitHub-issue RFCs with trade-off analysis. Uses precise vocabulary (module,
interface, depth, seam, adapter) over vague terms like "component" or "service".
