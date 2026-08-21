---
name: af-seed-ml-supervise
description: >
  Stand up ONE ml_registry campaign until it is legal to hand to af-ml-supervise: inventory
  collected data, freeze the metric, build loaders/preprocessors/train-eval/dispatch against
  that data, write four incumbent baseline rows into a version-2 ledger, bootstrap and
  register the model, then seed the starting idea set via the nine-axis closed set.
  Use when the human says "seed the campaign", "/af-seed-ml-supervise", "run seed-campaign",
  "get this ready for af-ml-supervise", or "skip research" (reuse an existing nine-axis
  sweep and only stand the campaign up).
---

# af-seed-ml-supervise

Take a project from "we have some data and a question" to "af-ml-supervise can start
without inventing anything." That is one job with two halves, in this order:

1. **Stand the campaign up** so `bootstrap-campaign` exits 0.
2. **Seed the starting idea set** so `origin="seeded"` ideas are written.
   Research (the nine-axis sweep) is optional on a rerun — see
   **Skip research**.

`af-ml-supervise` is the supervisor half. It consumes what this skill leaves behind.
It does not seed, and it does not build a trainer. This skill does not supervise.

**This is the written `af-ml-ideate`, widened to own setup.** The name that ships is
`/af-seed-ml-supervise`. Idea writes still go through `knowledge.ml_registry.ideate`
(`seed_campaign`) via `python -m knowledge.ml_registry.cli seed-campaign`. Setup
writes live in the **project** repo: loaders, preprocessor, dispatch command, ledger.
Do not reimplement the registry.

## What it is NOT

- **Not `af-ml-supervise`.** That skill dispatches trials, adjudicates against the
  ledger, and runs the ratchet. This one stops when the campaign is READY. Never
  start the supervisor from here unless the human explicitly chained the two.
- **Not `af-ml-model`.** That skill is a thin trigger over Karpathy's autoresearch.
  Different paradigm; do not merge them. Do decide where its loop sits beside this
  backlog, and record the seam — F.1 strand 4.
- **Not a side channel.** Every idea this skill writes goes through
  `knowledge.ml_registry.write_path.register_idea` with `origin="seeded"` and a
  `meta.axis` drawn from `IDEATION_AXES`.
- **Not a licence to invent a metric.** The metric is frozen from what the collected
  data can actually score. If the data cannot support a scalar, stop.

## Terminal state — READY

This skill is not done until every line below is true. A half-setup is not a handoff.

1. **Real data is reachable** and the metric is measurable on it. If the project's
   footage/labels are missing, STOP and fail loudly. Never fall back to synthetic
   fixtures and never write "verified on synthetic tracks".
2. **The project owns a dispatch command** that runs ONE arm, scores the FROZEN
   metric, and appends ONE version-2 ledger row. The writer emits
   `commit  metric_value  memory_gb  status  description  throughput  diff_lines`.
   Do not add those columns by hand.
3. **Join keys are unique per arm.** Write `{sha}:{arm_tag}`, not a bare SHA.
   A config-varying campaign that reuses one SHA collapses the registry join.
4. **The ledger holds ≥4 incumbent baseline rows** (`REQUIRED_BASELINE_RUN_COUNT`),
   measured on real data, with homogeneous throughput. Prefer more than 4 when a
   run is cheap; pass a measured floor rather than letting n=4 invent one.
5. **`bootstrap-campaign` exits 0** and names no blocking precondition.
6. **A model is registered** via `register-model-with-baseline`. The minted
   `model_id` is what later calls use, not a local alias.
7. **The nine-axis closed set has been swept** and `seed-campaign` has written
   `origin="seeded"` ideas. Every retrieval axis has a receipt, including empties.
8. **The four-strand plan has been run and its answers landed as ideas** (Phase
   F.1): already-solved, pretraining corpus, regime-derived augmentation, and
   the Karpathy-loop seam. A backlog whose detectors all share one pretraining
   corpus is not a search, however many architectures it names; a stage padded
   to clear a counter is not coverage.
9. **Every stage you will declare to `campaign-complete` has authored arms.**
   An empty stage is trivially "all answered" and the campaign will sail past a
   question nobody asked (`stage_never_authored` in
   `knowledge.ml_registry.completeness`). Either populate the stage or drop it
   from the plan on purpose.
10. **The handoff pack exists** — space file, minted model id, ledger path,
    dispatch command, stage list, void-throughput setting, and the exact
    `/af-ml-supervise` invocation. A human (or a later turn) can start supervise
    without asking you what to type.

`bootstrap-campaign` is the external check for items 2–6. Do not hand-roll those
preconditions. What is systematic lives in `knowledge.ml_registry.bootstrap`;
what is project-specific is the metric's meaning, the trainer, and the backlog.

## Resolve flags before Phase A

Read `$ARGUMENTS` / the human's prompt once, before any research dispatch.

| flag | how it is said | what it does |
|---|---|---|
| **skip research** | `skip research`, `--skip-research`, `use the existing ideas`, `don't re-research`, `research is done` | Do **not** dispatch the six generative agents or re-query the three retrieval axes. Load an existing `generator-script.json` + `retriever-script.json` and continue with setup + `seed-campaign`. Default `--mode batch` — the human already approved this idea set. |
| **batch / interactive** | `--mode batch`, `--mode interactive` | Confirm seam for `seed-campaign`. Skip-research implies batch unless the human overrides. |

Skip-research does **not** skip setup. Phases A–E still run. The only thing skipped
is producing new ideas. That is the rerun the owner wants: research already
exists, tell the skill to skip it, and when it finishes `/af-ml-supervise` can
start.

### Finding the existing scripts

Resolve in this order; first hit wins:

1. Paths the human named in this invocation.
2. `<project>/registry/generator-script.json` and `retriever-script.json`.
3. The newest `docs/plans/*-seed-campaign/generator-script.json` (and its sibling
   `retriever-script.json`) under the project repo.
4. Per-axis files in that same directory (`theoretical_math.json` …) that can be
   assembled into the two scripts without inventing candidates.

**If skip-research is set and no scripts resolve: STOP.** Name the paths you
looked at. Do not silently start a nine-axis research fleet. Do not invent
candidates to "be helpful". The human said the research is done; missing files
are a missing input, not a licence to redo it.

Validate whatever you load: both files must use the closed-set keys (missing
axis → empty list, not a re-sweep). Do not add an off-set key.

If this model already has `origin="seeded"` ideas in the space and the human
said skip research, do not run `seed-campaign` again — report the existing ids
and continue to the handoff pack. Re-seeding the same descriptions is not a
second measurement.

## Phase A — inventory the data that actually exists

Do this first. Setup that assumes a dataset is how you train on the wrong object.

- Catalogue what the project holds: footage, labels, trajectories-without-pixels,
  refuted sets, S3 prefixes, gitignored local trees. Resolve paths the way the
  project already requires (in `sports_analysis` that is `mvpvu.paths`, never a
  hand-written `REPO_ROOT / "data" / ...`).
- Separate **train**, **validate**, and **refuted**. Conflating them is how
  synthetic or off-scale data once read as evidence.
- State what a human or a model can score today. If labels are too few for the
  metric you wanted, either shrink the metric to what n supports or stop and say
  the campaign is blocked on labelling — do not invent GT.
- Operator clocks, scoreboards, and event timelines are **validation only**
  unless the project's own contract says otherwise. Never silently feed them to
  a model as a prior.

If real footage or labels are unreachable: stop. Name what is absent. Do not
build a harness against fixtures.

## Phase B — freeze the judging contract

Write these down before any code. Changing a metric after registration silently
rebases every prior verdict.

| field | rule |
|---|---|
| `metric` | One scalar the dispatch command writes to `metric_value`. A pair you care about (recall AND precision) is two campaigns or a single combined score you define now — not a footnote. |
| `direction` | `maximize` or `minimize`. |
| `win_condition` | Default `beats baseline by noise_floor`. |
| join key | `{sha}:{arm_tag}` from the first row. |
| `void_throughput_fraction` | **0 for any campaign whose metric is not training speed** (CV, detection, tracking). That disables the SPEED void only; unfair runs still void. Do not hack `baseline_throughput=0.01`. |
| stages | Declare the ordered stage list now if arms are expensive. Vision default: `representation,architecture,augmentation,training,tuning,capacity`. Drop any stage you will not author. Do not keep a name you will leave empty. |
| composing? | Non-composing (dispatch-script written up front) vs composing (project loop, registry only for the verdict). `sports_analysis`'s `src/stroke_lab/campaign.py` is the composing worked example. Choose explicitly. |

A metric the collected labels cannot estimate is not a metric. Refuse it.

## Phase C — build the harness in the PROJECT repo

This is project code, not registry code. The registry will not grow a data loader
for you.

Build, against the inventory from Phase A:

- **loaders** over the real collected data
- **preprocessors** the incumbent and later arms will share
- **train / eval** that scores the frozen metric on a held-out real split
- **a dispatch CLI** that runs ONE arm and appends ONE ledger row
- **tests that GENERATE their fixtures** from reachable inputs. A test that
  assumes a gitignored PNG/mp4/npy exists will fail in every clean worktree.

Ledger writer contract — copy the header from the writer, never type it into a
file by hand:

```
commit  metric_value  memory_gb  status  description  throughput  diff_lines
```

`commit` is `{sha}:{arm_tag}`. `description` for the incumbent rows MUST share a
prefix (`baseline` by default) so `bootstrap-campaign --baseline-prefix` can find
them. `status` is `ok` on a fair run. Throughput is whatever unit the project
owns; for CV it will not gate VOID if `void_throughput_fraction=0`, but the
column still has to be written.

Progress: any step that can run longer than a few minutes emits one flushed
`[progress]` line per unit (`n/total`, elapsed, a score). Use
`agent_factory/scripts/progress.py`. Budget on CPU time
(`resource.getrusage(RUSAGE_SELF)`), not wall clock — wall clock measures the
neighbours.

Worked example of the split: `stroke_lab.campaign` decides WHICH arm, 
`stroke_lab.train` decides what it SCORES and writes `results.tsv`,
`knowledge.ml_registry.verdict` decides whether that score beats the baseline.
Do not let the loop compute its own verdict.

## Phase D — measure the incumbent

Run the dispatch command ≥4 times on the **incumbent configuration**, nothing
else. These rows are the noise floor. They are not ideas.

- Same settings every time. Heterogeneous throughput makes the VOID gate
  meaningless; `bootstrap-campaign` will refuse it. Drop the odd row, do not
  average it in.
- Unique join keys even for repeats: `{sha}:baseline-0` … `{sha}:baseline-3`.
- If a run is cheap, do more than 4 and pass the measured floor in. Four-point
  SDs carry ~40% relative uncertainty; the tennis campaign's n=4 floor was an
  artifact that n=12 exposed.
- Real data only. A baseline measured on fixtures is not a baseline.

## Phase E — bootstrap and register

```sh
cd <praxis repo root>          # the package is not pip-installed; imports need this cwd

python -m knowledge.ml_registry.cli bootstrap-campaign \
    --ledger <project>/results.tsv --backlog <project>/backlog.jsonl \
    --model-id <local-alias> --metric <name> --direction <maximize|minimize> \
    --diff-size-limit 8 --skip-ids <settled> --out-dir <project>/registry \
    [--void-throughput-fraction 0] \
    [--noise-floor <measured>]

# exit 0 and ready:true, or it names the blocking precondition. Fix that. Do not
# register something unadjudicable.

python -m knowledge.ml_registry.cli register-model-with-baseline \
    --space-file <state>.json --meta-json <out-dir>/model_meta.json \
    --ledger <project>/results.tsv
# stdout: OK: registered model model-<hex>
```

`--skip-ids` only for ideas whose **argument still transfers**. A loss under a
different metric or a different representation does not transfer. Check
dependents before skipping — a skip silently kills them.

If `bootstrap-campaign` exits non-zero, this skill is not ready to seed. Fix the
named precondition. Do not invent a model. Do not register around the refusal.

Resolve, once registration succeeds:

1. `--space-file` — the registry space JSON.
2. `--model-id` — the minted id, not the local alias.
3. `--mode` — `interactive` (default unless the human said batch) or `batch`.

## The nine-axis closed set — enforced, not documented

`knowledge.ml_registry.ideate` defines a nine-value CLOSED axis set. Closed is
ENFORCED: every sweep and every write runs `require_closed_axis`, so a
caller-supplied `axes` iterable can only ever narrow the sweep to a subset,
never introduce an off-set axis. An off-set idea would be invisible to the
supervisor's axis-coverage escape valve (which matches only `RETRIEVAL_AXES`)
and would fragment per-axis yield.

The set, with the exact strings the registry will accept and nothing else:

**Six GENERATIVE axes** (`GENERATIVE_AXES`) — a generator proposes candidate
idea metas for ONE axis, given the model fact's own meta (metric, win_condition, …):

| axis | what to propose |
|---|---|
| `theoretical_math` | theoretical-math hypotheses about the metric, the loss, the estimator, the noise floor |
| `ablation` | remove or isolate a component the incumbent already has |
| `supplements` | add a component the incumbent lacks |
| `ml_architectures` | ML architecture families, not width/depth knobs of the same family |
| `non_ml_methods` | non-ML learning methods that could still move the registered metric |
| `ce_ideate_breadth` | a ce-ideate breadth pass: adjacent and implied directions the other five axes did not name |

**Three RETRIEVAL axes** (`RETRIEVAL_AXES`) — a retriever answers ONE axis with
the query it issued and the rows it retrieved. The module derives the execution
receipt from the retriever's own count/ids; a retriever never fabricates its
own receipt:

| axis | what to retrieve |
|---|---|
| `current_code` | the project's current trainer / model code (TODOs, hardcoded knobs, unused paths) |
| `prior_trials` | prior trials already in the registry (this model or siblings) |
| `af_learn_lessons` | the af-learn lesson space tagged for ML research |

`IDEATION_AXES = GENERATIVE_AXES + RETRIEVAL_AXES`. Those nine strings are the
closed set. Never invent a tenth. Never rename one. Never write `architecture`
when the set says `ml_architectures`. An axis whose generator or retriever
proposes nothing this pass still completes the sweep — seeding is a starting
set, not an exhaustive plan.

**Stage coverage is not the same as axis coverage.** After the sweep, map every
kept idea onto the stage list from Phase B. If a declared stage has zero arms,
author them or drop the stage. `campaign-complete` will otherwise block later
with `stage_never_authored`, and that is a setup bug, not a supervise bug.
Author them from the regime, not from the counter: three ideas written to clear
a `stage_thin` check are a stage that reads covered and is not (F.1 strand 3).

**Enumerate model families — and their PRETRAINING CORPORA — before treating
`ml_architectures` as populated.** Arms that differ only in depth or width are
one family. Arms that differ in family but share one pretraining corpus are one
experiment repeated N times: the deciding variable is held fixed while the
document reads as a thorough search. State which families you did not try, which
corpora you did not try, and why. Phase F.1 is where those corpora get named;
this is where you check the sweep actually spent them.

## Generators and retrievers are injected — this skill is the wiring

`seed_campaign` takes three callables: `generator`, `retriever`, `confirm`. The
CLI does not call an LLM, does not grep the repo, and does not prompt a human.
It consumes JSON:

```
--generator-script   {axis: [candidate_meta, ...]}     # the six generative axes
--retriever-script   {axis: {query, rows: [...]}}      # the three retrieval axes
--confirm-script     [bool, bool, ...]                 # interactive only
```

**The SKILL is what wires research onto that JSON.** You run the research. You
write the files. The CLI writes the ideas. Do not call `seed_campaign`
in-process; do not hand-roll `register-idea` in a loop. The CLI is the
entrypoint so the same refusals the tests exercise (`require_closed_axis`,
unregistered model, confirm-script exhaustion) sit between you and the space.

Each generative candidate needs at least `"description"`. The module stamps
`model_id`, `origin="seeded"`, and `axis` itself — do not set those on the
candidate. Extra keys (`basis`, `depends_on`, `stage`) pass through and are
fine when you have them. Put `stage` on every candidate you intend to keep.

Each retrieval row needs `"id"` (the source id: a path, a trial id, a lesson
id) and enough to become an idea meta (at minimum `"description"`). The `"id"`
is stripped before the idea is written and recorded on the receipt.

## Phase F — seed

Only after Phase E. `seed_campaign` refuses an unregistered `model_id`.

### 0. Skip-research short-circuit

If skip-research resolved above, **do not run F.2 or F.3**. Use the loaded
scripts, jump to F.4 (batch unless overridden), then F.6. Record in the
handoff that research was reused and from which paths.

One exception, and only one: F.1 still runs unless the loaded generator script
already carries its four strands — an adopted-system arm or a recorded negative,
arms that vary the **pretraining corpus** and not just the architecture,
regime-derived augmentations, and a loop seam. Skip-research means "the ideas
exist", and a script missing a strand is evidence that strand was never planned.
Append the missing strands to the loaded script rather than re-sweeping the
other axes.

### 1. Plan the campaign before proposing to build

Do this BEFORE F.2. Generative axes propose; this step reads and decides. Run it
first or the generators will spend their budget varying architecture on top of
whatever weights the incumbent already happens to carry.

Four strands, in this priority order, and the order is the point — each one is
cheaper than the one below it:

| # | strand | the question |
|---|---|---|
| 1 | **Already solved?** | has someone published a SYSTEM that does this job? |
| 2 | **Pretraining corpus** | if not, whose WEIGHTS saw a regime like ours? |
| 3 | **Augmentation** | what does OUR measured regime demand of the data? |
| 4 | **The Karpathy loop** | where does on-the-fly generation sit beside the backlog? |

#### Strand 1 — is it already solved, end to end?

Broader than strand 2: not "what trained models fit" but **has someone already
built and published a SYSTEM that solves this campaign's goal?** A challenge
winner, a released pipeline, a benchmark leader. For EACH declared campaign goal,
answer in writing:

- What published system targets this exact goal?
- What does it report, and on what object scale and camera rig?
- Is a checkpoint or a runnable pipeline released?

**Adopting a working solution and measuring it as arm one is strictly better
than rebuilding it over twenty arms.** Make the campaign look before it builds:
an adopted system that scores today converts the rest of the campaign from
"can we reach this?" into "can we beat a known number?", which is a far better
question to spend arms on. Where nothing solves the goal end to end, that
documented negative IS the answer — record it, and the next session does not
re-run this search.

#### Strand 2 — pretraining corpus is a first-class axis

The second question, once strand 1 finds no whole system: whose weights saw a
regime like ours?

**The failure this exists to stop.** A live detection campaign
(`sports_analysis`, `model-533010b57800`, metric `tiny_person_recall_at_p90`,
baseline 0.6076) ran nine arms against a zero-shot COCO incumbent
(`PekingU/rtdetr_v2_r50vd`): three threshold/NMS variants, class-agnostic
scoring, YOLOv8m, YOLO-World, Deformable-DETR, a second-stage patch verifier.
Every one scored at or below the incumbent. The only arm that beat it (0.6123)
added a geometric region prior — not a new model. The regime is a 2560x600
panorama whose median player box is 42.7 x 20.5 px; the eval downscales a
1280x720 broadcast corpus by 0.3588 to match. Grepping all three campaigns'
backlogs — 152 seeded ideas across detection, association and contact_point —
for a model pretrained on people in crowded scenes or on sport rather than
generic COCO (`crowdhuman`, `sportsmot`, `mot17`, `mot20`, `market1501`,
`dukemtmc`, `posetrack`) returned ZERO mentions. COCO's person class is
dominated by large isolated subjects; that regime is small, dense and occluded —
close to its opposite, and CrowdHuman-pretrained detectors are near-standard
practice in the MOT literature for exactly this failure. Nine arms of measured
evidence say it plainly: **seeding N architecture variants that all share one
pretraining corpus is ONE experiment run N times, not N experiments.** It was a
seeding failure, not a supervision failure — the ideas were never written, so
the supervisor could never run them.

Answer all five in writing, and put the answers where a supervisor will read
them (the handoff pack, and the `basis` of the ideas F.1 produces):

1. **What published work already solves THIS problem, or the closest neighbour?**
   Name the models, the benchmark they lead, and the corpus they were trained on.
2. **For each candidate, does its TRAINING DISTRIBUTION match our regime** —
   object scale, crowding, occlusion, camera rig, sport — or only its
   architecture? Pretraining match is what usually decides transfer.
   Architecture usually is not.
3. **Is there a checkpoint we can simply RUN as an arm, at inference cost,
   before anything is trained?** A zero-shot arm that takes minutes is worth
   more than a fine-tune that needs a GPU we may not have.
4. **What does the INCUMBENT's pretraining actually cover, and where does our
   regime fall outside it?** That question is what exposes a whole family of
   unseeded ideas.
5. **If no existing trained model fits, say so explicitly and record WHY.** A
   documented negative is what stops the next session re-running this survey.

#### Strand 3 — augmentation derived from the MEASURED regime

Augmentation is the thinnest stage in every live campaign, and thin for a
telling reason: detection carries 3 augmentation ideas and association 3, and
both sets were padded to clear a `stage_thin` check rather than because three
good ones existed. **A stage padded to satisfy a counter is worse than a stage
dropped on purpose, because it reads as covered.** Phase B already lets you drop
a stage you will not author; use that rather than filling it.

So derive augmentations from the numbers Phase A measured, never from a generic
list. For the 42.7 x 20.5 px object above, the live questions are downscale
consistency (the eval's own 0.3588 resize is a transform the training data may
never see), small-object copy-paste, mosaic, and blur/compression matched to the
rig's optics. For a different regime they will be different — that is the test:
if an augmentation idea would read identically in another project's backlog, it
was not derived from this regime and it is padding.

#### Strand 4 — where the Karpathy loop sits, decided and recorded

The repo ships `/af-ml-model`
(`agent_factory/skills/af-ml-model/SKILL.md`) — Karpathy's autoresearch loop:
mutate, run, keep or revert on a scalar, forever, under NEVER STOP. It is a
different paradigm from this curated backlog (see **What it is NOT**), and
seeding must decide the seam between them explicitly rather than leaving one to
be bolted on later. Record all four:

- **What it is pointed at** — which trainer/config surface it may mutate.
- **What it must NEVER touch**: the frozen judging contract from Phase B (the
  metric, its eval split, `EVAL_TOKENS`-equivalents) and the registered
  incumbent baseline rows. A loop mutating freely against a frozen contract is
  how a campaign silently invalidates its own baseline — every prior verdict
  rebases and nothing announces it.
- **Which axes it OWNS** versus which stay curated. It is good at dense local
  search inside one family — `tuning`, and `training`-stage knobs. It cannot
  discover a checkpoint trained on a different corpus, which is strands 1–3's
  whole job. Do not hand it `ml_architectures` or `representation` and call the
  backlog covered.
- **Its budget and its write path.** Loop-generated ideas are
  `origin="discovered"` under `max_discovered_ideas`, never `origin="seeded"`;
  the write path enforces that budget at the data layer. Seeding never writes a
  discovered idea (see **Never**).

If the answer is "no loop on this campaign", record that too. That is a decision,
and it is a legitimate one.

#### All four strands land as REGISTERED ideas

**The plan's output has to land in the backlog as ideas, not as prose.** A
research doc nothing dispatches is not a seeded arm; an idea nobody registered
cannot be run — and that holds for all four strands, not just the checkpoints.
Each becomes a candidate in the generator script for its axis, with a `stage`
and a `basis`:

| strand | axis it usually lands on | the `basis` must name |
|---|---|---|
| 1 adopted system | `supplements`, or `ce_ideate_breadth` for an adjacent approach | the system, what it reports, the rig it reported on |
| 2 checkpoint | `ml_architectures` / `supplements` | the corpus, the benchmark, the regime mismatch it closes |
| 3 augmentation | `ce_ideate_breadth` / `supplements` | the measured number it was derived from |
| 4 loop seam | recorded in the handoff, plus any curated arm it displaces | which axes it owns and what it may not touch |

Cheapest first: a released system or a runnable zero-shot checkpoint outranks
anything that needs training, and both outrank an architecture variant.

### 2. Dispatch the six generative axes in parallel

Skip this subsection when skip-research is set.

One research pass per generative axis, in parallel, each given:

- the model's meta (`metric`, `direction`, `win_condition`, `baseline`, `noise_floor`, …)
- the ONE axis it is responsible for
- the data inventory and the frozen contract from Phases A–B
- **the F.1 plan**: what already solves this end to end, what is already trained
  for it and on what corpus, where the incumbent's pretraining stops covering our
  regime, the measured numbers augmentation must answer to, and which axes the
  Karpathy loop owns. A generator that does not get this proposes another
  COCO-weighted architecture on an axis already spoken for.
- enough project context to propose a real hypothesis, not a slogan

Each pass returns a list of candidate metas for that axis only. An empty list
is legal. A candidate for a different axis is not — drop it, do not refile it
under the wrong key.

### 3. Run the three retrieval axes and keep the receipts

Skip this subsection when skip-research is set. The loaded retriever script
already carries each axis's receipt (`query` + `rows`).

Issue a real query for each retrieval axis and record what came back, including
nothing:

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
regardless of whether it yielded a seedable idea. An axis that legitimately
found nothing still proves it ran. **Never omit an empty retrieval axis from
the script** to "keep the file tidy": omit the key and the receipt's `query`
is `""` and the axis looks like it was never searched.

### 4. Confirm — interactive or batch

`--mode` is ONE seam (`confirm`). Both modes write ideas of IDENTICAL shape
(`origin="seeded"`, a `meta.axis` drawn from `IDEATION_AXES`); only which
candidates get through differs.

- **`--mode interactive`** (the default unless the human said batch). Present
  every candidate, in the order `seed-campaign` will consume them, and write
  `--confirm-script` as a JSON list of booleans, one per candidate, in that
  order. The order is load-bearing: generative axes in `GENERATIVE_AXES`
  order, then retrieval axes in `RETRIEVAL_AXES` order, candidates in list
  order inside each axis. A short list raises `confirm-script exhausted
  before every candidate was confirmed` and writes nothing further. Required
  in interactive; do not pass an empty list and hope.
- **`--mode batch`.** The CLI uses `always_confirm`. Every proposed candidate
  is written. `--confirm-script` is ignored. Use this only when the human
  asked for batch.

### 5. Write the scripts

```jsonc
// generator.json — keys MUST be the six GENERATIVE_AXES strings
{
  "theoretical_math": [{"description": "...", "stage": "association"}],
  "ablation": [{"description": "...", "stage": "tuning"}],
  "supplements": [],
  "ml_architectures": [{"description": "...", "stage": "architecture"}],
  "non_ml_methods": [{"description": "...", "stage": "association"}],
  "ce_ideate_breadth": [{"description": "...", "stage": "representation"}]
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

Never put an off-set key in either script. The CLI will look up only the
closed-set names; an extra key is silently ignored AND it is a sign you
invented an axis.

### 6. Run seed-campaign

```sh
cd <praxis repo root>

# batch
python -m knowledge.ml_registry.cli seed-campaign \
    --space-file <state>.json --model-id <minted-id> \
    --mode batch \
    --generator-script <generator>.json \
    --retriever-script <retriever>.json

# interactive
python -m knowledge.ml_registry.cli seed-campaign \
    --space-file <state>.json --model-id <minted-id> \
    --mode interactive \
    --generator-script <generator>.json \
    --retriever-script <retriever>.json \
    --confirm-script <confirm>.json
```

Stdout is JSON: `written` (`{axis: [idea_id, ...]}`), `receipts`
(`[{axis, query, count, ids}, ...]`), plus the closed-set lists
`generative_axes` / `retrieval_axes`. An axis that proposed nothing, or whose
candidates were all declined, lands with an empty list rather than being omitted.

Exit 1 is a named registry refusal (`REFUSED [<field>]: ...`). Exit 2 is
malformed input. Do not retry a refusal by renaming the axis or dropping the
receipt.

## Phase G — handoff pack, then stop

Report:

- the frozen metric, direction, void-throughput setting, and stage list
- the dispatch command and the ledger path
- `bootstrap-campaign` ready:true, and the minted `model_id`
- the F.1 plan, all four strands: (1) the published systems that target each
  campaign goal, what they report and on what rig, and whether a checkpoint is
  released — or the reasoned negative if nothing solves it end to end; (2) the
  trained models found, the corpus each saw, which regime dimensions they match,
  which are runnable zero-shot, and where the incumbent's pretraining stops
  covering our regime; (3) the measured numbers each augmentation was derived
  from; (4) the Karpathy-loop seam — what it is pointed at, what it may never
  touch, which axes it owns, its discovered-idea budget, or that no loop runs
  here
- every axis in the closed nine, with the idea ids written on it (empty list included)
- every retrieval receipt: the query issued, the count returned, the ids retrieved
- how many candidates were declined (interactive) or that batch confirmed everything
- that every written idea carries `origin="seeded"`
- that every declared stage has at least one authored arm, or was dropped on purpose

Then give the exact canonical handoff, not a paraphrase:

```sh
# The project-owned CampaignSpec names the composing or agent supervision adapter.
# The canonical portfolio controller runs either mode through campaign_job/agent_session,
# verifies finalize-only production before completion, and owns restart/stop semantics.
agent_factory/scripts/af-ml-portfolio-launch.sh \
    --config <project>/campaign_state/operator.json run --poll-interval 10

python -m knowledge.ml_registry.controller_cli \
    --config <project>/campaign_state/operator.json status
```

Point at `/af-ml-supervise` (`agent_factory/skills/af-ml-supervise/SKILL.md`).
**Do not start `af-ml-supervise` from this skill** unless the human explicitly
asked to chain them. Seeding-plus-setup and supervising are different jobs;
chaining them here would turn a starting set into a campaign the human did
not ask to run.

## Never

- Never invent an off-set axis, rename a closed-set axis, or write an idea
  whose `meta.axis` is not one of the nine. Closed is enforced; do not try
  to talk past it.
- Never start `af-ml-supervise` from this skill unless the human chained them.
  Setup, seed, report, hand off.
- Never adjudicate a verdict. Baseline rows are measurements of the incumbent,
  not trials. The registry decides later arms.
- Never call `register-idea` in a loop as a substitute for `seed-campaign`.
  The CLI is the entrypoint; the module stamps `origin="seeded"` and
  `require_closed_axis`.
- Never omit a retrieval axis that returned nothing. The receipt is the proof
  it ran.
- Never seed against an unregistered model. Finish Phase E first.
- Never set `origin="discovered"` on a seeded candidate. Discovery is the
  supervisor's job, under `max_discovered_ideas`.
- Never seed architecture variants before F.1 has asked whether a published
  system already solves this goal, and named the trained models and corpora that
  already exist for it. N architectures over one pretraining corpus is one
  experiment, not N.
- Never leave the plan as prose. Every strand — adopted system, checkpoint,
  augmentation, loop seam — becomes a seeded idea with a `stage` and a `basis`,
  or a recorded decision in the handoff. Otherwise it will never be dispatched.
- Never pad a stage to clear a thinness counter. Drop the stage in Phase B
  instead; padding reads as coverage and a dropped stage does not.
- Never point the Karpathy loop at the frozen judging contract or the incumbent
  baseline rows, and never let it own `ml_architectures` or `representation` in
  place of a curated arm.
- Never treat this run as an exhaustive plan. A generator or retriever may
  legitimately propose nothing on an axis for this model.
- Never add `throughput` / `diff_lines` to a ledger by hand. The dispatch
  command writes the version-2 row.
- Never use a bare SHA as the join key.
- Never substitute synthetic data for a measurement, and never declare READY
  on a metric the collected labels cannot support.
- Never leave a declared `campaign-complete` stage with zero authored arms.
- Never skip an idea whose argument does not still transfer.
- Never re-dispatch the nine-axis research fleet when the human said skip
  research. Missing scripts are a hard stop, not a prompt to start researching.
