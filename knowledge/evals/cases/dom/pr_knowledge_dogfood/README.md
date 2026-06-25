# `pr_knowledge_dogfood/` — does past-PR knowledge actually help a coding agent?

The cheapest experiment that proves or kills the bet behind
[the PR-ingestion proposal](../../../../../docs/proposals/2026-06-24-ingest-commits-and-prs.md):
hand-curate high-signal facts from recent merged Praxis PRs, push the whole set into a coding
agent's context, and A/B real Praxis tasks — including seeded footguns — on the **existing**
`ClaudeCodeRunner` eval harness.

Plan: [docs/plans/2026-06-24-001-feat-pr-knowledge-dogfood-experiment-plan.md](../../../../../docs/plans/2026-06-24-001-feat-pr-knowledge-dogfood-experiment-plan.md).
Findings: [RESULTS.md](RESULTS.md).

> **v2 (iterated apparatus).** v1's strict gate was NO-GO but apparatus-attributed (see RESULTS.md
> history). v2 acts on that diagnosis: it replaces the invalid `phoenix_tracing` footgun with a valid
> one (`yoyo_lazy_import`), adds a **repo-mounted** task to exercise the exploration-savings lever the
> sealed box couldn't, and gates on **cost-to-correct** (`cost_usd`) instead of biased first-pass token
> volume.

## Hypothesis

If knowledge from merged PRs helps an agent work better in this repo, then an agent **with** the
curated facts in context will (a) avoid a documented footgun a **without**-facts agent hits, and
(b) be cheaper **to a correct result** than the same knowledge delivered late as review feedback.

## How the A/B works

Each task is a **paired** set of cases that are byte-identical except for the seeded facts:

| Arm | Case id | `seeded_insight.direct_to_graph` |
|-----|---------|----------------------------------|
| treatment | `<task>` | the full curated fact set ([facts.md](facts.md)) |
| control | `<task>_before` | empty |

Both arms share an identical `seed_prompt`, the default **whole-file reader** (so *all* curated facts
reach the agent unranked — no retrieval variable), the all-zeros `target_commit` placeholder, and
`needs: [sandbox, file_io]` (only `ClaudeCodeRunner` serves them). `cost_usd` / `num_turns` are
recorded for both arms by the harness (`_claude_usage`).

### Control polarity — positive assertion, not `xfail`

Controls use the **positive-assertion** shape of `knowledge/evals/cases/pathlib_preference_before/`:
the control asserts the footgun is **present** (a clean PASS when the blind default fires), and the
treatment asserts it is **absent**. An `xfail` control on the absence check would silently **XPASS**
whenever agent nondeterminism avoids the footgun — a false win. The positive control instead **FAILs**
loudly in that case (the honest signal that the footgun isn't blind-tempting — exactly what killed the
v1 `phoenix_tracing` task). The "flip" the gate looks for: control exhibits the footgun **and**
treatment avoids it.

> **Validity discipline (R8):** before trusting any treatment win, confirm the matching control
> actually exhibits the footgun (control exhibit-rate ≥ ~2/3). A control that avoids the footgun blind
> proves nothing — demote or redesign that task. (This is the gate v1's phoenix task failed.)

### Cost-to-correct (the gate's cost metric)

First-pass token volume is biased toward the control: it credits the control's *wrong* output as free.
The gate scores **cost-to-correct** instead — knowledge upfront (treatment, one pass) vs the *same*
knowledge delivered late as review feedback (control's first pass **+** a rework turn that mounts the
control's wrong file and supplies the fact). `analyze.py` runs all three (treatment, control, rework)
and gates on `cost_usd`. The sealed box also forbids Bash/web and mounts no repo, so the parent
proposal's "fewer exploration turns" lever barely exists — `repo_mounted_dsn` is the one task that
exercises it.

## Tasks

All four are sealed-box file-writes (Bash/web disallowed). Treatment carries the 13 curated facts;
control is empty.

### 1. `umap_neighbors` — footgun (strong)

- **Source PR:** `d892e88` *fix(clustering): lower UMAP n_neighbors so topics don't collapse to a blob*.
- Fixture `clustering.py` sets `n_neighbors=min(15, n - 1)`; task: fix the heterogeneous-corpus collapse.
- Blind default: leave it at 15 (the umap-learn default). Fact: lower to `min(10, n-1)` (facts.md #1).
- Check (regex): `n_neighbors\s*=\s*(?:min\()?\s*15\b` — control asserts present, treatment absent.

### 2. `yoyo_lazy_import` — footgun (strong; replaces phoenix)

- **Source PR:** `1fdb8be` *fix(migrations): defer knowledge import*; real precedent
  `migrations/m2026_06_23_reject_rename.py` and `migrations/0001_reembed_candidates.py`.
- Create task: write a yoyo migration that calls a function from the `knowledge` package.
- Blind default: a **top-level** `from knowledge... import ...` — the universal Python instinct. yoyo
  execs migration files with the repo root off `sys.path`, so that import raises `ModuleNotFoundError`
  before the step runs. Fact: import `knowledge` **lazily inside the step** (facts.md #9).
- Check (regex): `(?m)^(?:from|import)\s+knowledge\b` (a column-0 import) — control present, treatment
  absent (the import is indented inside the step function).

### 3. `supersedes_edge` — convention task

- **Source PR:** `f3eecfe` *cluster by slot and supersede on custom resolution*.
- Create task: a `supersede_fact(...)` helper; name the replacement edge the project's way.
- Convention: the edge is named `supersedes` (facts.md #3). Check: `contains_text "supersedes"`
  (treatment) / `not_contains_text` (control). Not a load-bearing footgun (a blind agent may pick the
  word by chance) — carries cost-to-correct signal + a secondary convention flip.

### 4. `repo_mounted_dsn` — repo-mounted quantitative task (exploration savings)

- **Source:** the real `knowledge/serve/db.py` (DSN resolution + `PRAXIS_DB_ALLOW_REMOTE` gate, from
  `dcd99d4`) is mounted in the box.
- Task: write a standalone script that connects to the Praxis DB and counts active facts.
- A cold agent must **read** the mounted `db.py` to learn the DSN convention; the treatment carries
  facts #6/#11 so it can skip that reading. Signal: the `cost_usd`/`num_turns` delta — the parent
  proposal's exploration-savings lever, which the no-repo tasks can't show. Soft check:
  `contains_text "load_dotenv"` (scoped to the agent's file via `output_file`). No footgun flip.

## Files

```text
README.md            # this file
facts.md             # the 13 curated facts + per-fact provenance (fact -> source PR/commit)
<task>/                 { case.yaml [, fixtures/...] }   # treatment
<task>_before/          { case.yaml [, fixtures/...] }   # control
  tasks: umap_neighbors, yoyo_lazy_import, supersedes_edge, repo_mounted_dsn
analyze.py           # cost-to-correct orchestration (treatment + control + rework) + R8 gate
test_analyze.py      # offline tests over the pure aggregate/gate functions
fixtures/trials.sample.json  # committed records fixture (a worked GO example)
RESULTS.md / RESULTS.data.json  # findings + verdict, and the raw run data
```

## Running it

```bash
# one case, real Claude Code (needs the `claude` CLI logged in; no ANTHROPIC_API_KEY)
uv run python -m knowledge.evals.run umap_neighbors

# the whole experiment: N trials x (treatment + control + rework), cost-to-correct gate
uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3
```

These cases `SKIP` on non-`ClaudeCodeRunner` backends (they need `sandbox`/`file_io`), so they add no
signal to `--fake` baseline runs.
