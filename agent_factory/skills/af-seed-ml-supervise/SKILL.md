---
name: af-seed-ml-supervise
description: seed ONE registered ml_registry model's starting idea set by sweeping the nine-axis closed set, then hand off to af-ml-supervise. Use when the human says "seed the campaign", "/af-seed-ml-supervise", or "run seed-campaign".
---

# af-seed-ml-supervise

Seed ONE registered model's starting idea set — not an exhaustive plan, not a campaign.
Sweep the nine-axis closed set, write `origin="seeded"` ideas through the registry, report
what landed, and stop.

**This is the written `af-ml-ideate`.** The name that ships is `/af-seed-ml-supervise`. The
code that writes is `knowledge.ml_registry.ideate` (`seed_campaign`) via
`python -m knowledge.ml_registry.cli seed-campaign`. The skill was the missing piece; the
closed-set sweep, the write path, and the CLI already exist. Do not reimplement them.

`af-ml-supervise` is the supervisor half of the same loop. It consumes the backlog this
skill seeds. It does not seed. This skill does not supervise.

## What it is NOT

- **Not `af-ml-supervise`.** That skill dispatches trials, adjudicates against the ledger,
  and runs the ratchet. This one writes the starting ideas and stops. Never start a
  campaign from here.
- **Not `af-ml-model`.** That skill is a thin trigger over Karpathy's autoresearch loop.
  Different paradigm; do not merge them.
- **Not a trainer.** It never trains anything and never writes a ledger row.
- **Not a side channel.** Every idea it writes goes through
  `knowledge.ml_registry.write_path.register_idea` with `origin="seeded"` and a `meta.axis`
  drawn from `IDEATION_AXES`. Ideation adds no write path of its own.

## Preconditions — a registered model, or stop

```sh
cd <praxis repo root>          # the package is not pip-installed; imports need this cwd

python -m knowledge.ml_registry.cli readback \
    --space-file <state>.json --category model
```

`seed_campaign` itself refuses an unregistered `model_id` and names it
(`RegistryValidationError` on `model_id`). Confirm the model is in the space BEFORE
dispatching research. If it is not:

- Tell the human to run `bootstrap-campaign` first (the sequence lives on
  `af-ml-supervise` — ledger, four baseline rows, unique join key, dispatch command).
- Do not invent a model. Do not register one from this skill. Do not seed against a
  model-id that `readback` cannot find.

Resolve, before any research:

1. `--space-file` — the registry space JSON the campaign already uses.
2. `--model-id` — the minted id `register-model-with-baseline` printed, not a local alias.
3. `--mode` — `interactive` (default unless the human said batch) or `batch`.

## The nine-axis closed set — enforced, not documented

`knowledge.ml_registry.ideate` defines a nine-value CLOSED axis set. Closed is ENFORCED:
every sweep and every write runs `require_closed_axis`, so a caller-supplied `axes`
iterable can only ever narrow the sweep to a subset, never introduce an off-set axis.
An off-set idea would be invisible to the supervisor's axis-coverage escape valve
(which matches only `RETRIEVAL_AXES`) and would fragment per-axis yield.

The set, with the exact strings the registry will accept and nothing else:

**Six GENERATIVE axes** (`GENERATIVE_AXES`) — a generator proposes candidate idea metas
for ONE axis, given the model fact's own meta (metric, win_condition, …):

| axis | what to propose |
|---|---|
| `theoretical_math` | theoretical-math hypotheses about the metric, the loss, the estimator, the noise floor |
| `ablation` | remove or isolate a component the incumbent already has |
| `supplements` | add a component the incumbent lacks |
| `ml_architectures` | ML architecture families, not width/depth knobs of the same family |
| `non_ml_methods` | non-ML learning methods that could still move the registered metric |
| `ce_ideate_breadth` | a ce-ideate breadth pass: adjacent and implied directions the other five axes did not name |

**Three RETRIEVAL axes** (`RETRIEVAL_AXES`) — a retriever answers ONE axis with the query
it issued and the rows it retrieved. The module derives the execution receipt from the
retriever's own count/ids; a retriever never fabricates its own receipt:

| axis | what to retrieve |
|---|---|
| `current_code` | the project's current trainer / model code (TODOs, hardcoded knobs, unused paths) |
| `prior_trials` | prior trials already in the registry (this model or siblings) |
| `af_learn_lessons` | the af-learn lesson space tagged for ML research |

`IDEATION_AXES = GENERATIVE_AXES + RETRIEVAL_AXES`. Those nine strings are the closed
set. Never invent a tenth. Never rename one. Never write `architecture` when the set
says `ml_architectures`. An axis whose generator or retriever proposes nothing this
pass still completes the sweep — seeding is a starting set, not an exhaustive plan.

## Generators and retrievers are injected — this skill is the wiring

`seed_campaign` takes three callables: `generator`, `retriever`, `confirm`. The CLI does
not call an LLM, does not grep the repo, and does not prompt a human. It consumes JSON:

```
--generator-script   {axis: [candidate_meta, ...]}     # the six generative axes
--retriever-script   {axis: {query, rows: [...]}}      # the three retrieval axes
--confirm-script     [bool, bool, ...]                 # interactive only
```

**The SKILL is what wires research onto that JSON.** You run the research. You write the
files. The CLI writes the ideas. Do not call `seed_campaign` in-process; do not hand-roll
`register-idea` in a loop. The CLI is the entrypoint so the same refusals the tests
exercise (`require_closed_axis`, unregistered model, confirm-script exhaustion) sit
between you and the space.

Each generative candidate needs at least `"description"`. The module stamps `model_id`,
`origin="seeded"`, and `axis` itself — do not set those on the candidate. Extra keys
(`basis`, `depends_on`, `stage`) pass through and are fine when you have them.

Each retrieval row needs `"id"` (the source id: a path, a trial id, a lesson id) and
enough to become an idea meta (at minimum `"description"`). The `"id"` is stripped
before the idea is written and recorded on the receipt.

## Running it

### 1. Confirm the model

`readback --category model`. If the minted id is missing, stop and tell the human to
`bootstrap-campaign` first.

### 2. Dispatch the six generative axes in parallel

One research pass per generative axis, in parallel, each given:

- the model's meta (`metric`, `direction`, `win_condition`, `baseline`, `noise_floor`, …)
- the ONE axis it is responsible for
- enough project context to propose a real hypothesis, not a slogan

Each pass returns a list of candidate metas for that axis only. An empty list is legal.
A candidate for a different axis is not — drop it, do not refile it under the wrong key.

### 3. Run the three retrieval axes and keep the receipts

Issue a real query for each retrieval axis and record what came back, including nothing:

```sh
# prior_trials — the registry itself
python -m knowledge.ml_registry.cli readback \
    --space-file <state>.json --category trial
python -m knowledge.ml_registry.cli readback \
    --space-file <state>.json --category idea

# current_code — the project's trainer / model sources, not this skill file
# af_learn_lessons — the af-learn / Praxis lesson space for this project
```

Every retrieval axis records an `ExecutionReceipt` — `query`, `count`, `ids` —
regardless of whether it yielded a seedable idea. An axis that legitimately found
nothing still proves it ran. **Never omit an empty retrieval axis from the script**
to "keep the file tidy": omit the key and the receipt's `query` is `""` and the
axis looks like it was never searched.

### 4. Confirm — interactive or batch

`--mode` is ONE seam (`confirm`). Both modes write ideas of IDENTICAL shape
(`origin="seeded"`, a `meta.axis` drawn from `IDEATION_AXES`); only which
candidates get through differs.

- **`--mode interactive`** (the default unless the human said batch). Present every
  candidate, in the order `seed-campaign` will consume them, and write
  `--confirm-script` as a JSON list of booleans, one per candidate, in that order.
  The order is load-bearing: generative axes in `GENERATIVE_AXES` order, then
  retrieval axes in `RETRIEVAL_AXES` order, candidates in list order inside each
  axis. A short list raises `confirm-script exhausted before every candidate was
  confirmed` and writes nothing further. Required in interactive; do not pass an
  empty list and hope.
- **`--mode batch`.** The CLI uses `always_confirm`. Every proposed candidate is
  written. `--confirm-script` is ignored. Use this only when the human asked for
  batch.

### 5. Write the scripts

```jsonc
// generator.json — keys MUST be the six GENERATIVE_AXES strings
{
  "theoretical_math": [{"description": "..."}],
  "ablation": [{"description": "..."}],
  "supplements": [],
  "ml_architectures": [{"description": "..."}],
  "non_ml_methods": [{"description": "..."}],
  "ce_ideate_breadth": [{"description": "..."}]
}
```

```jsonc
// retriever.json — keys MUST be the three RETRIEVAL_AXES strings
{
  "current_code": {
    "query": "grep TODO in train.py",
    "rows": [{"id": "train.py:42", "description": "revisit warmup"}]
  },
  "prior_trials": {
    "query": "registry: sibling model trials",
    "rows": [{"id": "trial-abc", "description": "retry sibling winner"}]
  },
  "af_learn_lessons": {
    "query": "af-learn: lessons tagged ml-research",
    "rows": []
  }
}
```

```jsonc
// confirm.json — interactive only; one bool per candidate, consumption order above
[true, false, true]
```

Never put an off-set key in either script. The CLI will look up only the closed-set
names; an extra key is silently ignored AND it is a sign you invented an axis.

### 6. Run seed-campaign

```sh
cd <praxis repo root>

# batch
python -m knowledge.ml_registry.cli seed-campaign \
    --space-file <state>.json --model-id <id> \
    --mode batch \
    --generator-script <generator>.json \
    --retriever-script <retriever>.json

# interactive
python -m knowledge.ml_registry.cli seed-campaign \
    --space-file <state>.json --model-id <id> \
    --mode interactive \
    --generator-script <generator>.json \
    --retriever-script <retriever>.json \
    --confirm-script <confirm>.json
```

Stdout is JSON: `written` (`{axis: [idea_id, ...]}`), `receipts`
(`[{axis, query, count, ids}, ...]`), plus the closed-set lists
`generative_axes` / `retrieval_axes`. An axis that proposed nothing, or whose
candidates were all declined, lands with an empty list rather than being omitted.

Exit 1 is a named registry refusal (`REFUSED [<field>]: ...`). Exit 2 is malformed
input. Do not retry a refusal by renaming the axis or dropping the receipt.

### 7. Report, then hand off — do not supervise

Report:

- every axis in the closed nine, with the idea ids written on it (empty list included)
- every retrieval receipt: the query issued, the count returned, the ids retrieved
- how many candidates were declined (interactive) or that batch confirmed everything
- that every written idea carries `origin="seeded"`

Then tell the human the backlog is seeded and how to supervise it. Point at
`/af-ml-supervise` (skill `agent_factory/skills/af-ml-supervise/SKILL.md`) and the
`supervise-campaign` invocation there. **Do not start `af-ml-supervise` from this
skill.** Seeding and supervising are different jobs; chaining them here would turn
a starting set into a campaign the human did not ask to run.

## Never

- Never invent an off-set axis, rename a closed-set axis, or write an idea whose
  `meta.axis` is not one of the nine. Closed is enforced; do not try to talk past it.
- Never start `af-ml-supervise` from this skill. Seed, report, hand off.
- Never train, dispatch a trial, write a ledger row, or adjudicate a verdict.
- Never call `register-idea` in a loop as a substitute for `seed-campaign`. The CLI
  is the entrypoint; the module stamps `origin="seeded"` and `require_closed_axis`.
- Never omit a retrieval axis that returned nothing. The receipt is the proof it ran.
- Never seed against an unregistered model. Name `bootstrap-campaign` and stop.
- Never set `origin="discovered"` on a seeded candidate. Discovery is the
  supervisor's job, under `max_discovered_ideas`.
- Never treat this run as an exhaustive plan. A generator or retriever may
  legitimately propose nothing on an axis for this model.
