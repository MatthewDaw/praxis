---
name: af-seed-ml-supervise
description: >
  Take one ML campaign from a problem statement to a complete, evidence-backed, runnable handoff
  on its execution host: exhaustively qualify every catalogued data source and external prior-art
  lead, wire every admissible source, build or repair the end-to-end training/evaluation path,
  define and verify the judge, register canonical state, seed executable research-backed ideas,
  deploy, and then RUN the campaign itself -- baseline, settle the approach, diagnose and refine --
  in one long-running job that resumes from whatever state the target is already in. Use for
  "seed the campaign", "run the campaign", "/af-seed-ml-supervise", "resume the campaign", or
  "skip research".
---

# af-seed-ml-supervise

Take one campaign from a question all the way to a measured, refined model on its execution host,
in ONE long-running job. Setup, seeding and the campaign are phases of that job, not separate
commands with a human gate between them: answer the objective once at the front, and it runs until
the work is done or something genuinely blocks.

Nothing is complete on the laptop when the campaign will run on an EC2 box. The target host is part
of the scientific environment: code, data, registry state, hardware, throughput, and process
ownership must all be proven there.

This skill owns **everything required before unattended supervision can honestly begin**. A human
who invokes it should be able to return to either a READY handoff whose supplied command starts
the campaign, or a short, evidence-backed list of external authority/data blockers. It does not
supervise candidate ideas, adjudicate a candidate, move `production`, or start the unattended
campaign unless the human explicitly chains that work.

"Seed" is not a registry-only operation. It includes exhaustive data reconnaissance, adapter
completion, a runnable trainer/evaluator, prior-art research, research-backed hypotheses, a real
research-arm rehearsal with output audit and repair, target deployment, and proof that a
non-incumbent idea can traverse the same path as the future campaign. Do not hand the human a
checklist of ordinary engineering left for them to do.

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

The executable shell drivers `af-ml-campaign-loop.sh`, `af-ml-campaign-queue.sh`,
`af-ml-agent-queue.sh`, and `af-ml-supervise-keepalive.sh` are retired refusal shims. Never use,
copy, refresh, or remotely launch them. A project `CampaignLifecycle` adapter executed by
`knowledge.ml_registry.runtime.campaign_job`, under the canonical portfolio operator, is the only
supported long-running control plane.

## One command, resumed from derived state

This skill is ONE long-running job, not a setup step someone chains a campaign onto. Invoke it and
it takes the campaign as far as it can go: finish whatever setup is unfinished, then start, resume
or continue the campaign itself, and keep going until the work is done or something genuinely
blocks.

**Human input happens once, at the front.** Resolve the target and lane, then answer B0's ten
objective slots. Everything after that runs unattended. Do not return to the human for a decision
the spec already froze, and do not stop at READY to ask permission to continue -- READY is a
checkpoint in one job, not the end of it.

**Derive the current state from artifacts, never from a stored phase marker.** A checkpoint file
says what someone believed last time; artifacts say what is actually true, and they stay true
across a killed session, a manual fix, or a change someone made by hand. Probe in order and run the
first unsatisfied phase:

| evidence on the target | phase already satisfied |
|---|---|
| corpus matrix recorded, adapters decode real payloads | A |
| `spec.yaml` validates and its ten objective slots are answered | B |
| the harness runs C-END's trivial model end to end | C |
| the registry holds the experiment, registered model and binding | D |
| E1 ideas exist for this model fact with their receipts | E |
| a candidate arm has reached a recorded verdict on this host | F |
| a champion stands on a measured baseline | campaign C1 |
| the exclusive tier is settled | campaign C2 |

A phase whose evidence is present is verified, not re-run; a phase whose evidence is absent or
fails verification is executed. That is the same rule brownfield already follows, applied to the
whole job.

**Three ways to arrive, one code path.** `fresh` ignores derived state and rebuilds; `resume` picks
up an interrupted phase; `continue` extends a campaign that already reached a verdict frontier with
another refinement round. They differ only in where the probe is allowed to start, so there is no
separate mode logic to keep consistent.

**Stop for a blocker, not for a boundary.** Blockers are: an unanswered objective slot, an
unreachable target, absent score labels, a judge that cannot separate its trivial predictor, a
same-family train-over-sealed corpus collision, or a harness that fails C-END. Report those and
stop. Everything else -- a rejected arm, a failed candidate, an exhausted stage -- is the campaign
working, and it continues.

## Terminal state — READY on the execution target

Do not say READY until every condition is true on the host that will run supervision:

1. **Target identity.** Name the target host, checkout, registry root, RegistrySpace file, campaign
   state root, cache root, and CPU/GPU lane. Local is a target only when the human chose local.
2. **Durable revision.** The target host has a clean checkout at the exact reviewed revision;
   `git rev-parse HEAD` matches locally and the revision is reachable from the durable remote.
   A dirty rsync or unpushed commit is not provenance.
3. **Real adapter data.** On the target host, every required role enumerates through the project
   adapter and decodes at least one real payload/label pair. Fingerprints match registration. An upload-in-progress, raw bucket listing, or staging directory is not readiness.
4. **Hardware and disk.** The target host's measured device fingerprint, CPU, RAM, accelerator,
   free disk, cache budget, credentials, dependency lock, and network policy satisfy the declared
   resource lease. Refuse GPU work on a CPU host.
5. **Frozen judge.** Project spec and Experiment agree on scalar metric, direction, operating point,
   split unit, paired-resampling protocol, win condition, stage order, and spec digest.
6. **Project runtime.** A project lifecycle adapter implements preflight, setup, completion/terminal
   outcome, blocking diagnosis, trial count, one-arm dispatch, heartbeat, and void recording. It is
   driven by `knowledge.ml_registry.runtime.campaign_job`; candidate output never writes a verdict.
   The harness is finished: real data has run end to end through adapter, transforms, loss and judge
   behind a deliberately trivial model (C-END), the loss moved with its parameters, and the trivial
   model's recorded score is the measured trivial-predictor floor. Trying a new idea must require
   plugging in a model and nothing else -- if it needs an edit to a loader, transform, loss, metric
   or scoring path, the harness is not finished. Repairing a genuine defect later is expected and
   allowed; it obliges marking prior runs stale and re-measuring baseline and champion under the
   repaired harness.
7. **Canonical baseline.** On the target host, the canonical registry contains the registered model
   and an active baseline ModelVersion. `champion` resolves to it, its artifact verifies and
   compatibility-loads, and every deterministic incumbent pin matches exactly.
8. **Target measurement — exactly one baseline calculation.** Run the incumbent over the complete
   frozen scoring population exactly once on the target host. That calculation traverses the target
   process, lease, adapter, and evaluator path and writes the sole canonical baseline Run with
   per-unit evidence, typed metrics, device fingerprint, throughput unit, memory, CPU time, and
   load. Laptop throughput is not copied. Never repeat the complete baseline to estimate variance,
   populate multiple rows, or satisfy a generic run-count convention: the time cost is not justified
   during seeding, and candidate adjudication uses the frozen paired-resampling judge.
9. **Campaign registration.** `register_campaign_for_run(...)` accepts the spec using real score
   rows plus the project structural validator. Its derived rope/evidence is visible in the target
   event log.
10. **Bound IDEA model.** The target RegistrySpace model fact is created through
    `knowledge.ml_registry.write_path.register_model` and explicitly bound by `CampaignBinding` to
    the canonical experiment and registered model. It is not a second model identity.
11. **Closed-nine seed.** `seed-campaign` writes the approved ideas with `origin="seeded"`; all nine
    closed axes are swept, every retrieval axis has a receipt including empty results, and every
    declared stage has an authored arm.
12. **One-arm smoke.** On the target host, a bounded preflight arm runs through the actual campaign
    job, process group, writable roots, cache, timeout, progress heartbeat, `create-run`,
    `complete-run`, and canonical external adjudication seam. Use the smallest real, leakage-safe
    slice that proves the plumbing. It adds exactly one clearly non-baseline smoke Run and exits
    cleanly without consuming a seeded candidate. It must not repeat the complete baseline
    calculation from item 8. Then run ONE real candidate arm through the same path to a recorded
    verdict -- `rejected` is a perfectly good outcome. The incumbent smoke exercises none of the
    candidate wiring, so idea binding, verdict vocabulary and promotion are all first exercised by
    the supervisor unless a candidate is run here. In one campaign that first candidate surfaced
    three registry contract errors the incumbent smoke had passed straight over.
13. **Portfolio proof and handoff.** A one-shot portfolio run proves the operator/campaign/capacity
    configs, ownership, restart position, and typed outcome agree. Inspect the spawned command and
    prove it is `knowledge.ml_registry.runtime.campaign_job --config <campaign-job.json>`; a clean
    exit from a retired shell shim is impossible evidence. The handoff contains exact target start,
    observe, tail, status, `stop --drain`, `stop --force`, and `resume` commands plus all ids, paths,
    stages, and parked prerequisites.
14. **Executable IDEA frontier.** Every READY IDEA is either bound to a truthful executable
    recipe/arm or the target has a preflighted coding-agent one-arm worker that claims the IDEA,
    authors that hypothesis in an isolated worktree, runs the project trainer/evaluator, and exits
    after canonical adjudication. Prose hypotheses plus an unrelated finite arm list are not an
    executable campaign; never map them by position, modulo, or keyword resemblance.
15. **Exhaustive data disposition.** Every database/corpus in the project's data catalog has a
    recorded campaign-specific disposition. Every source that can legally and honestly improve
    training, selection, scoring, robustness measurement, or target-regime generalization is
    wired through a tested project adapter and used in the declared role. Every non-admitted source
    has evidence for its exclusion (for example label mismatch, no media join, leakage,
    duplicate derivation, target-scale mismatch, or inaccessible payload). "Not inspected" and
    "probably irrelevant" are never dispositions.
16. **Research-arm rehearsal and audit.** A bounded, non-incumbent research arm has trained,
    selected, scored, and produced the same report/artifact shape as a long run on real target-host
    data. Its outputs, failure renders, per-source/per-unit metrics, calibration, resource use,
    lifecycle records, and operator visibility have been audited. Every defect found has been fixed
    and the relevant proof rerun; the handoff names the arm, findings, repairs, and final evidence.

Laptop success is not READY for a remote campaign. If the target is unreachable, data is still
uploading, the revision is not durable, hardware is wrong, an incumbent pin drifts, or the smoke
does not write one Run, finish every independent setup item and report BLOCKED on the named item.

## Phase 0 — exhaust the evidence surface before choosing the campaign regime

Do this unless the human explicitly requests `skip research`. It is not enough to inspect the
first convenient registered corpus or repeat a campaign's existing data rows.

1. **State the target contract first.** Write the target ontology, inference inputs, desired
   output, deployment regime/camera assumptions, exact metric, and leakage unit. Distinguish
   labels available at training/scoring time from inputs available in production. A labelled
   position, identity, team, or outcome must never silently become an inference feature merely
   because a corpus provides it.
2. **Decide greenfield or brownfield MODEL, and say which.** Is there already a model for this
   ontology, or is this the first? Answer it explicitly and record the evidence, because it decides
   what the incumbent is and therefore what every candidate is measured against.

   This is NOT the same question as the `existing campaign` mode, which asks whether campaign SETUP
   exists. A brand-new campaign with no registered experiment can still target a model the project
   built long ago, and treating that as greenfield throws the existing model away.

   Look in all four places, and a hit in any of them makes the campaign brownfield:

   - the production tree, for a model family serving this ontology;
   - the registry, for a registered model, champion alias or prior campaign artifact;
   - **purged and archived code** -- `git log --diff-filter=D`, archive directories, and any
     `ml_archive`/`legacy` tree. Code deleted in a cleanup is still evidence, and the measurement
     recorded beside it is often the most valuable thing in the repository;
   - prior campaign reports and ranked follow-ups, which routinely name the thing this campaign is
     about to reinvent.

   A brownfield campaign starts from the existing model as its incumbent. A greenfield one must
   justify, in writing, why nothing found above serves -- "I did not find one" is only a finding
   after all four have actually been searched.
3. **Exhaust the whole local catalog.** Read every row in the complete project data catalog,
   registered-adapter registry, adapter-gap audit, inactive/archived campaign manifest, decision
   record, prior experiment artifact, and owner-data inventory. Do not sample by filename, sport,
   or apparent relevance. For every corpus record label ontology, pixel/media availability,
   temporal continuity, camera/regime/scale, split/leakage groups, size/cost, and possible
   train/selection/score/validation role.
4. **Find DATA that is not in this repository.** Phase 0 is about what the campaign can be
   measured on, so this round hunts corpora and labels only. Search primary sources, official
   dataset pages and the data releases behind papers, using task synonyms and neighbouring sports
   rather than the campaign's initial name. For each serious lead run a bounded verification of
   accessibility, labels, media pairing, and evaluation suitability. A documented negative is
   useful evidence; do not quietly omit a rejected lead.

   **Searching for SOLUTIONS -- models, published methods, runnable systems -- is Phase E, not
   here.** They are different questions asked for different reasons: this one decides what regime
   is measurable at all, and answering it does not require knowing how anyone models the problem.
   Doing both at once is how a promising method quietly starts driving the choice of data.
5. **Produce the complete data decision matrix.** Assign each catalogued corpus and serious
   external lead exactly one disposition: `admit`, `training-only`, `selection-only`,
   `scoring-only`, `validation-only`, `research-only`, `refuted`, `adapter-pending`, or
   `blocked-external`. Compare it to the target regime rather than ranking sources by raw size.
   The matrix is incomplete if it cannot account for every catalog row. At least one real,
   independent scoring source is mandatory; if none exists, the campaign cannot be READY and the
   smallest required labelled-data acquisition is the explicit blocker.
6. **Close every admissible adapter gap.** For every admitted source lacking a project adapter,
   implement or repair the adapter and tests during this invocation. It must expose immutable
   units, independent leakage groups, label/media joins, bounded real decoding, fingerprints, and
   honest partitions. Do not defer routine ingestion, parsing, manifests, or label normalization to
   a later human task. Do not wire a source merely to make the matrix look exhaustive when its
   label contract makes it harmful or unusable.
7. **Prove the proposed regime.** Render/inspect representative real examples from every admitted
   source and measure label census, resolution/scale, missingness, class balance, and
   source-to-target mismatch. Revise the target contract, corpus roles, or campaign scope when
   evidence demands it; do not preserve the original idea at the cost of an invalid experiment.

Save the corpus matrix, external-search receipts, adapter probes, rejected-source reasons, and
unresolved external blockers with the campaign plan. The final handoff links them. A prose claim
that data is "available" is not a completed data investigation.

## Resolve mode and target first

- `skip research`, `--skip-research`, `use the existing ideas`, or `research is done`: reuse the
  newest complete generator/retriever scripts (explicit paths first, then `<project>/registry/`,
  then newest `docs/plans/*-seed-campaign/`). Do not re-run research. Missing scripts are a hard stop.
  Setup and target provisioning still run.
- `existing campaign`, `--existing`, `brownfield`, `expand the campaign`, or `the setup is already
  done`: campaign code, harness, experiment, registered model, frozen metric and baseline ALREADY
  EXIST.

  **Every phase still runs, and every READY condition still has to be met.** Brownfield changes
  what a phase BUILDS ON, never which phases execute: A-D become verification rather than
  construction -- confirm each artefact against the same bar, create only what is genuinely absent,
  change nothing that already validates -- and a verification that fails is a defect to fix in this
  invocation, not a reason to accept the artefact as-is. Skipping a phase because something already
  exists there is how an unverified harness, an unmeasured baseline or an unsequenced backlog gets
  inherited unexamined. Phase E still seeds. This is the
  INVERSE of skip-research and the two are routinely confused: skip-research reuses existing IDEAS and
  still builds setup; `existing campaign` reuses existing SETUP and still produces new ideas. They
  compose - both together means verify setup and reuse ideas, which is a no-op rerun, so refuse it and
  say why.
- `--mode batch` approves every scripted candidate. `--mode interactive` presents candidates in
  closed-axis order and consumes one boolean per candidate. Default interactive; skip-research
  implies batch unless overridden.
- Use the host named by the human. Otherwise read project deployment/campaign config. If it names a
  remote/devbox host, that is the target; do not fall back to the laptop because SSH is inconvenient.

## Phase A — materialize the admitted data on the target

**Licensing is out of scope for this project and must not be checked, recorded, or raised.** This
is personal research: rights are already cleared for every holding, including the restricted ones.
Do not classify corpora by licence tier, do not exclude a source on licence grounds, and do not
attach a licence caveat to a recommendation or a handoff. A source is admitted or refused on
evidence about its LABELS, media join, leakage, regime match and accessibility -- never its terms.


Classify each holding as train, score, validation-only, sealed, refuted, duplicate/derived, or
adapter-pending. Catalog presence authorizes a planned idea, not dispatch. Operator timelines and
derived production maps are validation-only unless the project contract explicitly says otherwise.

Verify through target-host adapters, not raw object listings:

- enumerate bounded units and structural partitions;
- decode representative payloads and labels;
- **assert the specific classes and fields the plan depends on are present in the bytes, and record
  their counts.** A catalog row is a claim, not evidence. One afternoon of checking found four rows
  whose stated content was absent: a basketball instants set advertising "ball keypoints" with no
  ball annotation, a corpus named `*-court-ball` containing only court keypoints, an action corpus
  whose tubes are people rather than the ball, and a tracking set declaring a `ball` category it
  never populates. Refuse the corpus for that role rather than discovering it mid-campaign;
- record immutable fingerprints, cache bytes, and leakage groups;
- prove sealed labels are unavailable to candidate code;
- reject ephemeral download staging as a runtime source;
- **check the corpus against every OTHER campaign's declared roles before admitting it.**

### The cross-campaign corpus ledger

A corpus is not free just because this campaign has not used it. Projects of this shape share model
families across campaigns, and that is what makes reuse dangerous:

> **A corpus held as sealed or test data for a model family must not be used for TRAINING by any
> campaign that touches the same family.** Doing so contaminates the other campaign's test set
> retroactively, and nothing in that campaign will ever notice -- its numbers simply become wrong.

Cross-family reuse is fine and should be recorded rather than refused: two campaigns training
different families on the same units leak nothing.

Build the check from what the project already keeps -- the committed consumer ledger beside the data
catalog, plus every registered campaign spec's `corpora[].roles`. For each corpus this campaign
wants, resolve: which campaigns already declare it, in which role, against which model family. Then

- refuse a same-family train-over-sealed collision outright, naming the campaign it would corrupt;
- record every other overlap in the corpus matrix, with the campaigns and roles named.

**Regenerate the ledger before trusting it.** A committed ledger is a snapshot, and a stale one is
worse than none: it will confidently report that nothing collides. Check its source commit against
HEAD and rebuild if they differ.

### Build what is missing, and repair what is about to carry weight

Adapters are part of Phase A's deliverable, not a prerequisite someone else satisfies.

**Build every adapter an admitted source lacks.** A source classified `adapter-pending` is not
admitted until it enumerates units, exposes leakage groups and label/media joins, decodes real
bytes on the target, and carries a committed manifest and tests. Do not defer ingestion, parsing,
manifest generation, or label normalization to a later human task.

**Repair any existing adapter this campaign is about to make load-bearing.** An adapter that has
only ever served one campaign's narrow slice will be asked for something new; check what THIS
campaign needs from it and fix the adapter, in the adapter, before building on it. The same applies
to shared loaders sitting behind adapters: if a campaign depends on one, its correctness is now in
scope, and its OTHER consumers must be re-checked when you change it.

**Never work around an adapter defect in campaign code.** The tell is knowledge leaking upward: if
campaign code carries a corpus's archive layout, member prefix, annotation format, class ordering,
or which track id is which object, that knowledge belongs in the adapter and its absence there is a
defect you are hiding. Working around it means the next campaign pays the same cost, and the
project ends up with two implementations of one problem.

Four real examples from one campaign, all of which were worked around rather than fixed:

- a shared ground-truth loader whose frame cap counted *distinct frames seen*, which silently
  truncated an identity-major MOT file to a single track. It was already load-bearing for another
  live campaign consuming the same corpus at the same cap;
- an adapter exposing a MOT conversion in which two of six camera views ship no ground truth, while
  the raw CSVs it does not expose annotate the ball in all six;
- a per-unit payload that strips the member prefix its own manifest records, so addressing members
  by the manifest's prefix silently missed them;
- a person-tracking parser that drops the ball on purpose, forcing a second corpus reader to exist
  beside it.

Each was cheap to fix in the adapter and expensive to leave. Fix them here.

Run the same label/media and partition probes established in Phase 0 after target materialization.
A source that passed locally but is unavailable, differently credentialed, or different at the
target is no longer admitted; repair it or refuse readiness. Record the target-side corpus matrix
as the actual training-data contract.

If real score labels are unreachable, stop. Never substitute fixtures.

## Phase B — define the objective and the judge

**B0 is a blocking clarification loop with the human. Nothing downstream is designed until the
objective is fully specified, because the objective is what every later decision is derived from.**
Ask, do not assume; a defaulted objective silently decides the corpus roles, the label rule, the
metric and the win condition, and every one of those is then wrong in a way no later gate detects.

Nothing is BUILT here and nothing is sealed here. Phase B produces a specification; Phase C builds
it and proves it runs; Phase D registers it, and registration is what puts it under change control.
"Freeze" is the wrong verb for work that has not started.

Under change control does not mean untouchable. A real defect found later gets FIXED, on the spot,
in the harness -- see the repair rule at the end of Phase C. What registration buys is that the
change becomes visible and costed instead of silent, and that no candidate can quietly adjust the
thing measuring it in its own favour.

Two distinct things are specified here and they must not be collapsed:

* the **objective** — what the trainer optimises. Differentiable, per-sample, a proxy.
* the **judge** — what decides promotion: scalar metric, direction, operating point, split unit,
  aggregation, paired-resampling protocol, win condition. Candidate code may never edit it.

Their disagreement is diagnostic. A falling loss beside a flat judge means the judge is broken or
the proxy is misaligned, and you can only see that if they are separate objects.

### B0 — the objective specification, answered explicitly by the human

Refuse to proceed while any slot is unanswered or answered vaguely. Re-ask the specific slot; do
not fill it from context, from the corpus, or from what a similar campaign did.

1. **Prediction unit.** What is predicted, and per what — frame, clip, event, track, pixel?
2. **Output space.** Binary, fixed N-way, variable-N choice, regression, set, or interval.
3. **Correctness rule, including partial credit.** For anything temporal this is THE question: is a
   detection within +/-k frames correct, and what is k? Spotting campaigns are meaningless until
   the tolerance window is a number.
4. **Error costs.** Relative cost of a miss, a false alarm, and a misattribution. If they are not
   equal, the metric must reflect that and the loss should too.
5. **Base rate.** The measured positive-class prior. For rare events state it as a number from the
   real labels, not an estimate.
6. **Training objective and its alignment to 3-5.** Name the loss and say how it handles the base
   rate: reweighting, focal, negative sampling, or nothing and why nothing is defensible.
7. **The trivial predictor.** What does always-negative, always-majority, or constant output score
   under the proposed judge? If that number is respectable, the judge is not yet a judge.
8. **Aggregation unit and its independence.** What makes two units independent evidence.
9. **Operating point policy.** Frozen threshold, argmax, or tuned — and if tuned, tuned on what.
10. **The win.** The numeric condition, on the paired interval, that promotes a candidate.

Record the ten answers in the campaign plan. A later phase that contradicts one of them is a
defect in that phase, and never a reason to revise the objective quietly.

### B1 — specify the judge

Name one scalar and direction. Define operating point, aggregation, split unit, minimum effective
sample, shared seeds, paired bootstrap resamples/confidence, and numeric win condition before the
baseline. Candidate and champion use identical units/seeds; adjudication uses the paired interval
from `/af-ml-supervise`, not the spread or range of independent repeats.

Baseline setup has a hard run-count budget: one complete baseline calculation and one canonical
baseline Run. Do not use repeated seeds, repeated inference, or duplicate baseline rows to
estimate a noise floor during seeding. A separate lifecycle smoke is bounded plumbing evidence,
not another full baseline measurement and not another baseline Run.

Declare only stages with authored arms. Vision defaults are
`representation,architecture,augmentation,training,tuning,capacity`, but evidence decides. Freeze
the target resource lease, heartbeat cadence, arm timeout, disk/cache budget, device fingerprint,
and target-measured throughput policy.

**Declare what the host must look like for a measurement to count.** A shared box makes throughput,
CPU time and wall clock into fiction, and nothing notices because the numbers still look like
numbers. The lease therefore carries a load ceiling as well as a core count -- the maximum
1-minute load average, relative to the declared cores, under which a measurement is admissible.

`os.getloadavg()` is already recorded on every run in most harnesses of this shape; what is missing
is a consequence. Give it one: a run whose observed load exceeds the ceiling is completed with
`validity` marked contended rather than valid, and a contended measurement may not be compared for
throughput, may not set a baseline, and may not promote. It stays in the ledger as evidence, which
is the point -- a silently-degraded number is worse than an obviously-quarantined one.

**Every bound that changes the measurement belongs in the spec, never in an env default or a
launcher script.** Frame caps, unit caps, clip caps and sampling seeds define what was measured as
surely as the metric does, and a candidate measured under a different bound is not comparable to
the champion. Register them with the experiment. A campaign that reads a cap from the environment
must treat the spec value as the floor and record the effective value on every artifact.

This is not hypothetical: a ball-possession campaign registered its baseline at a 300-frame window
while the operator's launcher still defaulted to 60, and the portfolio then scored 0.6029 against a
0.3697 champion -- a difference caused entirely by the bound and invisible in both reports.

### B2 — size every measurement to the decision it serves

A measurement that takes hours has to earn them. **The sample is derived from the QUESTION, never
from the size of the corpus**, and the full corpus is for final convergence and promotion evidence,
not for finding out whether an idea is worth pursuing. Most of what a campaign does needs far less
data than it defaults to using.

Declare four measurement kinds and what each is allowed to spend:

| kind | question | sample |
|---|---|---|
| screen | is this worth more compute? | the smallest set that could show an effect you would act on |
| confirm | does it beat the incumbent? | the declared judge in full, at or above the minimum effective sample |
| diagnose | where does it fail? | chosen for coverage of failure modes, not statistical power; deliberately skewed to hard units |
| regress | did we break something? | a small fixed set, run often |

Treat those four as VOCABULARY, not a quota. How many units it actually takes to show an effect, to
locate a failure, or to be confident nothing broke depends on the effect size, the variance and the
units themselves -- and whoever is running the arm can see all three and this document cannot. Pick
the smallest sample that answers the question in front of you, escalate when it does not, and do not
wait for permission to stop.

**What is NOT optional is recording what you ran on.** Every run carries, beside its metrics:

- the measurement kind it was serving;
- the number of split units, and a fingerprint of WHICH units -- not just how many;
- what it cost, in CPU time and wall clock.

Without that, two numbers from the same campaign are not comparable and nobody can tell why one took
four minutes and the other four hours. With it, the campaign accumulates its own evidence about how
much data its questions actually need, which is the only way that judgement gets better instead of
being re-guessed every time. The unit fingerprint is the load-bearing part: `n=40` twice over
different units is not a repeat measurement, and only the fingerprint distinguishes them.

**An arm earns a bigger sample by surviving a smaller one.** Escalating on survival is cheap; a
campaign that confirms every idea at full scale spends most of its budget proving that bad ideas are
bad.

**And escalating re-baselines the incumbent.** The champion is re-measured on the new sample before
anything is judged against it. A candidate that scores lower on a bigger sample than the champion
scored on a smaller one has NOT lost -- the numbers were computed over different units, and a wider
sample usually includes harder ones the narrow one omitted. Performance falling as coverage grows is
what an honest measurement looks like; rejecting it discards good arms for being measured more
thoroughly and tilts the campaign toward whatever the first small sample made look easy. Keep both
incumbent numbers: the gap between them is what the narrow sample was hiding.

Two constraints make cheap screening sound rather than merely fast, and skipping either turns a
saving into a wrong answer:

- **A screening subset is FIXED and SHARED, declared in the spec like any other unit set.** The
  paired protocol pairs by split unit; a per-arm random subset makes arms incomparable and the
  interval meaningless. Different sizes across kinds are fine. Different UNITS between two things
  being compared are not.
- **Screening runs on fit/selection units, never on the scoring set.** Every look at scoring data is
  a comparison, and enough cheap looks is selection on the score set with no record that it
  happened. The scoring units are touched at confirm, once.

**A run that exceeds its declared time budget is a defect in the sampling plan, not a fact of
nature.** Report it, resize, rerun. Waiting it out teaches nothing and costs the campaign a slot.
Where the right cutoff is depends on the effect size, the variance and the unit count, so it is
judgement rather than a constant -- but "how long will this take, and what will I do with the
answer" is asked BEFORE dispatch, and an arm timeout is declared with the lease in B1.

Stop early on a CLEAR verdict, not on impatience. An interval that still straddles the rope means
NOT ENOUGH EVIDENCE -- which is a call for more units or a better-powered design, never a rejection.
Killing a good arm because the screen was too small to see it is the failure this whole policy has
to avoid, and it is invisible UNLESS the sample was recorded: with the unit count and fingerprint in
the run, an under-powered rejection can be spotted and rerun later; without them it is
indistinguishable forever from an idea that deserved to lose.

## Phase C — build and verify the harness

**What Phase C delivers is a finished harness with a model-shaped hole in it.** By the end of this
phase an experimenter imports the harness, plugs in the model they want to try, and gets a measured
result -- without editing the loaders, the transforms, the loss, the metric, the split logic or the
scoring path. If trying a new idea requires touching any of those, Phase C is not done.

The target is a harness good enough that it does not NEED changing, not one that may never change.
Aim to build it once; expect to repair it rarely; never rebuild it per experiment.

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

When it is genuinely greenfield, the project owns real loaders, shared preprocessing, trainer,
selection decoder, evaluator with per-unit paired evidence, artifact compatibility loading, and a
lifecycle adapter consumed by campaign job. Tests generate fixtures; separate real-payload checks
execute on the target. Long work emits flushed typed progress inside the heartbeat cadence. Measure
CPU time with `resource.getrusage`, not wall time.

The harness is not complete until all of the following are present and observed:

- one source-specific reader per admitted label shape, with parsing failures and absent labels
  refused rather than converted to empty supervision;
- deterministic split construction at the declared leakage boundary, with explicit selection vs
  scoring separation and a test that catches a deliberately leaked unit;
- a simple non-learned or published-system baseline that establishes the metric and data path;
- every proposed trainable arm represented by a truthful configuration/recipe or by the one-arm
  coding-agent worker described below—no unimplemented named "families";
- a frozen selection procedure that never examines score data, calibration/abstention behavior
  where relevant, and per-source/per-unit diagnostics beside the scalar;
- a real render or inspectable artifact for prediction-vs-label failures, plus a bounded failure
  census that informs at least one seeded idea;
- a compatibility-loadable artifact and inference entry point restricted to production-legal
  inputs; and
- unit, contract, and target-real-payload tests covering the entire train → select → score path.

If existing code has a partial loader, placeholder baseline, fixture-only data path, or evaluator
that cannot score a candidate artifact, finish or replace that incomplete seam in this invocation.
Calling it "future work" is not a handoff.

### C-END — prove the whole path with a deliberately trivial model

The harness is not verified because its parts have tests. It is verified when real data has gone
all the way through it and produced a number. Do this before Phase D registers anything.

1. **Pull full representative samples through the real adapter** -- the same enumerate/decode path
   Phase A proved, over enough units to include every admitted corpus and both ends of the class
   distribution. Not fixtures, not a hand-built array.
2. **Run them through the real transforms** into the declared input shape.
3. **Plug in a deliberately trivial model.** Structurally correct and obviously incapable: the first
   two pixel values, the mean of one raw slice, a fixed constant -- anything that maps a real input
   to the DECLARED output shape and could not possibly solve the task. Use an existing project model
   instead only when the campaign is brownfield and one already serves this ontology.
4. **Assert the loss is a number you could train from.** Finite, not NaN, and it MOVES when the
   trivial model's parameters move. A loss that is constant in the parameters is not a loss, and
   finding that here costs minutes instead of a campaign.
5. **Take real optimizer steps and watch the loss fall.** Fit the trivial model to a handful of
   samples it is allowed to memorise, through the REAL training loop -- the actual batching,
   collation, optimizer, scheduler and update path a candidate will use, not a hand-rolled loop
   written for this check. The loss must fall over steps. If it does not, the defect is in the LOOP,
   not the model, and this is the only moment you can tell those apart cheaply.

   A loss that is finite and gradient-sensitive can still never be trained: a campaign once
   accumulated a whole epoch's gradient into a single update, so forty epochs took forty steps. Every
   other assertion here passed. The failure surfaced much later and read as a weak model, which is
   the most expensive way to discover a batching bug.

   This is NOT the capacity probe. Here the model is deliberately incapable and the subject under
   test is the plumbing; there the model is the real one and the subject is whether its hypothesis
   class contains the answer. Same shape, opposite question -- and running this one first means a
   later capacity failure is a real verdict on the architecture rather than a broken loop wearing an
   architecture's name.

6. **Assert the judge returns a number** on that model's predictions, through the real aggregation
   and split-unit path, over real units.
7. **Record what the trivial model scores. That is the measured answer to B0 slot 7.** If it is
   respectable under the proposed judge, the judge is not discriminating: return to B0, revise the
   metric, aggregation or unit definition, and run this again.

This also settles the output-shape contract, which is where variable-size problems break -- a frame
offering a different number of candidate objects than the last, a clip with no positive event, a
unit containing a single class. If the trivial model cannot produce a well-formed output for those,
neither will the real one.

Keep it. It is the natural home for the null arm the campaign compares against later, and re-running
it after any change to a loader, the loss, the metric or the label rule is the fastest way to notice
that one of them stopped meaning what it meant.

### Repairing the harness after it is built

A defect found later is fixed, not worked around. Agents repair the harness on the spot; that is the
correct response to a real problem and does not need permission. What separates a repair from
tampering is which of these it is:

* **Repair** -- the harness was WRONG. A metric that scores a degenerate predictor well, a loss that
  cannot descend, a loader that silently drops data, a split that leaks. Fix it in the harness.
* **Per-experiment modification** -- the harness is fine and an idea scores badly under it. Changing
  the scoring path to accommodate an idea is not an experiment, it is choosing the answer. Refused.

A repair is never free, and the cost is the part people skip: **every measurement taken under the old
harness is no longer comparable.** So a repair carries three obligations, all of them in the same
change -- say plainly what was wrong and when it started; mark the affected prior runs stale rather
than leaving them to be read as current; and re-measure the baseline and champion under the repaired
harness before adjudicating anything new against them.

One campaign repaired its metric twice mid-flight. Both repairs were correct -- the metric had been
capping a perfect predictor at 0.5 on single-class units, then rewarding a constant predictor on
them -- and both invalidated every number measured before, including a registered baseline and the
control that had been used to argue the features were worthless. The repairs were right; not
re-baselining immediately would have been the error.

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

**Register into a throwaway registry root first, then the canonical one.** The policy gate enforces
field requirements that no `--help` output reveals, and each refusal costs a round trip; taking
those round trips against the live ledger risks half-written state on the registry every other
campaign shares. Point `SPORTS_ANALYSIS_ML_REGISTRY_ROOT` (or the project's equivalent) at a temp
directory, run the whole setup path end to end, read the result back, then delete it and run the
identical command against canonical.

One real campaign hit five sequential refusals this way -- `metric.operating_point.threshold` must
be finite, `metric.scoring_corpus` must be a string, cross-corpus scope needs `metric.scoring_corpora`
instead, `runs.idea_id` is NOT NULL, and verdict/status pairs are a closed vocabulary -- and every
one was found in the sandbox for free. `knowledge/ml_registry/policy_gate.py` is the authority on
those requirements; read it rather than guessing from the CLI.

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
4. create/complete exactly one real baseline Run from exactly one complete baseline calculation,
   create its artifact, and create baseline ModelVersion plus `champion` through canonical registry
   services;
5. create/save the RegistrySpace model fact with `write_path.register_model` and record its
   `CampaignBinding` to the canonical experiment/model.

Read it all back on the target. An identical rerun writes nothing; drift refuses by named field.
Never mutate a model fact from a different metric/data contract.

## Phase E — research, document, and seed the closed nine

The exact closed set is:

- generative: `theoretical_math`, `ablation`, `supplements`, `ml_architectures`,
  `non_ml_methods`, `ce_ideate_breadth`;
- retrieval: `current_code`, `prior_trials`, `af_learn_lessons`.

`require_closed_axis` is enforced. Empty axes remain present; empty retrieval gets a receipt. Every
candidate needs `description`, `stage`, and concrete `basis`. The CLI stamps model fact id,
`origin="seeded"`, and axis.

### Brownfield starts with the incumbent's failures, not with a search

On a brownfield campaign, Phase E does NOT open with research. It opens with a diagnosis of the
model that already exists: where does it fail, on which split units, with what signature, and how
much of the metric each failure mode costs. Phase C's failure census is the instrument; if it was
not built, build it before searching.

**Diagnose by running it, then reasoning hard about what came out.** Execute the champion over real
units. Render the outputs and LOOK at them -- overlays, crops, filmstrips, whatever this project
already renders; `render it, then believe it` is a diagnosis rule before it is a reporting rule.
Compare predictions against labels case by case. Read the distribution of the loss, not its mean.
The analysis is the point; what it must be analysis OF is observed output.

**Then read the code against what you just saw.** The output tells you WHAT fails; the source tells
you WHY. Neither alone is a diagnosis. An observed failure with no mechanism leaves the fix to
guesswork -- you can group the symptom and still have no idea which of five plausible causes to
seed against. A mechanism with no observed failure is a story. Put them together and you get
something a campaign can act on: this frame is wrong, and it is wrong because THIS assumption in
the model does not hold on THIS kind of unit.

That pairing is also what makes a structural claim checkable. "A different architecture would not
have this problem" is unfalsifiable until you can name the mechanism the current one is stuck with;
once you can, the claim becomes an arm somebody can lose.

The failure this replaces is source reading INSTEAD of output -- reading the model, forming a
theory about its limitations, and seeding against the theory without ever running it. That is a
plausible story about a model rather than a measurement of one, and plausible stories are exactly
what a campaign cannot adjudicate. The order matters: observe first, then explain, because a theory
formed before looking decides what you notice afterwards.

Name each hole concretely enough to recognise it again: which units, what the model does instead of
the right thing, and what fraction of the metric it costs. A hole nobody can point at in a rendered
frame is not yet a hole, it is a suspicion -- keep it, mark it as one, and go find out.

Research is then DIRECTED at those holes, and it looks for two kinds of answer: a fix that closes
the hole in the model you have, or a different model that STRUCTURALLY CANNOT have it. Both are
legitimate outcomes of the same directed search, and the second is not a failure of the first --
sometimes the honest reading of a census is that the incumbent's architecture guarantees the failure
mode and no amount of patching removes it.

A round that returns citations unrelated to any measured failure has answered a question nobody
asked -- it is the brownfield equivalent of rebuilding something the archive already held. State
each hole, then say what would fill it.

Two things this must not become:

- **A refinement-only backlog.** Directed research biases toward patching the incumbent, and
  sometimes the incumbent is the problem. A wholly different approach stays admissible and belongs
  in the exclusive tier alongside the patches -- but it needs a stated reason to believe it wins
  BY A LOT, grounded in the census. "Different" is not a reason; "the census says 60% of the loss is
  a failure mode this approach structurally cannot have" is.
- **A census of symptoms.** Group failures by cause, not by appearance. Two units failing for the
  same reason are one hole, and ten cosmetic variants of one hole will consume a campaign.

Greenfield skips this and starts at round 1 below.

### Research in rounds, because the rounds answer different questions

Run these as distinguishable rounds and record each one's outcome separately. Collapsing them into
a single search produces a pile of citations rather than a decision.

1. **Has this project already built one?** Search the production tree, the registry's artifacts and
   champions, prior campaign reports and ranked follow-ups, and -- the one that gets skipped --
   **purged and archived code**: `git log --diff-filter=D`, `ml_archive`, `legacy`, anything a
   cleanup removed. A model deleted in a tidy-up is still a built model, and the real-footage
   measurement recorded beside it is frequently the most valuable artifact in the repository. This
   round is first because it is the cheapest and the most often skipped.
2. **Has anyone else built one you can run?** External code or weights that execute end to end on
   this ontology.
3. **Is there a published claim without runnable code?** Papers asserting a solution but shipping
   no usable implementation. Real candidates, carrying reimplementation risk; say so rather than
   treating them as equivalent to something you can run.
4. **What are the genuinely different general approaches?** The wide axis. Not variations -- the
   handful of fundamentally different ways this problem gets attacked.

**Anything found in rounds 1 or 2 becomes an ARM, not a citation.** A pre-existing model -- yours or
somebody else's -- is a candidate to run through the harness and measure against, on exactly the
same footing as a novel hypothesis. Recording it in the plan and then inventing something instead is
the failure this round exists to stop.

A campaign that skipped round 1 rebuilt a ball detector from colour statistics while its own archive
held one measured at 84.6% recall / 95.5% precision -- along with the recorded finding that the
colour signal it was rebuilding on had been measured HARMFUL on that footage. The cost was not the
wasted build; it was a baseline, a control and a seeded backlog all committed to the wrong approach
before anyone looked.

Then cover four strands. The first two decide the approach; the last two build on whichever
approach wins, and are seeded as additive with a dependency rather than raced against it:

1. published end-to-end systems, rig/metric, and runnable checkpoint—or reasoned negative
   *(exclusive tier)*;
2. pretrained candidates and the corpus/regime each actually saw *(exclusive tier)*;
3. augmentations derived from measured target resolution, optics, visibility, and leakage groups
   *(additive tier)*;
4. Karpathy-loop seam: allowed code/axes/budget and immutable judge, or the decision not to run it
   *(additive tier)*.

Researching strand 3 early is fine and often cheap -- an augmentation derived from the RIG rather
than the model is true whatever the model turns out to be, and "do not mirror, because side cues
carry signal" does not stop being true when the architecture changes. What is not fine is letting
it compete for budget with the approach decision. Research it when you notice it; seed it in E1
only if it passes the independence test, and in E2 otherwise.

### Seed a SEQUENCE, not a list

Campaign ideas are not peers. Some are **decisions**: the general approach, the model family, the
problem decomposition -- only one can win, and they are tried in isolation against each other.
Others are **additions**: augmentations, mathematical heads and injections, sampling and weighting,
then fine tuning.

**The round a candidate is seeded in carries that distinction, so no idea tracks a dependency.**
Everything seeded in E1 is dispatchable the moment it is written -- exclusive candidates race each
other, and E1's additives are independent by the test above. Everything seeded in E2 applies to the
winner, which by then exists. There is no partial order to encode and no `depends_on` to populate,
because an idea that would need one is an idea E2 has not been written yet.

If a candidate seems to require a prerequisite, that is the signal it belongs in E2 and has been
drafted too early. Do not plan it, do not seed it with a forward reference, and do not invent a
dependency field to hold it -- write it after the thing it depends on has won, when you can write
it against something real.

### Decomposition is a candidate, and often the winning one

When the census says the incumbent's biggest problem is that it is DOING TOO MUCH -- one model
carrying two problems whose failures have nothing in common -- splitting it is a first-class
candidate in the exclusive tier, not a refactor to propose later. Seed it. It is frequently the
answer, and it is systematically under-proposed because it looks like architecture work rather than
a hypothesis.

Court fitting is the worked example: one model asked to find the paint AND fit the court fails at
both, and the split -- a paint detector, then a fitter that consumes its output -- makes each half
separately measurable and separately improvable.

A natural break point has three properties. Check all three before seeding it, because a split at
the wrong seam costs two campaigns and buys nothing:

1. **The halves fail differently.** If both halves fail on the same units for the same reason, the
   seam is imaginary and you have split one problem into two copies of itself.
2. **Each half is measurable alone.** There is a judge for the upstream half that does not require
   the downstream half to exist. If the only way to score the first stage is to run the second, it
   is not a stage, it is an internal layer.
3. **The seam is a contract, not a tensor.** What crosses it is a canonical record another model
   could produce instead -- which is exactly what makes the upstream half replaceable by ground
   truth labels while the downstream half is developed.

**A split creates a MODEL, so it creates a CAMPAIGN.** One campaign per model is not suspended
here: the new stage gets its own campaign number and its own frozen judge, and the existing campaign
narrows to what remains. It does NOT become a second model inside this campaign. The downstream
campaign then declares the upstream as an input -- `source: labels` while the upstream is still
being built, `source: model: <registered>@<alias>` once it has a champion -- and because both
resolve to the same canonical record, the downstream campaign can start before the upstream one
finishes.

That last point is what makes the split cheap enough to be worth trying: it does not serialize the
work. Seed the decomposition, and if it wins the exclusive tier, open the sibling campaign rather
than growing this one.

**A campaign may take this pivot ONCE.** Decomposition is admissible exactly one time per campaign,
and the campaign that took it may not take it again. A campaign allowed to keep splitting never
converges: every split defers the measurement that would have told you whether the last one helped,
and the backlog grows a level of indirection per round while the metric stays unmeasured. One split,
then run.

The narrowed campaign inherits that spent allowance -- it has already been decomposed and must now
produce a model. The NEW sibling campaign starts with its own unspent one, because it is a different
model with a different judge and its own census may find a real seam. That is not a loophole: each
model gets one look at whether it is doing too much, which is the number of looks the question
deserves.

If a second seam is genuinely there, it will still be there after the first split has been measured,
and by then you will know from evidence rather than from architecture taste whether it matters.

**The sibling campaign is greenfield in REGIME and brownfield in EVIDENCE.** No model exists for the
sub-problem, so it runs Phase 0 through G properly: its own frozen judge, its own harness, its own
registered model, its own closed nine. But it does not start from a blank search -- the parent's
census is what justified the split, so round 1 arrives already answered. Carry it across rather than
re-deriving it.

**Its baseline is the parent's IMPLICIT behaviour, extracted.** Before the split the parent was
already doing that sub-task somehow -- badly, internally, unmeasured. Lift that out and register it
as the sibling's incumbent. This is the rule that makes a decomposition falsifiable: if a dedicated
model cannot beat what the monolith was already doing by accident, the seam was wrong, and one arm
tells you so. A sibling that registers a trivial or refusing baseline instead has skipped the single
comparison that would have caught a bad split, and will spend a campaign discovering it.

**The parent does NOT narrow its judge.** It is tempting to rescore the parent on only what it still
does, and it destroys the campaign's own record: every historical run becomes incomparable and the
incumbent it was ratcheting against stops meaning anything. The parent keeps its end-to-end metric,
because that is the outcome anyone actually wants; the sibling gets a stage-level one. What the
parent gains is not a smaller problem but a declared input.

That combination is what the input layer is for, and it yields two honest numbers instead of one
ambiguous one. The parent measured with `source: labels` isolates its own contribution with a
perfect upstream. Measured with `source: model: <sibling>@champion` it gives the composed,
shippable number. The gap between them is the error the upstream contributes -- previously
unmeasurable, and usually the number that tells you whether to keep investing in the sibling. Note
that the two runs are correctly NOT comparable to each other: they were fed different inputs, and
adjudication refuses to pit them against one another.

**The pivot is not finished when the sibling has a champion. It is finished when the parent has been
resumed on it.** A split that ends with two campaigns and no composed measurement has spent two
budgets to learn nothing: the sibling's stage-level number says it beat the parent's implicit
behaviour at the SUB-task, which is not the question anybody asked. Close the loop explicitly:

1. The sibling campaign reaches a champion and finalizes.
2. The parent flips its declared input from `source: labels` to
   `source: model: <sibling>@champion`. One line; no code change, because both resolve to the same
   canonical record.
3. The parent RE-MEASURES end to end.
4. **That number against the parent's pre-split incumbent is the verdict on the decomposition** --
   the only comparison that answers whether splitting was right. Record it as the pivot's outcome,
   not as a routine arm.
5. The parent then resumes its normal loop with the input in place.

Step 4 needs care, and it is the one place the split genuinely costs something. The parent's pre-split
runs were fed by its own internal sub-step; the composed run is fed by the sibling. Different inputs,
so the paired machinery will refuse to compare them -- correctly. This comparison is therefore a
deliberate, one-time RE-BASELINE, judged as an adoption decision and recorded with the input change
named, never slipped through as an ordinary ratchet step. Anyone reading the campaign later must be
able to see that the incumbent changed shape here and why.

If the composed number does NOT beat the pre-split incumbent, the decomposition failed. Say so, keep
both records, and let the parent revert to its pre-split incumbent. The sibling is not thereby
worthless -- it is a measured model of a real sub-problem, and it stays registered -- but the
composition does not ship. A pivot that cannot fail is not a hypothesis, and this is the step where
it is allowed to.

### Seed the approach now; seed its refinements once it has won

Seeding happens in two rounds, and the second is not optional.

**E1, before any arm runs.** Every exclusive candidate -- prior in-project models, external runnable
solutions, published claims, wide-axis approaches -- plus additives that are TRULY approach
independent. The closed nine applies here in full; what changes is that the content is
approach-level. `ablation` in E1 means dropping a whole component of an approach, not masking one
feature of one head.

**E2, once the exclusive tier is settled.** The approach-specific refinements, generated from the
winner's MEASURED failure census rather than from imagination. This is where Phase C's failure
census earns its keep: a refinement backlog written against observed failures of the thing that
actually won beats one written in advance against a thing that lost.

**E2 is a CYCLE, not a round.** Once the direction is settled the campaign keeps turning:

1. **Census the current champion.** Run a `diagnose` measurement over units chosen for coverage of
   failure modes. Group by cause, attribute each mode's share of the metric, and rank by what it
   costs.
2. **Take the top mode and find out why**, output beside source, as in the brownfield diagnosis.
3. **Research a fix for THAT mode.** Search externally -- this is encouraged, not a fallback.
   Somebody has usually met this failure before, and a published fix that transfers is worth more
   than an invented one that might.
4. **Seed it, run it, adjudicate it** through the normal one-arm lifecycle.
5. **If it is adopted, go back to step 1 -- and re-census.** Do not carry the old ranking forward.

That last point is the whole reason this is a loop and not a longer list. **Adopting an arm changes
the failure distribution.** The mode that ranked second before a fix is frequently not the mode that
ranks first after it: the fix may have removed both, or exposed one that was previously masked, or
made a rare mode dominant by shrinking everything around it. A backlog ranked once and worked
top-to-bottom is optimising against a model that no longer exists after its first adoption.

A rejected arm does NOT trigger a re-census -- nothing changed, so the ranking still holds. Take the
next hypothesis for the same mode, or the next mode down.

**Stopping: keep cycling until the improvements go marginal, and read that off the census.**

Step 1 already gives you the stopping number. **The top remaining mode's share of the metric is a
CEILING on what the next cycle can win** -- if the biggest failure mode costs 2% of the metric, a
perfect fix for it wins 2%, and every imperfect one wins less. So marginality is not a feeling about
recent results; it is a quantity computed before the cycle starts.

Stop when that ceiling drops below what the campaign declared it cares about -- the rope is the
natural threshold, since it is already the campaign's own statement of what difference is worth
having. A cycle whose best possible outcome lands inside the rope cannot produce a result the judge
would call a win, and running it is spending budget to learn something you could have read off the
census.

Two supporting signals, neither sufficient alone:

- **adoptions clustering at the rope's edge** -- still winning, no longer winning anything that
  matters;
- **cost per unit of gain rising across cycles** -- the cheap modes are gone.

A run of cycles with no adoption is NOT by itself a stop. It may mean the hypotheses were weak while
the mode is still expensive and still worth attacking; the census says which. That distinction is
the difference between a campaign that finished and one that gave up.

Whatever ends it, report the final census with the verdict. "The remaining failures are these, they
cost this much, and they were not worth the next arm" is a finding a successor can act on -- and it
tells them exactly where to start if the cost calculus ever changes. "It stopped improving" is not.

### The independence test, because this is where the split fails

An additive idea belongs in E1 only if it would survive a different approach winning, **unedited**.
Apply it literally: if a completely different decomposition won tomorrow, could this idea be
dispatched exactly as written? If it has to be rewritten, or it names a component that only one
approach has, it is approach dependent and belongs in E2.

Independent, and safe to seed early:

- augmentations derived from the RIG -- resolution, optics, visibility, leakage groups. "Do not
  mirror, because side cues carry signal" is true whatever the architecture is;
- additional corpora and data supplements -- wiring another labelled source does not depend on the
  model that will consume it;
- supervision and label-rule variants -- how the labels are DERIVED is upstream of what consumes
  them.

Dependent, and belongs in E2:

- anything naming a feature, a layer, a head or a loss term of one candidate approach;
- ablations of a specific representation, which cease to mean anything when the representation
  changes;
- tuning of a hyperparameter that only one approach has.

A campaign that ignored this seeded thirty ideas of which roughly twenty-four named parts of one
head. Replacing that head killed all twenty-four at once, and every one of them had read as
perfectly sensible when written.

**Do not spend the budget tuning a losing approach.** An ablation on a head that a different
decomposition is about to replace is wasted, and the waste is invisible because the ablation runs
perfectly well. Settle the exclusive tier first.

The number of tiers and their names follow the evidence -- do not force a fixed taxonomy. What must
be explicit is which ideas are exclusive commitments tried alone, and which are built on top of a
decision already made.

For every strand, preserve the source, exact applicability, regime mismatch, and disposition in
the campaign plan or seed receipt. Research must drive decisions, not merely populate citations:

- Test or faithfully reproduce an accessible near-solution when it can be evaluated through the
  frozen harness; otherwise document the precise incompatibility (input mismatch, unavailable
  weights, invalid metric, or target-regime mismatch).
- Include data-centric hypotheses—additional admissible sources, label normalization, sampling,
  source weighting, augmentation, and domain-shift controls—alongside model and mathematical
  hypotheses.
- For `theoretical_math`, identify the loss/objective/constraint and its expected failure mode;
  do not seed formula-shaped prose with no executable interpretation.
- For `current_code` and `prior_trials`, retrieve actual files, run records, reports, and failure
  artifacts. For `af_learn_lessons`, retain an empty receipt only after the real search.
- Every idea names the admitted corpora it touches, production-legal inputs it requires, frozen
  stage it changes, and either its executable recipe or coding-agent work item.
- Every idea names the round it belongs to. An E1 candidate must be dispatchable as written, with
  no prerequisite of any kind; a candidate that needs one is deferred to E2 rather than seeded with
  a forward reference.

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

**Take a host-level lane slot, not just a portfolio slot.** A portfolio's `max_active` governs only
its own campaigns; it cannot see another session, another checkout, or a human running something by
hand on the same box. Those are exactly what contend in practice. If the project already has a
cross-process slot mechanism for accelerators -- an `flock` semaphore over N slots is the usual
shape -- extend the same mechanism to the CPU lane rather than inventing a second one. A campaign
job then blocks until a lane is free instead of quietly halving everyone's throughput.

Before the first measurement, record what else is running on the host and its load, and treat that
as part of the target evidence. A baseline established while another campaign was mid-sweep is not
a baseline; it is a number taken under conditions no later run will reproduce.


1. Push the reviewed revision, update the target checkout without overwriting target work, and prove
   clean HEAD equality.
2. Install from the lockfile under the service account/environment that will own the process.
3. Resolve durable adapter data and credentials, cache only bounded smoke inputs, and record target
   free disk before/after.
4. Materialize target-native registry, RegistrySpace, state/cache roots, resource lease,
   `operator.json`, portfolio/campaign/capacity manifests, and campaign-job config. Never copy
   laptop absolute paths into them.
5. Run target preflight, the single complete incumbent calculation, compatibility load, a bounded
   campaign-job plumbing smoke that does not repeat the full baseline, and **one bounded
   non-incumbent research arm from claim through artifact and score**
   through the exact long-run worker. Audit its outputs before the portfolio proof: inspect the
   report/artifact schema and compatibility load, prediction-vs-label renders, per-source/per-unit
   metrics, calibration/abstention, errors/refusals, resource use, heartbeat, lifecycle records,
   and operator visibility. Repair every defect the audit exposes and rerun the affected proof;
   a failed or suspicious research arm is a setup defect, not a handoff note. Then run the
   portfolio one-shot. Verify one new Run per proof, heartbeat, artifact checksum, process cleanup,
   typed outcome, external adjudication, and idempotent restart position. A
   baseline-only seed-smoke adapter is not the long supervisor and cannot satisfy this item. A worker that only
   opens a worktree or writes a plan is not a candidate dispatch.
6. Before handoff, run `python -m knowledge.ml_registry.runtime.campaign_job --help` and
   `python -m knowledge.ml_registry.cli.portfolio --help` in the target environment, then inspect the
   target operator config and portfolio child command. Refuse READY if either resolves a legacy
   shell driver, a removed registry verb, a different checkout, or a baseline-only adapter.
7. Prepare the detached portfolio start command and ownership-aware observe/tail/stop commands. Do
   not start unattended supervision from this skill.

Copy local registry files only if embedded repo paths, artifact URIs, device evidence, and event
history remain valid on target; normally rebuild deterministic seed state and remeasure there.

## Phase G — report, then continue into the campaign

READY is a checkpoint, not an exit. Report the evidence, then carry straight on into the campaign
loop in the same job: establish the measured baseline, settle the exclusive tier, then diagnose and
refine. Hand back to a human only on a blocker from the list above, or when the campaign has
genuinely finished.

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

Do not offer a retired shell driver as an alternative start command. The handoff is incomplete
unless the portfolio controller was observed spawning the configured canonical campaign job on the
execution target and its typed outcome was read back through the portfolio status command.

When the executable IDEA frontier uses a coding-agent worker, the portfolio one-shot remains setup
evidence; its baseline-only smoke command is not the long-run start command. Generate a target-native
supervisor prompt/config and hand off a detached `codex exec` (or the target's equivalent coding
agent) that explicitly reads `/af-ml-supervise`, the canonical binding, and the frozen project
contract. Preflight the executable and authentication, and prove one candidate dispatch before
marking that handoff READY.

## Scope guides; it never blocks

Every phase names the paths it writes, and that is a statement about FOCUS, not a fence. **No
campaign is ever blocked by something outside its scope.** If the point of failure is an upstream
campaign, a shared model, an adapter, the harness, or anything else this campaign does not own, it
is allowed -- and expected -- to go fix that thing rather than stop, file a note, or work around it.
A campaign that reports "blocked on someone else's model" when it could have repaired the model has
chosen the more expensive outcome.

Two kinds of fix, and only the second needs ceremony:

- **A defect.** The dependency does not do what it already claimed. Fix it wherever it lives, land
  it in its own commit, and name the campaign that found it. No adjudication, no permission: making
  code match its own contract is never a hypothesis.
- **An improvement.** The dependency does what it claimed, and this campaign needs it to do BETTER.
  That is a change to a measured model, so it belongs to the owning campaign's judge -- and the
  blocked campaign **runs it there itself**. Author the fix as an arm in the upstream campaign and
  dispatch that ONE arm through the upstream's own harness, judge, corpora, split units and seeds.
  The one-arm lifecycle is already the unit of work; nothing needs restarting, and no supervisor
  needs waking. Record the Run against the upstream experiment, let Praxis adjudicate it as it would
  any other arm, and carry on.

  Run the arm that unblocks you, not a campaign's worth of them. You are borrowing the upstream's
  judge to settle one question, not taking over its backlog -- its seeded ideas remain its own to
  dispatch. If unblocking appears to need several arms, that is the signal it is not a fix but a
  campaign, and it belongs to the upstream's own run.

  What is forbidden is moving another campaign's `champion` on YOUR metric. Through the upstream's
  frozen judge the promotion is legitimate and its ratchet holds; scored on the downstream's metric
  it is meaningless, and the next person to read that campaign cannot tell what its numbers mean any
  more. The judge you use is what makes the difference, not who is running.

**And you do not wait for either.** While the upstream fix is being adjudicated, the downstream
campaign proceeds on `source: labels`. That is the whole reason the labels source is permanently
legal rather than a migration state: it decouples a downstream campaign's schedule from an upstream
campaign's verdicts. Measure against labels now, flip to the champion when it lands, and record both
-- the gap between them was going to be the interesting number anyway.

The one thing that IS forbidden is silence. A fix outside scope is committed separately, named as
such, and reported with the campaign that prompted it. Scope stops being a useful signal the moment
work drifts across it unremarked.

## Never

- Never declare READY on a different host from execution.
- Never inspect only a sample of the data catalog. Every catalogued corpus gets a recorded,
  evidence-backed campaign disposition, and every genuinely useful admissible source is wired.
- Never leave an adapter, data manifest, loader, baseline, evaluator, real-payload test, or
  executable arm as routine follow-up work for the human while claiming the campaign is seeded.
- Never treat a research-arm run as a smoke-test checkbox: inspect its artifacts and outputs, fix
  material defects, and rerun the relevant proof before handing off the long campaign.
- Never use a label available only in a corpus as a production inference input, or claim
  cross-sport/cross-rig validity without an independent scoring set in that regime.
- Never count a bucket prefix, upload, or staging tree as adapter-proven data.
- Never sync a dirty tree or use an unpushed revision as remote provenance.
- Never copy laptop paths, throughput, hardware fingerprints, or baseline evidence into remote
  state.
- Never calculate the complete baseline more than once or register multiple baseline Runs. A
  campaign-job plumbing smoke uses a bounded real slice and remains explicitly non-baseline.
- Never run a GPU lease on a CPU host or silently change the resource declaration.
- Never let candidate code read sealed labels, edit the judge, self-report a verdict, or move an
  alias.
- Never create a parallel live ledger/registry or revive pre-cutover lifecycle commands.
- Never seed an unbound/legacy model fact, omit an empty receipt, invent a tenth axis, or pad stages.
- Never call an IDEA executable merely because it has a citation or prose basis; it must be
  dispatchable through the real trainer/evaluator or the preflighted one-arm worker.
- Never stop at READY to ask permission to continue; READY is a checkpoint inside one job. Stop for
  a blocker, never for a phase boundary.
- Never trust a stored phase marker over the artifacts on the target; derive state from evidence so
  a killed session, a manual fix, or a hand-made change cannot desynchronise it.
