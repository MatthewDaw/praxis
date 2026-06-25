# `pr_knowledge_dogfood/` — does past-PR knowledge actually help a coding agent?

The cheapest experiment that proves or kills the bet behind
[the PR-ingestion proposal](../../../../../docs/proposals/2026-06-24-ingest-commits-and-prs.md):
hand-curate high-signal facts from recent merged Praxis PRs, push the whole set into a coding
agent's context, and A/B a handful of real Praxis tasks — including seeded footguns — on the
**existing** `ClaudeCodeRunner` eval harness.

Plan: [docs/plans/2026-06-24-001-feat-pr-knowledge-dogfood-experiment-plan.md](../../../../../docs/plans/2026-06-24-001-feat-pr-knowledge-dogfood-experiment-plan.md).

## Hypothesis

If knowledge from merged PRs helps an agent work better in this repo, then an agent **with** the
curated facts in context will (a) avoid a documented footgun a **without**-facts agent hits, and
(b) trend toward fewer tokens/turns on the same tasks. A null on either signal means the bet is not
proven and the ingestion pipeline does **not** get built yet.

## How the A/B works

Each task is a **paired** set of cases that are byte-identical except for the seeded facts:

| Arm | Case id | `seeded_insight.direct_to_graph` | Footgun check asserts |
|-----|---------|----------------------------------|-----------------------|
| treatment | `<task>` | the full curated fact set ([facts.md](facts.md)) | footgun **absent** |
| control | `<task>_before` | empty | footgun **present** |

Both arms share an identical `seed_prompt`, the default **whole-file reader** (so *all* curated facts
reach the agent unranked — no retrieval variable), the all-zeros `target_commit` placeholder, and
`needs: [sandbox, file_io]` (only `ClaudeCodeRunner` serves them). Tokens/turns are recorded for both
arms by the harness (`_claude_usage`), so every task carries the quantitative signal regardless of
whether it has a footgun.

### Control polarity — positive assertion, not `xfail`

The plan's KTD described the control as an `xfail` case carrying the footgun-**absence** check (a
failing control = XFAIL). This suite instead uses the **positive-assertion** control shape of
`knowledge/evals/cases/pathlib_preference_before/`: the control asserts the footgun is **present**
(a clean PASS when the blind default fires), and the treatment asserts it is **absent**. Reason: an
`xfail` control on the absence check silently **XPASSes** whenever agent nondeterminism happens to
avoid the footgun blind — reading as a meaningful green when it actually means the footgun never
triggered. The positive control instead **FAILs** loudly in that case, which is the honest signal
that the footgun isn't blind-tempting. This is the same correction the follow-on auto-distill plan
(002) adopted. The "flip" the go-gate looks for is therefore: control exhibits the footgun
(footgun-present PASS) **and** treatment avoids it (footgun-absent PASS).

> **Validity discipline (R8):** before trusting any treatment PASS, confirm the matching control
> actually exhibits the footgun (its footgun-present check PASSes). A control that avoids the footgun
> blind proves nothing — demote or redesign that task.

## Tasks

All three tasks are sealed-box file-writes: the agent has only `seed_prompt` + injected facts +
(for two tasks) a mounted fixture, with Bash/web disallowed and no repo mounted. So the token/turn
signal reflects how directly the agent reaches the right answer, not repo-exploration savings.

### 1. `umap_neighbors` — footgun (strong)

- **Source PR:** `d892e88` *fix(clustering): lower UMAP n_neighbors so topics don't collapse to a blob (#57)*.
- **Fixture:** a `clustering.py` whose `_reduce` sets `n_neighbors=min(15, n - 1)` (the pre-fix value).
- **Task:** a heterogeneous corpus collapses into one mega-cluster; fix the dimensionality-reduction
  step so distinct topics survive. (`output_file: clustering.py` scopes the check to the agent's file.)
- **Blind default:** leave `n_neighbors` at 15 (the umap-learn default; high → over-weights global
  structure → mega-cluster). **Neutralizing fact:** lower it to `min(10, n-1)` (facts.md #1).
- **Check (regex, value-tolerant of whitespace):** `n_neighbors\s*=\s*(?:min\()?\s*15\b`.
  Control asserts it matches (footgun present); treatment asserts it is absent.

### 2. `phoenix_tracing` — footgun (medium validity)

- **Source PR:** `22db05f` *chore(serve): set up Phoenix tracing at app import time*.
- **Fixture:** an `app.py` FastAPI entrypoint with an `if __name__ == "__main__":` block that runs
  `uvicorn.run("app:api", ...)`.
- **Task:** wire up Phoenix/OpenTelemetry tracing so spans export when the app runs under uvicorn.
- **Blind default:** put `setup_tracing()` inside the `__main__` block (the instinctive "startup
  code" home) — dead, because uvicorn string-imports the app and never runs `__main__`.
  **Neutralizing fact:** call `setup_tracing()` at module-import scope (facts.md #2).
- **Check (regex):** `\nsetup_tracing\(\)` — a call at column 0 (module scope). Control asserts it is
  absent (call indented under `__main__`, or absent); treatment asserts it is present.
- **Caveat:** weaker construct validity than `umap_neighbors` — a blind agent may place the call at
  module scope anyway. Confirm the control exhibits the footgun before counting this pair; if it
  reliably doesn't, treat it as a non-footgun quantitative task.

### 3. `supersedes_edge` — convention task (token/turn + convention signal)

- **Source PR:** `f3eecfe` *feat(contradictions): cluster by slot and supersede on custom resolution*.
- **Task (no fixture, create from scratch):** write a `supersede_fact(old_fact_id, new_fact)` helper
  that records that a new fact replaces an old one, naming the relationship/edge type the way this
  codebase does.
- **Convention:** the project names the directional replacement edge `supersedes` (not
  `contradicted_by`, not `replaces`) — facts.md #3.
- **Check:** `contains_text "supersedes"` (treatment) / `not_contains_text "supersedes"` (control).
- **Role:** primarily the quantitative (token/turn) task; its convention check is a *secondary*,
  medium-validity binary signal (a blind agent may pick the word "supersedes" by chance), not a
  load-bearing footgun.

## Files

```text
README.md            # this file (U1)
facts.md             # U2: the curated fact set + per-fact provenance (fact -> source PR/commit)
umap_neighbors/         { case.yaml, fixtures/clustering.py }   # treatment
umap_neighbors_before/  { case.yaml, fixtures/clustering.py }   # control
phoenix_tracing/        { case.yaml, fixtures/app.py }          # treatment
phoenix_tracing_before/ { case.yaml, fixtures/app.py }          # control
supersedes_edge/        { case.yaml }                           # treatment
supersedes_edge_before/ { case.yaml }                           # control
analyze.py           # U4: trial aggregation + R8 go-gate
tests/               # U4: aggregator tests over committed fixture transcripts
fixtures/transcripts/# U4: committed sample transcripts (runs/ is gitignored, so fixtures live here)
RESULTS.md           # U5: per-arm numbers, footgun outcomes, go/no-go verdict
```

## Running it

```bash
# one case, real Claude Code (needs the `claude` CLI logged in; no ANTHROPIC_API_KEY)
uv run python -m knowledge.evals.run umap_neighbors

# the whole experiment, N trials/arm + aggregation + verdict
uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3
```

These cases `SKIP` on non-`ClaudeCodeRunner` backends (they need `sandbox`/`file_io`), so they add no
signal to `--fake` baseline runs.
