# Domain pack: `tax-1040-2025`

The concrete **domain file contract** for `af-fulfill` (see
`docs/proposals/2026-06-27-af-fulfill-deliverable-spine-proposal.md`). A *domain* is this directory
of reviewable, diffable files — never records in a database. Praxis holds only the per-session run
state; these files define the domain.

This pack is the proving case: a 2025 federal Form 1040 from a single W-2. Every value is grounded in
the harness's existing deterministic logic (`app/rules.py`, `app/tax_engine.py`, `app/schemas.py`) so
the data-driven pack and the Python engine compute the identical return.

## The files (one concern each)

| File | Concern | Consumed by |
|---|---|---|
| `manifest.yaml` | identity, project name, file wiring, the deliverable | af-intake-plan + af-fulfill |
| `requirements.yaml` | the requirement set: what must be gathered, cover-sources, guards | **af-intake-plan** → Praxis facts |
| `fields.yaml` | field schemas + validation (the typed boundary, S6) | af-fulfill (validate inputs) |
| `rules.yaml` | rule **tables** as data: standard deduction, marginal brackets | the evaluator |
| `compute.yaml` | the **calculation graph**: ordered line steps over fields + tables | the evaluator |
| `template.yaml` | output: official AcroForm field map + assumption receipt + hash | the form-fill seam |
| `policy.yaml` | ≤5-question budget, defaults, guardrail scope (S1, S5, S9) | af-fulfill runtime |

## How the pieces are consumed

1. **Author (`af-plan` → `af-intake-plan`).** `af-intake-plan` ingests `requirements.yaml` into Praxis as
   `category:"requirement"` facts with `source:"prd-tax-1040-2025"` (project = `tax-1040-2025`). The
   probe (2026-06-27) confirmed this is all that's needed — no Praxis changes.
2. **Run (`af-fulfill`, once per taxpayer).** Create a Praxis **space** (per-user isolation, confirmed
   by probe). Seed it from `requirements.yaml` (NOT a snapshot — snapshots are space-scoped). Then loop:
   - `praxis_incomplete_requirements("tax-1040-2025")` → the live to-do list.
   - For each, resolve by **cover-source order**: cover from a fact (W-2 extraction), infer, default
     (records an assumption, S5), or **ask** (decrements the budget, S1). Asks ranked by Δ-bottom-line
     (S4) using *provisional* evaluator runs.
   - Validate every value against `fields.yaml` (S6); on a valid cover, `praxis_record_outcome(succeeded)`
     on that requirement — flipping its derived completeness (confirmed by probe).
3. **Produce.** When `completeness_summary` shows 0 incomplete, the **evaluator** runs `compute.yaml`
   over `rules.yaml` to produce every line (the LLM never does math, S2/S10), then the **form-fill seam**
   writes the lines into the official PDF via `template.yaml` and bundles the assumption receipt + hash.

## The data/code line (D4)

Everything domain-specific here is **data in files**. The only code is generic, domain-agnostic runtime
plumbing: the gather loop, the **evaluator**, the **document-extraction** seam, the **form-fill** seam.

The evaluator is NOT a programming language. It dispatches a **closed vocabulary of ops** —
`sum`, `add`, `subtract`, `copy`, `const`, `table_lookup`, `marginal_tax`, `clamp_min`, `round` — over
an ordered, acyclic list of steps. No loops, no user functions, no control flow. Guards
(`requirements.yaml`) use a closed predicate set — `exists`, `eq`, `gt`, `lt`, `gte`, `lte`. Adding a
new op or predicate is a deliberate change to the shared evaluator (code, reviewed), not something a
domain author smuggles in via YAML. That boundary is what keeps "math as data" from becoming a language.
