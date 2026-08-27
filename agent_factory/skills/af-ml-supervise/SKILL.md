---
name: af-ml-supervise
description: Drive one registered model's campaign to a verified production alias by dispatching one commit-backed run at a time, adjudicating canonical registry measurements, advancing or rolling back champion, and finalizing only after coverage and compatibility pass. Use for an already bootstrapped campaign with project-owned training and a curated IDEA backlog.
---

# af-ml-supervise

Supervise one experiment for one registered model. Work one IDEA at a time, let the project-owned
trainer produce measurements and immutable artifacts, adjudicate those measurements outside the
candidate code, and close only when `finalize` has verified and moved the model's `production`
alias.

The standard registry is the canonical evidence store:

- SQLite projections: `experiments`, `runs`, `artifacts`, `registered_models`, `model_versions`,
  `lineage`, `aliases`, and `events`;
- content-addressed blobs for immutable artifact bytes;
- an append-only event log written before its SQLite projections;
- Praxis RegistrySpace for IDEA inventory, citations, claims, stages, dependencies, verdict
  memory, and ratchet requeue state.

The registry stores bytes, hashes, and pointers. Git stores code. Do not add registry flavors,
embedded source archives, or a second campaign ledger.

This is a deliberate cutover from the pre-registry lifecycle. Historical tabular evidence is
accepted only through the explicit historical importer and can be reproduced through `export-runs`;
neither an export nor an old shell loop is live authority. Do not translate old operator commands
mechanically: the public commands below and the controller-owned campaign job are the supported
surfaces.

## Boundaries

- This is not a trainer. The project owns corpus loading, preprocessing, training, evaluation,
  and compatibility loading.
- This is not campaign setup or idea generation. If the experiment, registered model, baseline,
  frozen metric, or IDEA inventory is missing, use `af-seed-ml-supervise` through the portfolio
  campaign job. Do not improvise setup while supervising.
- This is not `af-ml-model`; that is a different research loop.
- This is not the portfolio controller. A portfolio may supervise at most two campaigns, while
  each invocation here owns exactly one campaign and exactly one in-flight run.
- Never infer a verdict from candidate-authored output. Praxis adjudication services are the only
  writers of run verdicts; adjudication and ratchet are the only writers of `champion`.
  `finalize` is the only writer of `production`.

## Preconditions

Before dispatching, verify all of these from canonical state and the project-owned CampaignSpec:

1. The experiment exists and freezes its ordered stages, scalar metric, direction, numeric win
   condition, paired adjudication protocol (shared seeds, split unit, bootstrap resamples and
   confidence level), baseline throughput, and spec digest.
2. The registered model exists and names its family, scope, axis, production protocol, and any
   declared historical extension.
3. `champion` resolves to an active model version whose content-addressed artifact verifies and
   whose run belongs to this experiment.
4. The current checkout is the champion code SHA or an explicitly reconciled descendant. Every
   run will carry a complete `code_ref` with repository, SHA, base SHA, diff hash, and diff lines.
5. The CampaignSpec names the trainer, evaluator, compatibility loader, resource lease, isolation
   roots, timeout, heartbeat cadence, stages, and metric policy.
6. The ready IDEA set is nonempty or canonical completeness explains why no dispatch is needed.
   An empty eligible queue is never itself proof of completion.
7. No unresolved run already exists for the selected IDEA. `running` and `complete` both mean the
   question is still in flight.

The registry refuses a second in-flight Run for the same `(experiment, idea)` and records a
`trial_refused` event. Do not catch that refusal and launch anyway.

Refuse with the named precondition. Do not seed, train, or mutate another campaign to make a
preflight pass.

## One-arm lifecycle

Repeat this sequence serially:

1. Read the campaign view joining RegistrySpace IDEA fact IDs to registry `runs.idea_id`. Display
   labels are not join keys.
2. Select the next eligible IDEA from the open stage. Claim it with the campaign job's owner and
   lease duration.
3. Create branch/worktree `arm/<experiment>/<idea>` from the current champion SHA. The arm is this
   commit lineage, not a permanently installed module or a configuration switch.
4. Commit the candidate before launch. Compute `code_ref` from Git and create one `running` Run.
   Run creation cannot write measurements or a verdict.
5. Launch the project trainer inside the controller-owned process group and resource lease.
   Heartbeat the IDEA claim only while the verified child process is alive. Stream typed progress.
6. The trainer completes the Run with typed metrics and writes immutable artifacts through the
   registry. Completion changes the Run to `complete`; it does not answer the IDEA.
7. Adjudicate against the current champion using registry measurements. The adjudicator writes the
   verdict and terminal status. Candidate code never supplies either.
8. Apply the verdict's Git and alias outcome exactly, then refresh stage coverage, diagnoses, and
   the ancestry-aware ratchet before choosing another IDEA.

The public registry surface is discoverable with:

```sh
python -m knowledge.ml_registry.cli --help
python -m knowledge.ml_registry.cli create-run --help
python -m knowledge.ml_registry.cli complete-run --help
python -m knowledge.ml_registry.cli adjudicate-run --help
python -m knowledge.ml_registry.cli registry-status --help
python -m knowledge.ml_registry.cli finalize --help
```

Use project-owned adapters or the campaign job to supply their JSON payloads. Do not rebuild
business rules in shell.

IDEA claim operations remain on the RegistrySpace bridge. Use `claim-idea` before launch and
`heartbeat-idea-claim` with the exact same owner while the child is alive. Use `registry-status`
and the campaign view for Run state; never infer it from an IDEA display tag.

## Spend the smallest measurement that answers the question

An arm dispatched at full corpus scale by default is how a campaign spends a day proving a bad idea
is bad. Before step 5 launches anything, decide which question this dispatch is answering and size
it accordingly -- the spec's B2 block declares the kinds and their unit sets:

- **screen** -- is this worth more compute? The smallest set that could show an effect worth acting
  on, on fit/selection units.
- **confirm** -- does it beat the champion? The declared judge in full, at or above the minimum
  effective sample. This is the expensive one; it runs once, on an arm that already survived a
  screen.
- **diagnose** -- where does it fail? Sized for coverage of failure modes, deliberately skewed to
  hard units, not powered for statistics. Its output is a census, not a verdict.
- **regress** -- did we break something? A small fixed set, run often.

Those four are vocabulary for the record, not a quota to hit. How many units it takes to show an
effect depends on the effect size, the variance and the units, and the dispatcher can see all three.
Choose the smallest sample that answers the question, escalate when it does not.

**Recording it is not optional.** Every Run carries, beside its typed metrics: the measurement kind,
the number of split units AND a fingerprint of which ones, and the cost in CPU time and wall clock.
The fingerprint is the load-bearing part -- `n=40` twice over different units is not a repeat
measurement, and nothing else distinguishes them. Without this the campaign cannot say why one arm
took four minutes and another four hours, cannot tell an under-powered rejection from a real one,
and cannot accumulate any evidence about how much data its own questions need.

**An arm earns a bigger sample by surviving a smaller one.** Escalating on survival is cheap; an arm
that dies in screening cost minutes, and the record shows exactly what it was screened against.

Two things a cheap screen must not do, because either silently invalidates everything downstream:

- **Never screen two arms on different units.** The paired protocol pairs by split unit; a per-arm
  subset makes the interval meaningless while still printing one. The screening set is fixed, shared
  and declared -- if it is not in the spec, it is not a screen, it is an anecdote.
- **Never screen against the scoring set.** Every look is a comparison. Enough cheap looks is
  selection on the score units with nothing in the record saying so, and the confirm that follows is
  then measuring a choice it helped make. Scoring units are touched at confirm, once.

A run that exceeds its declared arm timeout is a **defect in the sampling plan**, not a fact of
nature: report it, resize, redispatch. Waiting out a multi-hour measurement teaches nothing and
holds a lane the rest of the campaign needs. Where the cutoff sits depends on effect size, variance
and unit count, so it is judgement -- but "how long will this take, and what will I do with the
answer" is answered before dispatch, not discovered during it.

Stop early on a CLEAR verdict, never on impatience. An interval still straddling the rope means NOT
ENOUGH EVIDENCE, which calls for more units or a better-powered design. Rejecting there kills a good
arm for being under-measured, and nothing in the record afterwards distinguishes that from an idea
that deserved to lose.

## Typed measurements and verdicts

A completed Run records:

- metric and explicit validity;
- throughput and a comparable throughput unit;
- memory;
- CPU time;
- start and end one-minute load;
- device fingerprint on the Run itself.

Adjudication compares the candidate to the model version currently named by `champion`:

```text
invalid measurement                                      -> voided
incomparable throughput units                            -> refuse adjudication
throughput below the frozen experiment floor             -> voided
paired 95% CI of the delta lies entirely above zero      -> adopted
paired 95% CI of the delta crosses zero                  -> parked
paired 95% CI of the delta lies entirely below zero      -> rejected
```

**Adjudicate on a PAIRED interval, never on the range or spread of independent baseline repeats.**
Evaluate candidate and champion over the SAME inputs with the SAME seeds, compare the per-unit
DIFFERENCE, and take a paired bootstrap confidence interval over the split unit. Most of the
variance between independent repeats is common-mode -- some draws are simply harder -- and pairing
removes it, which is what makes a real improvement detectable at all.

**Impose no effect-size floor.** A candidate whose interval clears zero is adopted however small
the point estimate. Effect size governs whether to BUILD something, not whether to USE something
that already exists and measures positive; when adoption is a parameter change with no new code,
weights, or inference cost, refusing a real gain buys nothing. Small gains also compound, and a
floor applied per candidate means none of them ever accumulate. Report the point estimate and the
interval for EVERY candidate regardless of verdict, and where several are adopted, report their
COMBINED effect too -- each was measured against the champion alone and they may not simply add.

Do NOT use max-minus-min of N repeats as a threshold. Range is not an uncertainty measure: it is
inflated relative to the spread and it GROWS as repeats are added, so the test gets stricter as
evidence accumulates, which is backwards. Measured on a real campaign, a five-repeat range was
2.40x the standard deviation and rejected a genuine +1.05% improvement as noise. If a legacy
noise-floor value is recorded on the Experiment, report the interval verdict beside the old
floor-based verdict so a change of method stays auditable rather than reading as a moved goalpost.

Keep metric, direction, units, split, and fairness context frozen for the comparison. Invalidity is
evaluated before throughput, so an invalid and slow Run remains invalid evidence rather than being
misreported as a speed loss. The throughput floor is frozen on the Experiment; an operator cannot
disable or rewrite it while adjudicating a candidate.

Record the regime each interval was measured under -- degradation settings, seeds, corpus mix --
as bookkeeping, not as a bar. A small effect is more fragile to a regime change than a large one,
so when the regime changes the small adopted gains are the ones to re-check first.

Run status and scientific verdict are orthogonal and use only these pairs:

| Run status | Verdict | Meaning |
|---|---|---|
| `running` | none | trainer is active |
| `complete` | none | measurements await external adjudication |
| `succeeded` | `adopted` | fair win |
| `succeeded` | `rejected` | fair loss |
| `succeeded` | `parked` | fair but inconclusive |
| `voided` | `voided` | unfair measurement; IDEA remains retryable |
| `failed` | none | execution failure; IDEA remains retryable |
| `superseded` | none | interrupted, orphaned, or invalidated execution |

Never call a failed or voided run a rejection. Never count `running`, `complete`, `failed`,
`voided`, or `superseded` as a fair measurement.

## Verdict effects: Git and aliases move together

### Adopted

- Create the immutable ModelVersion and move `champion` in the same adjudication event that marks
  the Run `succeeded/adopted`.
- The ModelVersion pins artifact checksum, family version, run, code SHA, preprocessing hash,
  calibration, thresholds, and compatibility evidence.
- Merge the arm commit into the campaign branch. In that merge, delete the implementation or
  diagnostic it supersedes. Make an adopted configuration the default and remove its toggle.
- Keep one implementation in the working tree. Adoption does not copy code into a parallel
  production file; the alias move is the promotion seam.

### Rejected or parked

- Mark the Run `succeeded` with the external verdict.
- Create lightweight tag `runs/<run_id>` at the exact candidate SHA.
- Delete the arm branch/worktree and remove its implementation, diagnostics, and configuration
  toggle from the tree. The tag and registry evidence are the retrieval path.
- Record the reason. A park means no directional evidence, not a soft rejection.

### Voided

- Mark the Run and verdict `voided`, diagnose the fairness failure, and keep the branch only until
  the rerun resolves the IDEA.
- Do not advance stage coverage. A void is a decision to rerun, not an answer.

### Failed or superseded

- Record the execution or interruption reason and prove the owned process group is dead before a
  retry.
- Reconcile the launch intent, process record, terminal state, and registry before creating a new
  Run. Never guess that a missing wrapper means its child died.

No branch, arm-only module, toggle, or diagnostic may outlive a settling verdict (`adopted`,
`rejected`, or `parked`). Voided and failed attempts retain the candidate only while a reconciled
rerun is outstanding. Before removing candidate code, verify the `runs/<run_id>` tag resolves to
the Run's `code_ref.sha`.

Every source deletion in these verdict commits still goes through `af-clean`: a located finding,
the matching change class, tier-2 execution witnesses, blind verification, and witnessed apply.
Adjudication authorizes the lifecycle outcome; it does not bypass deletion evidence.

## Stages and IDEA dependencies

Stages order questions. `depends_on` remains an IDEA-level scientific dependency:

- a stage opens only after every IDEA in earlier stages is answered or explicitly unreachable;
- `depends_on` unlocks only when each named IDEA was adopted;
- when a dependency receives a terminal non-adoption, its dependents become unreachable to a
  fixpoint;
- a void, failure, supersession, or unresolved Run does not settle either a stage or a dependency;
- exclusions and out-of-scope dispositions must be explicit and reported, never silently filtered.

A stage can close without testing enough candidates, so completion also requires minimum measured
coverage. Count only the latest fair terminal Run produced by each IDEA, exclude incumbent no-op
remeasurements, and report every untried model family with its reason.

Use the canonical staging services rather than recreating them: `next_queue` raises `StagingStuck`
when an open stage has unanswered items but no eligible queue; `stage_coverage` and `thin_stages`
measure whether each stage earned closure. Unless the CampaignSpec deliberately sets a stricter
policy, fewer than three fair measured IDEAs is thin. The answered set includes explicit exclusions,
out-of-scope dispositions, and the fixpoint of unreachable dependencies; none may be silently
filtered.

A prior loss transfers only when its scientific argument survives the current champion, metric,
representation, and data. Enumerate the material model families before opening an architecture
stage. Several parameter variants of one family do not cover several families.

"Answered" comes from the latest canonical Run, never from an IDEA metadata status. Reading IDEA
status instead of joined Run state requeues already-settled work indefinitely. The campaign view's
latest Run per IDEA is the only input to measured, answered, and retryable predicates.

## Fairness and resource validity

Budget training on CPU time, not wall time. Record load and device identity with every Run.
Respect the controller's named lease: device, threads, state root, checkout, cache, and throughput
isolation. A worktree isolates code, not compute.

This distinction is empirical, not cosmetic: concurrent test workers can consume an entire CPU
while the most expensive candidates hit wall-clock limits. Afterward, model cost and co-tenant
contention are indistinguishable. CPU time measures what the candidate consumed; load facts expose
what the machine withheld.

Long runs must heartbeat at least twice per claim lease, but only while their verified child PID is
alive. Renewal must use the claim's exact owner; never heartbeat on behalf of a process whose
liveness was not verified. On cancellation, use the persisted PGID with `os.killpg`, verify every
member is gone, supersede the in-flight Run with a reason, and release the lease only through
reconciled lifecycle code. Never use a command-pattern kill: it can match the invoking shell and
leave the actual child orphaned.

Progress is distinct from heartbeat. Long-running trainers emit flushed `[progress]` records with
completed units, total units, elapsed time, ETA, and score when meaningful. The campaign job drains
stdout continuously through the canonical progress stream helper so a full pipe cannot deadlock the
child. Do not iterate a text-mode pipe and assume lines are live; reader buffering can withhold
correctly flushed producer output. Score warnings require enough prior units to estimate variance
and must be diagnostic, never an automatic verdict.

## Diagnose before retrying

A void makes the IDEA retryable, but repeating an unchanged cause is not progress. Two voids of the
same kind require a diagnosis before another dispatch:

- repeated invalid or truncated measurements mean the CPU-time budget or trainer must be fixed;
- repeated throughput voids mean the frozen speed floor or resource isolation is excluding the
  candidate class;
- a latest void with no replacement Run is explicitly awaiting rerun.

Acknowledge a diagnosis only after its cause is fixed, with a reason. The acknowledgement records
the current occurrence count; a new void of that kind must surface the diagnosis again. Never use
acknowledgement to mute evidence.

The canonical campaign job reports one typed outcome: `COMPLETE`, `BLOCKED`, `STALLED`,
`RETRYABLE`, `FAILED`, `QUOTA`, or `CANCELLED`. A diagnosis that more runs cannot fix is `BLOCKED`;
an iteration that creates no Run is `STALLED`; a reconciled interruption is `RETRYABLE`. Budget or
backlog exhaustion is a declared campaign policy and a reported scientific result, never an
implicit success.

## A fixable blocker is never a stop

`BLOCKED` is reserved for a cause this loop genuinely cannot remove: absent data it has no
credential to fetch, hardware it does not have, a licence question, a decision that is a human's to
make. **Stopping on a cause the loop could have fixed is a defect, not a safe default.** The
expensive failure mode is a supervisor that exits tidily, reports a blocker, and waits -- when the
blocker was a bug in an adapter, a stale contract, or an upstream model that needed one arm.

So when a run stalls on something outside this campaign:

- if it is a DEFECT -- code not doing what it already claimed -- fix it where it lives, commit it
  separately naming this campaign as the finder, and continue;
- if it is an IMPROVEMENT to an upstream model, author it as an arm in the upstream campaign and
  dispatch that ONE arm through the upstream's own harness and frozen judge. Record the Run against
  the upstream experiment. Do not restart the upstream campaign, do not wake its supervisor, and do
  not take over its backlog -- borrow its judge for one question and return;
- if the upstream verdict has not landed yet, do not wait for it. A declared input reading
  `source: labels` is permanently legal and decouples this campaign's schedule from another's
  verdicts. Measure now, flip to the champion when it lands, record both.

Report every out-of-scope fix with the campaign that prompted it. Silence is the one thing this
allowance does not cover: work that drifts across a scope unremarked destroys the signal the scope
exists to give.

A blocker that gets fixed immediately costs one arm. The same blocker deferred costs a campaign,
because everything measured after it is measured against a known-broken dependency.

## Ancestry-aware ratchet

Three distinct harmful comparisons may invalidate the current adoption only when every rejection
has paired observed-versus-parent counterfactual evidence under the same dataset, split, seed,
harness, preprocessing, device, throughput unit, and intervention digest.

The ratchet must prove the current champion caused harm relative to its direct parent. Three later
rejections alone are insufficient, especially across a stage boundary. Evidence must be consecutive
within the same comparable ancestry and belong to three distinct IDEAs. A stage transition or a
different fairness fingerprint makes a pair ineligible rather than requiring a manual streak reset.
On a valid rollback:

- atomically supersede the harmful adopted Run and effective ModelVersion;
- move `champion` to the parent version with `set_by=ratchet` and a reason;
- revert the adoption merge in Git;
- requeue only IDEAS whose rejection was attributable to that adoption;
- reconcile the RegistrySpace requeue idempotently from the registry event.

Never protect a favored result by clearing evidence. Never roll back from an unpaired streak.

## Finalization and completion

An adopted short-run winner is not necessarily a deployable model. When every stage is populated,
closed, sufficiently measured, and has no required rerun, the project produces the final
checksummed artifact through a Run and the adjudicator creates its active champion ModelVersion.

Then invoke `finalize`. It alone:

1. verifies campaign coverage and current champion lineage;
2. verifies artifact bytes and checksum in the content-addressed store;
3. verifies every required upstream is the current active `production` version;
4. runs the declared compatibility loader against current HEAD;
5. atomically appends the finalization event and moves `production`;
6. re-verifies completeness before the controller releases dependencies.

Process exit, an empty queue, a `champion` alias, or a ModelVersion without `production` is not
campaign completion. The controller independently calls finalization verification before it
unlocks descendants.

Production code may evolve. Every change that can affect a loader must re-run compatibility for
every `production` alias. A broken version becomes effectively `incompatible` until code is
restored or a compatible version is exported and finalized.

## Executable generic proof

The checked-in fixture exercises experiment and Run creation, trainer completion, external
adjudication, atomic ModelVersion plus `champion`, compatibility loading, and `finalize` plus
`production`. It uses a caller-provided disposable registry root, performs no training, and
touches no live campaign:

```sh
python -m knowledge.ml_registry.testing.standard_campaign_fixture \
  --registry-root /tmp/standard-campaign-fixture
```

Use this fixture to validate the generic lifecycle. Do not use a project campaign as documentation
test data.

## Reporting

Report the campaign, current experiment and registered model, initial and final champion versions,
every Run with metric/status/verdict/code SHA, all artifact IDs, stage coverage, dependency and
unreachable dispositions, ratchet actions, compatibility result, and final `production` alias.

Include every rejection, park, void, failure, and supersession with its reason, and the point
estimate and paired interval for every candidate including the rejected ones. A campaign with no
adoptions can be a legitimate result; never widen an interval, drop the pairing, re-seed, or
re-estimate the frozen protocol to manufacture a winner.
