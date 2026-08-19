---
name: af-ml-supervise
description: Drive ONE registered ml_registry model's campaign to close — dispatch trials serially, adjudicate each against the external ledger, advance the baseline on a win, and roll it back when the ratchet fires. Use when a project has a scalar metric, a training command that appends to results.tsv, and a backlog of hypotheses to work through one at a time. This is the supervisor half of the ML research loop; af-build routes any ticket carrying meta.experiment_id here.
---

# af-ml-supervise

The loop that turns a backlog of hypotheses into a moving baseline: run one arm, score it against
an external ledger, keep it if it beats the noise floor, drop it if it loses, and roll back an
adoption that three later rejections expose as noise.

**This skill is referenced by name from four places that already exist** —
`af-build/SKILL.md`'s research routing, three helpers in `hooks/_ticket_state.py`, and
`knowledge/ml_registry`'s design proposal. Everything downstream of it shipped; this is the piece
that was missing. Read `docs/proposals/2026-08-13-ml-research-registry.md` before changing its
contract.

## What it is NOT

- **Not `af-ml-model`.** That skill is a thin trigger over Karpathy's autoresearch, where the loop
  blindly mutates `train.py` and keeps or reverts on `val_bpb`. This one works a CURATED backlog
  of stated hypotheses. Different paradigm; do not merge them.
- **Not a trainer.** It never trains anything. It decides what to run and what the result means.
  The project supplies the training command.
- **Not a proposer, and not the setup half.** Standing the campaign up (data inventory,
  metric, loaders, dispatch, four baseline rows, `bootstrap-campaign`) and seeding the
  backlog are `/af-seed-ml-supervise`'s job (the written `af-ml-ideate`; idea writes
  remain `knowledge.ml_registry.ideate` via `seed-campaign`). If those preconditions
  are not met, go there — do not hand-roll them inside this skill.
  `register-idea` by hand is still a perfectly good way to add one idea to a campaign
  that is already registered.

## Preconditions — one command checks them all

```sh
python -m knowledge.ml_registry.cli bootstrap-campaign \
    --ledger <project>/results.tsv --backlog <project>/backlog.jsonl \
    --model-id <id> --metric <name> --direction maximize \
    --diff-size-limit 8 --skip-ids <settled> --out-dir <project>/registry \
    [--void-throughput-fraction 0]    # disable the SPEED void only; unfair runs still void
```

`bootstrap-campaign` verifies every precondition below, MEASURES the noise floor from the ledger's
own baseline rows, and emits a schema-valid `model_meta.json` plus seeded `ideas.jsonl`. It exits
non-zero and names the blocking precondition rather than registering something unadjudicable —
which matters because each of these failures is invisible until registration, long after the
training runs that filled the ledger have been paid for.

Do not hand-roll these checks per project. What is systematic lives in
`knowledge/ml_registry/bootstrap.py`; what is project-specific is the metric's meaning, the
trainer, and the backlog.

If this command would refuse, `/af-seed-ml-supervise` is the skill that builds
the missing pieces against the project's collected data. Do not reconstruct them
here and do not start supervising a campaign `bootstrap-campaign` has not admitted.

The preconditions it enforces, and why each one is fatal rather than cosmetic:

1. **A registered model.** `meta` must carry `metric`, `direction`, `win_condition`, `baseline`,
   `noise_floor`, `baseline_throughput`, `diff_size_limit`. Bootstrap now also emits
   `baseline_runs` (the 4 commits) and `sigmas`, so `register-model-with-baseline` consumes
   `model_meta.json` as-is. The metric is FROZEN for the model's life — changing it mid-campaign
   silently rebases every prior verdict.
2. **A version-2 ledger.** `results.tsv` with header
   `commit  metric_value  memory_gb  status  description  throughput  diff_lines`.
   Versions 0 and 1 lack `throughput`/`diff_lines` and cannot be adjudicated: a synthesized
   throughput equal to baseline can never trip the void floor and a synthesized `diff_lines` of 0
   can never breach `diff_size_limit`, so inventing them turns two of the four verdicts into dead
   code. **Do not add the columns by hand — add them to the loop that writes the file.**
3. **At least 4 baseline rows** in that ledger. `REQUIRED_BASELINE_RUN_COUNT = 4`; the noise floor
   is measured from them and never re-measured per trial.
4. **A unique join key per arm.** The registry joins trials to ledger rows by the `commit` column.
   A campaign that varies arms by CONFIG rather than by code will write every row under the same
   SHA and the join collapses. Use `{sha}:{arm_tag}` — it identifies the harness code and the arm,
   which is strictly more informative than a bare SHA for a config-varying campaign.

   Observed on the first real campaign: 16 ledger rows sharing 2 keys, discovered at registration
   after every one of those training runs had already been paid for.

5. **Homogeneous baseline throughput.** `baseline_throughput` is the SLOWEST baseline run's
   seq/s (not the metric mean ~0.68). It gates the VOIDED verdict, so a baseline measured under
   different settings — one seed among four-seed runs, say — makes that gate meaningless.
   `bootstrap-campaign` flags it; the fix is to drop the odd row, not to average it in.
6. **A dispatch command** the project owns, which runs ONE arm and appends ONE row.

If the box is remote, `af-ml-model-remote` is the mechanism (ssh + tmux, detached). It adds no ML
logic and its box is CPU-only.

## The decision rule, and the one number that governs it

```
ledger status not in {ok, ""}                                      -> VOIDED (unfair run)
throughput < baseline_throughput * (1 - void_throughput_fraction)  -> VOIDED (speed)
delta >  noise_floor   -> ADOPTED    baseline advances to this arm
delta < -noise_floor   -> REJECTED
otherwise              -> PARKED     (stagnant but cheap) or REJECTED (stagnant and costly,
                                      by diff_size_limit)
```

Two VOID gates, checked in that order, and they mean different things. An unfair run
(`budget_exhausted`, …) was never measured; a slow run was measured and was expensive. `#32`
voids the first *before* the speed gate, so a truncated arm that was also slow is recorded as
truncated.

`void_throughput_fraction` lives on model meta (bootstrap default `0.05`). **`0` disables the
speed gate only.** Unfair runs still void. CV campaigns whose metric is not training speed must
set `--void-throughput-fraction 0` rather than hacking `baseline_throughput=0.01` — and must
not read that as "VOID is off".

Both tests are **strict**: a delta of exactly one floor is evidence of nothing in either
direction.

**`noise_floor` is the most consequential constant in a campaign, and 4 runs is a thin basis for
it.** An SD estimated from 4 points carries roughly 40% relative uncertainty, and the floor sets
the bar for every verdict that follows. Two things are worth doing beyond the minimum:

- **Measure it from more than 4 runs** if a run is cheap, then register the measured value
  explicitly rather than letting it be inferred. On the tennis stroke campaign, 4 runs suggested
  SD 0.0164 while 12 runs gave 0.0115 — the smaller sample was inflated by an artifact, and only
  the larger one exposed it.
- **Adjudicate a MEAN of repeats, not a single run**, when repeats are affordable. Averaging r
  runs divides the standard error by sqrt(r). On that campaign a single 5-fold run had SD 0.0115
  and a spread of 0.0393 across seeds — seed alone moved a result four points, so single-run
  adjudication against a one-sigma floor would have manufactured roughly five winners from
  optimiser noise across a 35-arm backlog. Setting the floor at 2x the standard error of a 4-seed
  mean brought that under one.

The registry cannot know which of these applies; it takes the floor you give it. Give it a
measured one.

## Sequencing the backlog

A backlog with dependencies and a cost filter still has no ORDERING, and without one a campaign
will tune a hyperparameter for a model it is about to replace. `knowledge/ml_registry/staging.py`
supplies ordered stages; the project supplies the stage LIST, because a vision campaign's stages
are not an LLM finetune's.

```python
from knowledge.ml_registry.staging import next_queue, StagingStuck

STAGES = ("representation", "architecture", "augmentation", "training", "tuning", "capacity")
try:
    stage, queue, blocked = next_queue(
        backlog, answered_ids=answered, adopted_ids=adopted, stages=STAGES)
except StagingStuck:
    # missing skip/out-of-scope union — NOT a finished campaign
    raise
```

Prefer `next_queue(...)`: it unions `unreachable` into answered, opens the stage, and raises
`StagingStuck` if that stage still has leftover unanswered items and an empty eligible queue.
Treat `StagingStuck` as a missing skip/out-of-scope union, not success.

Two rules in it are worth knowing before you rely on them:

**A stage closes on ANY verdict, not on an adoption.** A stage that answered "none of these help"
is settled — the first campaign this was built for had seven representation arms and zero
adoptions, which is a real answer about the corpus, not a failure. Gating on adoption would wedge
a campaign behind any inert axis forever. `errored` deliberately does NOT count as answered: an
arm that crashed is not an arm that lost.

**`depends_on` and `stage` mean different things and are both needed.** `depends_on` gates on one
idea WINNING — a composition arm is only meaningful if the thing it composes with was adopted.
`stage` gates on a whole question being ANSWERED, however it was answered.

**A stage will not close if anything in it is merely FILTERED rather than answered**, and the
failure is silent: `open_stage` keeps returning that stage, the eligibility filter yields an empty
queue, and the loop exits reporting nothing to do — indistinguishable from a finished campaign.
On the first campaign to use staging, a 27-item backlog would have stopped after four items and
looked successful. Three separate things caused it there, and every campaign has the same three:

```python
answered |= excluded          # in the local backlog but never registered (--skip-ids)
answered |= out_of_scope      # filtered by the loop (needs different data, too expensive, ...)
answered |= unreachable(items, answered, adopted)   # dependency judged but not adopted
```

`unreachable()` is supplied because `depends_on` gates on ADOPTION: the moment a dependency is
answered as anything else, every dependent is dead, and dead items hold a stage open forever. It
runs to a fixpoint so a chain collapses in one pass rather than one link per invocation.

**Report all three rather than filtering silently.** An item that never ran for a structural
reason is a real omission, and a reader will otherwise mistake it for one that was tried and lost.

**The cost is real and permanent:** a late stage never influences an early one. If an augmentation
would only pay off under an architecture that lost, that interaction is invisible. Staging trades
interaction coverage for not spending the budget on questions whose answer is about to be
invalidated — right when arms are expensive, wrong when they are nearly free. Do not stage a
campaign whose arms cost seconds.

## A stage must EARN the right to close

A stage closes when every item in it is answered. Nothing checks whether it was answered by
**running** anything. An item can be answered by being excluded at registration, by becoming
unreachable through `depends_on`, by being filtered as out of scope, or by being a no-op against
the incumbent — and a stage made of those closes having tested nothing.

```python
from knowledge.ml_registry.staging import stage_coverage, thin_stages

cov = stage_coverage(backlog, STAGES, measured_ids=ran_and_produced_a_verdict)
if thin_stages(cov):
    log(f"THIN: {thin_stages(cov)} closed on fewer than 3 measured arms")
```

`measured_ids` must contain only items that ran and produced a verdict **from their own result**.

Observed on the first staged campaign, on the axis the stage order itself calls high-leverage.
The architecture stage held five authored ideas and produced **two** real comparisons:

| idea | outcome |
|---|---|
| M01 mlp | ran → rejected |
| M02 tcn | excluded by the skip list — never ran |
| M03 gru | re-measured the INCUMBENT; could only park |
| M04 transformer | ran → rejected |
| M05 composition | dead: `depends_on: [M02, …]` |

One skip cost two arms. The campaign then advanced and gave **augmentation four arms** — more
than the axis that decides what the model IS.

### Two rules that follow

**A prior loss transfers only if its ARGUMENT transfers.** Skipping `velocity` was sound: it cost
26 dimensions against ~1,400 samples, and that argument holds under any baseline. Skipping a TCN
was not: it lost in a superseded campaign against a different metric AND a different
representation, and nothing about "it lost before" survives adopting a new representation
underneath it. Before adding an id to `--skip-ids`, write down the argument and check it still
holds. **And check what depends on it** — a skip silently kills its dependents.

**Enumerate the model FAMILIES before opening the architecture stage.** Arms that differ only in
depth or width are one family, not four. On that campaign every head — linear, MLP, TCN, GRU,
transformer — was a sequence model over *flattened* joints, while the skeleton-action-recognition
literature had converged on **graph** models. The gap went unnoticed because the stage looked
populated. The tell was in the results the whole time: the only arm that won was `bones`, which is
literally 2s-AGCN's second stream — the campaign adopted a component of the SOTA architecture
without ever trying the architecture.

List the families that exist for the task, name the one each arm belongs to, and **state in the
report which families you did not try and why**. An untried family is a hole in the result, not an
absence of evidence about it.

## A wall clock is not a measurement instrument on a shared machine

`throughput` gates the VOID verdict and a wall-clock budget decides whether a run was truncated.
**Both are wall-clock derived, so both measure the neighbours as much as the arm.**

Observed: a campaign ran on an 8-core box while other agents used ~800% CPU running test suites.
Two arms -- the two most expensive architectures in the backlog -- were truncated by their budget
and voided. Model cost and co-tenant contention are both consistent with that, and after the fact
they cannot be separated. The budget was raised on the assumption it was model cost, which may
simply have been wrong.

- **Budget on CPU TIME** (`resource.getrusage(RUSAGE_SELF).ru_utime + ru_stime`), not wall clock.
  CPU time is what the arm actually consumed; wall clock is what the machine had left over.
- **Record load alongside throughput** if you keep a speed gate at all, so a void can be
  attributed rather than guessed at.
- **A git worktree does not isolate CPU.** Separating code is not separating the machine.

Otherwise the same arm passes or fails depending on who else is running, and the campaign records
that as a property of the model.

## "Answered" comes from the TRIAL, never from the idea

`resolve-verdict` writes the verdict onto the **trial**. It does not stamp the idea. So a loop that
asks `idea.meta.status` sees `None` for every arm the registry just adjudicated.

That is not a cosmetic difference. A campaign that treats "no idea status" as *unanswered* will
delete the arm's local verdict, re-queue it, run it again, and repeat — **indefinitely**. Measured:
three arms re-ran before it was caught, and nothing in the loop could notice, because every
iteration looked like honest new work with real progress lines and real ledger rows.

```python
from knowledge.ml_registry.report import idea_verdicts
answered = set(idea_verdicts(space, model_id))    # {tag: trial status}, latest trial per idea
```

Two exclusions are load-bearing:

- **`voided` does not answer.** The run was unfair, so the question is still open and the arm must
  re-run.
- **`complete` does not answer.** It means the training finished and is AWAITING adjudication —
  the trial is in flight, not settled.

The latest trial wins, so a successful re-run supersedes an earlier void.

## Acknowledge a diagnosis you have actually fixed

A diagnosis is computed from the trial HISTORY, so it **outlives its own cause**. Raise the
budget, disable the gate, move to a quieter machine — none of that erases the voids that prompted
it, so it keeps firing and a loop that halts on a blocking diagnosis halts *permanently*.

```sh
python -m knowledge.ml_registry.cli acknowledge-diagnosis \
    --space-file <state>.json --model-id <id> --kind budget_too_small \
    --reason "budget now measured in CPU time, not wall clock"
```

**This is not a mute.** The void count of that kind is recorded at acknowledgement, and the
diagnosis fires again the moment a NEW void of the same kind appears. Acknowledging a cause you
did not actually fix therefore buys exactly one more arm before the loop stops again — which is
the right cost, because it makes a false acknowledgement cheap to detect and impossible to
sustain.

## Diagnose a void, do not just re-run it

A void is a decision to **re-run**. That is right once, and wrong the moment the reason is
something re-running cannot change.

```sh
python -m knowledge.ml_registry.cli campaign-status --space-file <state>.json --model-id <id>
```

surfaces `diagnoses` alongside the verdicts. Two voids of the same KIND is not bad luck — it says
the setting that produced them will keep producing them:

- **`budget_too_small`** — arms voided as unfair runs (truncated). Re-running reproduces the
  truncation. Raise the budget above the SLOWEST arm you intend to try, not the typical one.
- **`void_gate_too_tight`** — arms voided on throughput. A structurally slower arm (a richer
  representation, a heavier head) can never pass, so the gate is rejecting on cost rather than
  merit. Set `void_throughput_fraction` to 0, or reference the SLOWEST baseline rather than the
  median.
- **`awaiting_rerun`** — ideas whose latest trial voided and are therefore still UNMEASURED.
  Treating a void as answered records nothing at all, which is strictly worse than a rejection:
  a rejection at least says a question was asked.

**Why this matters more than it looks.** On the first campaign to run expensive arms, the two most
costly architectures in the backlog both exceeded a wall clock tuned for heads that finish in a
third of the time. Both voided. An autonomous loop would have re-run each, truncated each again,
and closed on `CLOSE_VOID_LIMIT` having explained nothing — while the surviving evidence said
"cheap models win". That is a selection effect produced by the harness, not a result: a budget
tuned to cheap arms silently removes exactly the arms that might beat them.

## The ratchet

Three consecutive rejections on **distinct** ideas is read as evidence the last adoption was
noise. The adoption is invalidated, the baseline is restored to `previous_baseline`, and every
idea rejected under that inflated bar is re-queued to the untried backlog.

This exists because a false adoption is self-concealing: it raises the bar for everything after
it, so the errors it causes look like ordinary rejections. A prior adoption that is beaten
FAIRLY is `superseded` instead, and ideas rejected under it stay rejected.

**The inference only holds while the rejections compete against the adoption on the same axis.**
Across a stage boundary it breaks: later arms vary something the adoption never competed against,
so their rejections are not evidence about it.

Observed on the first staged campaign — a representation change adopted at +0.0239, then two
architecture arms rejected (an MLP at −0.0177, a transformer at −0.0146). Neither was caused by an
inflated bar: **both scored ABOVE the pre-adoption baseline** and would merely have parked against
it. They lost because those architectures are worse on ~1,400 samples, which is precisely what one
of them was authored to demonstrate. One further rejection from an unrelated augmentation arm would
have rolled back a sound adoption and re-queued three settled ideas.

```sh
python -m knowledge.ml_registry.cli reset-ratchet \
    --space-file <state>.json --model-id <id> \
    --reason "architecture stage closed; its arms varied the head, not the adopted representation"
```

The registry cannot detect a stage boundary — stages are the caller's taxonomy — so this is
explicit rather than automatic. It clears ONLY the streak: baseline, previous_baseline and every
recorded verdict are untouched, so a genuinely false adoption stays catchable by the next three
rejections that do compete with it. Every reset is recorded on the model with its reason, because a
ratchet cleared without one is indistinguishable from a ratchet cleared to protect a favoured
result.

## Running it

```sh
cd <praxis repo root>          # the package is not pip-installed; imports need this cwd

# --meta-json is the PATH to bootstrap's model_meta.json (carries baseline_runs + sigmas)
python -m knowledge.ml_registry.cli register-model-with-baseline \
    --space-file <state>.json --meta-json <out-dir>/model_meta.json --ledger <project>/results.tsv
# stdout: OK: registered model model-<hex>  -- rewrite each idea.model_id to this minted id

# --meta-json only (no --model-id flag). model_id lives inside the json, from ideas.jsonl.
python -m knowledge.ml_registry.cli register-idea \
    --space-file <state>.json --meta-json <idea>.json

# Non-composing campaign -- dispatch list written up front:
python -m knowledge.ml_registry.cli supervise-campaign \
    --space-file <state>.json --model-id <minted-id> --ledger <project>/results.tsv \
    --dispatch-script <trials>.json [--max-dispatches N] [--lesson-file lessons.jsonl]
```

Do **not** use the `backlog` CLI verb as the campaign roster — it lists only UNTRIED ideas, so
judged arms vanish and look excluded.

**Know what `--dispatch-script` is before you rely on it.** It is a list of trial-meta objects
consumed in dispatch order — the supervisor pops one per dispatch. It adjudicates correctly and
reproducibly, but it cannot CHOOSE an arm from prior results, because the whole list is written up
front.

That matters whenever a campaign wants to **compose** winners: if adopting `drop_face` should make
every later arm run as `drop_face,<candidate>`, the arm depends on a verdict that does not exist
when the script is written. Two honest options, and the choice should be explicit:

- **Non-composing campaign** — every arm is measured against the moving baseline VALUE but is
  itself independent. The dispatch script works as-is, and the run is fully reproducible.
- **Composing campaign** — the project drives the loop itself, one arm at a time, and calls the
  registry only for the verdict. Adjudication stays external; only the ORDER becomes adaptive.
  `sports_analysis`'s `src/stroke_lab/campaign.py` is the worked example. Use `--json` on
  `register-trial` to get `trial_id`; pass that to `resolve-verdict --trial-id`, never an idea id.

  ```sh
  python -m knowledge.ml_registry.cli register-trial \
      --space-file <state>.json --meta-json <trial>.json --ledger <project>/results.tsv --json
  # {"trial_id": "trial-<hex>"}  -- this is what resolve-verdict needs
  python -m knowledge.ml_registry.cli resolve-verdict \
      --space-file <state>.json --trial-id <trial-id> --ledger <project>/results.tsv --json
  ```

Never resolve this by having the loop compute its own verdict. A verdict decided by the code that
wants to win is not a verdict, and the external ledger exists precisely to prevent it.

## One trial in flight per idea, and how to kill a campaign

`register-trial` refuses a second trial for an idea that already has one unresolved. Two at once
means the same question is being answered twice concurrently, and the registry would adjudicate
both — two verdicts for one idea, with whichever resolved last silently winning.

This is not a hypothetical. On the first real campaign, killing a supervising loop left its
training child **orphaned to PID 1**, still running the PREVIOUS (uncomposed) configuration. The
relaunched campaign started the composed arm under the same idea, so two runs raced — each taking
half an 8-core box, each about to write a ledger row under the same arm tag. Nothing objected. The
duplicate was found by reading `ps`, which is not a control.

**Killing a composing campaign means killing its process GROUP.** The supervisor spawns the
trainer as a child; terminating only the parent orphans the child, which keeps burning CPU and
finishes by writing a row for a configuration nobody is adjudicating any more.

```sh
kill -TERM -"$(ps -o pgid= -p "$PID" | tr -d ' ')"    # the leading '-' means the GROUP
ps -eo pid,ppid,cmd | grep '[t]rain'                  # then VERIFY. PPID 1 is an orphan.
```

**Do not reach for `pkill -f <pattern>` over ssh.** The remote shell's own command line contains
the pattern, so `pkill` matches and kills the shell before any later command in the same
invocation runs. That is precisely how the orphan above survived a kill that appeared to succeed:
`pkill -f "…campaign"; pkill -f "…train"` never reached the second statement.

If a run genuinely died without resolving, its trial stays in flight and would wedge the idea
forever. Free it deliberately:

```sh
python -m knowledge.ml_registry.cli supersede-trial \
    --space-file <state>.json --trial-id <id> --reason "orphaned by a supervisor restart; PID confirmed dead"
```

`--reason` is required. A trial abandoned without one is indistinguishable from a trial quietly
discarded for losing, and the dead-ideas register depends on telling those apart.

Note `voided` does NOT block a re-run — that is what voided means — and neither does any trial
that already has a verdict. Only `running`/`complete` block, and `complete` means *the run
finished and is awaiting adjudication*, which is exactly the state a duplicate dispatch races.

## Run it to COMPLETION, not to an empty queue

**An empty queue is not a finished campaign.** A composing loop runs one batch of arms and exits;
if nothing relaunches it, a human sits in every stage transition and the campaign quietly stops
partway through its own plan.

Measured on the first real campaign: it completed a partial architecture search and halted.
Augmentation, training, tuning and capacity were never reached, and no train-to-convergence
existed as a concept at all. **Nothing errored** — each invocation exited 0 having done exactly
what it was asked, and what it was asked was one stage's worth of arms.

```sh
AF_DISPATCH="uv run python -m stroke_lab.campaign --max-arms 8 ..." \
agent_factory/scripts/af-ml-campaign-loop.sh \
    --space-file <state>.json --model-id <id> \
    --stages representation,architecture,augmentation,training,tuning,capacity
```

The loop stops for exactly three reasons, and conflating them is how a campaign wastes a night:

| exit | meaning |
|---|---|
| `0` COMPLETE | `campaign-complete` passed |
| `3` BLOCKED | a diagnosis more arms cannot fix — a budget that truncates every retry, a stage nobody authored |
| `4` STALLED | an iteration produced no new trial; repeating it changes nothing |

### What `campaign-complete` demands

```sh
python -m knowledge.ml_registry.cli campaign-complete \
    --space-file <state>.json --model-id <id> --stages <ordered,phases>
```

- **Every phase POPULATED.** A stage with zero registered arms is trivially "all answered", so it
  closes instantly and the campaign sails past a question nobody asked. Both `tuning` and
  `capacity` were empty on the first campaign and neither was mentioned anywhere.
- **Every phase CLOSED, and not thin** — see `stage_coverage`.
- **Nothing awaiting a re-run.** A voided arm is unmeasured, not answered.
- **A train-to-convergence run**, recorded as `convergence_run` on the model. Every arm in a
  campaign is a short cross-validation probe tuned to DISCRIMINATE between candidates — not a
  trained model. Selecting a winner and never training it is half a job. Waive with
  `--no-require-convergence` only for a campaign that never meant to ship.

## Closing

Close reasons are an enum: `CLOSE_WON`, `CLOSE_MAX_TRIALS`, `CLOSE_BACKLOG_EXHAUSTED`,
`CLOSE_VOID_LIMIT` (3 consecutive voids), `CLOSE_TRIAL_TIMEOUT`.

**Decide `--budget-exhausted-ok` explicitly.** Without it a campaign that never improves can never
close, and it wedges the whole build set behind af-build's completeness gate. A campaign that
honestly found nothing is a RESULT, not a failure, and it must be able to say so.

## Progress logging — a heartbeat is not progress

**A heartbeat proves the process is alive. It cannot tell you how far along it is, whether it will
finish this hour, or whether what it is producing is getting worse.** Those are the questions
actually asked while a job runs, and answering them by waiting for the job to end is the same as
not answering them.

Measured on the first campaign to run an expensive step: it ran **28 minutes emitting nothing**.
Its per-unit scores were 0.6183 / 0.6273 / 0.4123 / 0.0491 — diverging from the third unit onward
— and it was *simultaneously* being truncated by a wall-clock budget. Both facts existed inside
the process the whole time. Both were only discoverable after it exited, by which point a
meaningless number had been adjudicated and recorded as a verdict. Every check while it ran
returned "394% CPU", which was true right up to the end and told nobody anything.

Any step that can run longer than a few minutes MUST emit one line per unit of work:

```python
import sys; sys.path.insert(0, "<praxis>/agent_factory/scripts")
from progress import Progress

p = Progress("M06 stgcn", total=20)      # total is what makes an ETA possible
for unit in units:
    ...
    p.step(score=metric)                  # score enables the degradation warning
p.done()
```

```
[progress] M06 stgcn 7/20 35% elapsed 9m48s eta 18m12s last=0.6183 mean=0.6221
[progress][WARN] M06 stgcn: last=0.0491 is 3.2 sigma below the mean of the previous 7 (0.5881)
```

Three properties are load-bearing:

- **`total` gives an ETA**, which is what turns "it is still running" into a decision. The unit
  count is almost always known up front — folds × seeds, files to migrate, tickets in a set.
- **`score` gives a degradation warning** *while there is still time to act*. It fires at 3 sigma
  over at least 4 prior samples, so it stays rare enough to be read.
- **Lines are flushed and prefixed `[progress]`**, so a supervisor can `grep` them out of an
  interleaved log. A buffered progress line is not a progress line.

**The consumer half is where this actually breaks.** A correct producer is useless behind a
reader that buffers, and the obvious reader is wrong in a way that looks right:

```python
for line in proc.stdout:          # WRONG -- hidden read-ahead buffer withholds lines
```

Iterating a pipe in text mode fills an internal buffer before yielding anything. Measured: an arm
emitted its first progress line at 1m31s and then nothing for 30 minutes, while the child sat at
373% CPU working normally. The producer was correct and `PYTHONUNBUFFERED` was set — the
supervisor's own reader was holding the lines. Use the helper:

```python
from progress import stream_progress
out = stream_progress(proc)       # echoes [progress] live, returns full stdout
```

It also drains stdout continuously, which matters independently: a child that fills a pipe nobody
is reading BLOCKS, so collecting output only after `wait()` deadlocks anything chatty enough to
fill 64KB.

Non-Python steps follow the same convention by hand: one flushed line per unit, prefixed
`[progress]`, carrying `n/total`, `elapsed`, and a metric where one exists.

## Reporting

Report every arm, including the losers. An experiment reported only when it wins is not a
measurement. Specifically:

- the adopted chain, in order, with each delta
- every rejection WITH its reason — this is the dead-ideas register, and its value is that a
  future session does not re-propose a settled question
- every park, which is "no evidence either way" and NOT a soft rejection
- every VOIDED or ERRORED arm, distinguished from arms that lost. An arm that crashed is not an
  arm that lost, and conflating them silently shrinks the campaign.

A campaign of N arms with zero adoptions is a legitimate and informative outcome. Say so plainly
rather than lowering the floor until something passes.
