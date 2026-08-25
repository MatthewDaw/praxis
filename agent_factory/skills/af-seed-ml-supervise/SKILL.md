---
name: af-seed-ml-supervise
description: >
  Make one canonical ml_registry campaign runnable on its actual execution host: inventory real
  data, freeze the judge, build and smoke the project campaign adapter, register canonical
  experiment/model/baseline state, seed the nine-axis IDEA set, deploy the exact revision and
  state to the target, and return an observed launch handoff. Use for "seed the campaign",
  "/af-seed-ml-supervise", "get this ready for af-ml-supervise", or "skip research".
---

# af-seed-ml-supervise

Take one campaign from a question to an execution-target-specific handoff for
`/af-ml-supervise`. Setup is not complete on the laptop when the campaign will run on an EC2 box.
The target host is part of the scientific environment: code, data, registry state, hardware,
throughput, and process ownership must all be proven there.

This skill sets up and seeds. It does not supervise candidate ideas, adjudicate a candidate, move
`production`, or start the unattended campaign unless the human explicitly chains that work.

## Current architecture only

The standard registry is canonical live authority:

- SQLite projections for experiments, runs, artifacts, registered models, model versions, aliases,
  lineage, and events;
- content-addressed blobs and an append-only event log;
- RegistrySpace for IDEA inventory, citations, claims, stages, dependencies, and ratchet state;
- Git for code.

The project owns loaders, preprocessing, training/evaluation, compatibility loading, setup adapter,
and campaign lifecycle adapter. Do not add a second campaign ledger or revive a pre-cutover tabular
lifecycle. Discover the installed surface with `python -m knowledge.ml_registry.cli --help` and use
the canonical campaign job and portfolio commands documented here and in `/af-ml-supervise`.

## Terminal state — READY on the execution target

Do not say READY until every condition is true on the host that will run supervision:

1. **Target identity.** Name the target host, checkout, registry root, RegistrySpace file, campaign
   state root, cache root, and CPU/GPU lane. Local is a target only when the human chose local.
2. **Durable revision.** The target host has a clean checkout at the exact reviewed revision;
   `git rev-parse HEAD` matches locally and the revision is reachable from the durable remote.
   A dirty rsync or unpushed commit is not provenance.
3. **Real adapter data.** On the target host, every required role enumerates through the project
   adapter and decodes at least one real payload/label pair. Fingerprints and licence tiers match
   registration. An upload-in-progress, raw bucket listing, or staging directory is not readiness.
4. **Hardware and disk.** The target host's measured device fingerprint, CPU, RAM, accelerator,
   free disk, cache budget, credentials, dependency lock, and network policy satisfy the declared
   resource lease. Refuse GPU work on a CPU host.
5. **Frozen judge.** Project spec and Experiment agree on scalar metric, direction, operating point,
   split unit, paired-resampling protocol, win condition, stage order, and spec digest.
6. **Project runtime.** A project lifecycle adapter implements preflight, setup, completion/terminal
   outcome, blocking diagnosis, trial count, one-arm dispatch, heartbeat, and void recording. It is
   driven by `knowledge.ml_registry.runtime.campaign_job`; candidate output never writes a verdict.
7. **Canonical baseline.** On the target host, the canonical registry contains the registered model
   and an active baseline ModelVersion. `champion` resolves to it, its artifact verifies and
   compatibility-loads, and every deterministic incumbent pin matches exactly.
8. **Target measurement.** A real incumbent reproduction traverses the same target process, lease,
   adapter, and evaluator path as candidates. The target host writes the canonical baseline Run with
   per-unit paired evidence, typed metrics, device fingerprint, throughput unit, memory, CPU time,
   and load. Laptop throughput is not copied.
9. **Campaign registration.** `register_campaign_for_run(...)` accepts the spec using real score
   rows plus the project structural validator. Its derived rope/evidence is visible in the target
   event log.
10. **Bound IDEA model.** The target RegistrySpace model fact is created through
    `knowledge.ml_registry.write_path.register_model` and explicitly bound by `CampaignBinding` to
    the canonical experiment and registered model. It is not a second model identity.
11. **Closed-nine seed.** `seed-campaign` writes the approved ideas with `origin="seeded"`; all nine
    closed axes are swept, every retrieval axis has a receipt including empty results, and every
    declared stage has an authored arm.
12. **One-arm smoke.** On the target host, an incumbent reproduction/preflight arm runs through the
    actual campaign job, process group, writable roots, cache, timeout, progress heartbeat,
    `create-run`, `complete-run`, and canonical external adjudication seam. It adds exactly one
    expected Run and exits cleanly without consuming a seeded candidate.
13. **Portfolio proof and handoff.** A one-shot portfolio run proves the operator/campaign/capacity
    configs, ownership, restart position, and typed outcome agree. The handoff contains exact target
    start, observe, tail, status, `stop --drain`, `stop --force`, and `resume` commands plus all ids,
    paths, stages, and parked prerequisites.
14. **Executable IDEA frontier.** Every READY IDEA is either bound to a truthful executable
    recipe/arm or the target has a preflighted coding-agent one-arm worker that claims the IDEA,
    authors that hypothesis in an isolated worktree, runs the project trainer/evaluator, and exits
    after canonical adjudication. Prose hypotheses plus an unrelated finite arm list are not an
    executable campaign; never map them by position, modulo, or keyword resemblance.

Laptop success is not READY for a remote campaign. If the target is unreachable, data is still
uploading, the revision is not durable, hardware is wrong, an incumbent pin drifts, or the smoke
does not write one Run, finish every independent setup item and report BLOCKED on the named item.

## Resolve mode and target first

- `skip research`, `--skip-research`, `use the existing ideas`, or `research is done`: reuse the
  newest complete generator/retriever scripts (explicit paths first, then `<project>/registry/`,
  then newest `docs/plans/*-seed-campaign/`). Do not re-run research. Missing scripts are a hard stop.
  Setup and target provisioning still run.
- `existing campaign`, `--existing`, `brownfield`, `expand the campaign`, or `the setup is already
  done`: campaign code, harness, experiment, registered model, frozen metric and baseline ALREADY
  EXIST. Phases A-D become VERIFICATION, not construction: confirm each artefact, create only what is
  genuinely absent, and change nothing that already validates. Phase E still seeds. This is the
  INVERSE of skip-research and the two are routinely confused: skip-research reuses existing IDEAS and
  still builds setup; `existing campaign` reuses existing SETUP and still produces new ideas. They
  compose - both together means verify setup and reuse ideas, which is a no-op rerun, so refuse it and
  say why.
- `--mode batch` approves every scripted candidate. `--mode interactive` presents candidates in
  closed-axis order and consumes one boolean per candidate. Default interactive; skip-research
  implies batch unless overridden.
- Use the host named by the human. Otherwise read project deployment/campaign config. If it names a
  remote/devbox host, that is the target; do not fall back to the laptop because SSH is inconvenient.

## Phase A — inventory real data on the target

Classify each holding as train, score, validation-only, sealed, refuted, duplicate/derived, or
adapter-pending. Catalog presence authorizes a planned idea, not dispatch. Operator timelines and
derived production maps are validation-only unless the project contract explicitly says otherwise.

Verify through target-host adapters, not raw object listings:

- enumerate bounded units and structural partitions;
- decode representative payloads and labels;
- record immutable fingerprints, licence tier, cache bytes, and leakage groups;
- prove sealed labels are unavailable to candidate code;
- reject ephemeral download staging as a runtime source.

If real score labels are unreachable, stop. Never substitute fixtures.

## Phase B — freeze judge and resources

Freeze one scalar and direction. Define operating point, aggregation, split unit, minimum effective
sample, shared seeds, paired bootstrap resamples/confidence, and numeric win condition before the
baseline. Candidate and champion use identical units/seeds; adjudication uses the paired interval
from `/af-ml-supervise`, not the spread or range of independent repeats.

Declare only stages with authored arms. Vision defaults are
`representation,architecture,augmentation,training,tuning,capacity`, but evidence decides. Freeze
the target resource lease, heartbeat cadence, arm timeout, disk/cache budget, device fingerprint,
and target-measured throughput policy.

## Phase C — build the project-owned path

**Greenfield is the exception, not the rule. LOOK BEFORE YOU BUILD.** Most invocations land on a
project that already has campaign code, and a second harness beside a working one violates the
one-implementation-per-problem rule that every project of this shape enforces. Before writing any
loader, trainer, evaluator or dispatch path, inventory what exists: campaign folders and their spec
files, the structural validator that admits a spec, the typed campaign contracts, the evaluator that
computes the frozen metric, and any existing paired-evidence harness.

If a harness exists, Phase C is **wiring, not construction**. Map registry concepts onto what is
there rather than recreating them: an arm is an entry in the existing spec's arm list, not a new
abstraction; the loaders are the project's existing corpus template; the metric is the one the
campaign already declares. Where the registry needs a row shape the project does not emit, write ONE
thin adapter that projects the existing measurement into that row - never a second train/eval path.

If the two cannot be reconciled without duplication, STOP and record the specific incompatibility as
a blocker. Building parallel machinery because reconciling was harder is the failure this paragraph
exists to prevent.

When it is genuinely greenfield, the project owns real loaders, shared preprocessing, trainer/evaluator with per-unit paired evidence,
artifact compatibility loading, and a lifecycle adapter consumed by campaign job. Tests generate
fixtures; separate real-payload checks execute on the target. Long work emits flushed typed progress
inside the heartbeat cadence. Measure CPU time with `resource.getrusage`, not wall time.

Before calling that lifecycle runnable, join the seeded IDEA inventory to execution. A parameter
idea may name an already-implemented arm/config. A prose architecture, data, or training hypothesis
normally needs a coding-agent worker to author one commit-backed arm at dispatch time. Freeze that
worker's one-arm contract and prove its command imports, claims exactly one eligible IDEA, creates
one Run, executes the same train/eval adapters, leaves verdict writing to Praxis, and exits. If
neither path exists, the campaign is setup-incomplete even when its incumbent smoke passes.

Campaign code does not open its registry or decide its verdict. The adapter creates/completes one
commit-backed Run and its artifacts; Praxis adjudication is the only verdict writer. Every Run has a
full `code_ref` and fact id joins, never a display-name or bare-SHA join.

## Phase D — register canonical state on the target

Inspect the actual CLI first:

```sh
python -m knowledge.ml_registry.cli --help
python -m knowledge.ml_registry.cli create-experiment --help
python -m knowledge.ml_registry.cli create-run --help
python -m knowledge.ml_registry.cli complete-run --help
python -m knowledge.ml_registry.cli create-artifact --help
python -m knowledge.ml_registry.cli register-model --help
python -m knowledge.ml_registry.cli adjudicate-run --help
python -m knowledge.ml_registry.cli finalize --help
python -m knowledge.ml_registry.cli registry-status --help
```

The project setup adapter performs this idempotent sequence:

1. validate and register CampaignSpec with
   `runner.register_campaign_for_run(registry, spec, scoring_corpora=real_rows,
   structural_validator=...)`;
2. create the Experiment;
3. reuse or create the canonical registered model;
4. create/complete the real baseline Run, create its artifact, and create baseline ModelVersion plus
   `champion` through canonical registry services;
5. create/save the RegistrySpace model fact with `write_path.register_model` and record its
   `CampaignBinding` to the canonical experiment/model.

Read it all back on the target. An identical rerun writes nothing; drift refuses by named field.
Never mutate a model fact from a different metric/data contract.

## Phase E — seed the closed nine

The exact closed set is:

- generative: `theoretical_math`, `ablation`, `supplements`, `ml_architectures`,
  `non_ml_methods`, `ce_ideate_breadth`;
- retrieval: `current_code`, `prior_trials`, `af_learn_lessons`.

`require_closed_axis` is enforced. Empty axes remain present; empty retrieval gets a receipt. Every
candidate needs `description`, `stage`, and concrete `basis`. The CLI stamps model fact id,
`origin="seeded"`, and axis.

Before seeding, cover four strands:

1. published end-to-end systems, rig/metric, and runnable checkpoint—or reasoned negative;
2. pretrained candidates and the corpus/regime each actually saw;
3. augmentations derived from measured target resolution, optics, visibility, and leakage groups;
4. Karpathy-loop seam: allowed code/axes/budget and immutable judge, or the decision not to run it.

Run against the target RegistrySpace:

```sh
python -m knowledge.ml_registry.cli seed-campaign \
  --space-file <target-space.json> --model-id <model-fact-id> \
  --mode batch \
  --generator-script <generator-script.json> \
  --retriever-script <retriever-script.json>
```

Interactive mode adds `--confirm-script`. With skip-research, do not duplicate existing seeded ideas
for the exact model fact; report their ids.

## Phase F — deploy and prove the target host

1. Push the reviewed revision, update the target checkout without overwriting target work, and prove
   clean HEAD equality.
2. Install from the lockfile under the service account/environment that will own the process.
3. Resolve durable adapter data and credentials, cache only bounded smoke inputs, and record target
   free disk before/after.
4. Materialize target-native registry, RegistrySpace, state/cache roots, resource lease,
   `operator.json`, portfolio/campaign/capacity manifests, and campaign-job config. Never copy
   laptop absolute paths into them.
5. Run target preflight, real incumbent reproduction, compatibility load, campaign-job one-arm
   smoke, one candidate-IDEA dispatch through the exact long-run worker, and portfolio one-shot.
   Verify one new Run per proof, heartbeat, artifact checksum, process cleanup, typed outcome,
   external adjudication, and idempotent restart position. A baseline-only seed-smoke adapter is not
   the long supervisor and cannot satisfy this item.
6. Prepare the detached start command and ownership-aware observe/tail/stop commands. Do not start
   unattended supervision from this skill.

Copy local registry files only if embedded repo paths, artifact URIs, device evidence, and event
history remain valid on target; normally rebuild deterministic seed state and remeasure there.

## Phase G — handoff, then stop

Report target evidence for every READY item, frozen contract, corpus fingerprints, resources,
canonical ids/paths, baseline Run, derived rope, seeded ids/receipts, stage coverage, prerequisites,
and smoke Run. Give exact target commands:

```sh
# detached start
agent_factory/scripts/af-ml-portfolio-launch.sh --config <operator.json> run

# observe/status
python -m knowledge.ml_registry.cli.portfolio --config <operator.json> status
tail -f <portfolio-controller.log>

# controlled stop or restart
python -m knowledge.ml_registry.cli.portfolio --config <operator.json> stop --drain
python -m knowledge.ml_registry.cli.portfolio --config <operator.json> stop --force
python -m knowledge.ml_registry.cli.portfolio --config <operator.json> resume
```

Also give the SSH/service/tmux wrapper when applicable. Point the next operator to
`/af-ml-supervise`; do not start it here.

## Never

- Never declare READY on a different host from execution.
- Never count a bucket prefix, upload, or staging tree as adapter-proven data.
- Never sync a dirty tree or use an unpushed revision as remote provenance.
- Never copy laptop paths, throughput, hardware fingerprints, or baseline evidence into remote
  state.
- Never run a GPU lease on a CPU host or silently change the resource declaration.
- Never let candidate code read sealed labels, edit the judge, self-report a verdict, or move an
  alias.
- Never create a parallel live ledger/registry or revive pre-cutover lifecycle commands.
- Never seed an unbound/legacy model fact, omit an empty receipt, invent a tenth axis, or pad stages.
- Never start `/af-ml-supervise` unless the human explicitly chained it.
