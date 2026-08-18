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
- **Not a proposer.** Seeding the backlog is `af-ml-ideate`'s job (unwritten). Until it exists,
  seed the backlog by hand with `register-idea`, which is a perfectly good way to work.

## Preconditions — one command checks them all

```sh
python -m knowledge.ml_registry.cli bootstrap-campaign \
    --ledger <project>/results.tsv --backlog <project>/backlog.jsonl \
    --model-id <id> --metric <name> --direction maximize \
    --diff-size-limit 8 --out-dir <project>/registry
```

`bootstrap-campaign` verifies every precondition below, MEASURES the noise floor from the ledger's
own baseline rows, and emits a schema-valid `model_meta.json` plus seeded `ideas.jsonl`. It exits
non-zero and names the blocking precondition rather than registering something unadjudicable —
which matters because each of these failures is invisible until registration, long after the
training runs that filled the ledger have been paid for.

Do not hand-roll these checks per project. What is systematic lives in
`knowledge/ml_registry/bootstrap.py`; what is project-specific is the metric's meaning, the
trainer, and the backlog.

The preconditions it enforces, and why each one is fatal rather than cosmetic:

1. **A registered model.** `meta` must carry `metric`, `direction`, `win_condition`, `baseline`,
   `noise_floor`, `baseline_throughput`, `diff_size_limit`. The metric is FROZEN for the model's
   life — changing it mid-campaign silently rebases every prior verdict.
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

5. **Homogeneous baseline throughput.** `baseline_throughput` gates the VOIDED verdict, so a
   baseline measured under different settings — one seed among four-seed runs, say — makes that
   gate meaningless. `bootstrap-campaign` flags it; the fix is to drop the odd row, not to average
   it in.
6. **A dispatch command** the project owns, which runs ONE arm and appends ONE row.

If the box is remote, `af-ml-model-remote` is the mechanism (ssh + tmux, detached). It adds no ML
logic and its box is CPU-only.

## The decision rule, and the one number that governs it

```
delta = value - baseline            (sign flipped when direction == "minimize")

delta >  noise_floor   -> ADOPTED    baseline advances to this arm
delta < -noise_floor   -> REJECTED
otherwise              -> PARKED     (stagnant but cheap) or REJECTED (stagnant and costly,
                                      by diff_size_limit)
throughput < baseline_throughput * 0.95  ->  VOIDED, before any of the above
```

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

## The ratchet

Three consecutive rejections on **distinct** ideas is read as evidence the last adoption was
noise. The adoption is invalidated, the baseline is restored to `previous_baseline`, and every
idea rejected under that inflated bar is re-queued to the untried backlog.

This exists because a false adoption is self-concealing: it raises the bar for everything after
it, so the errors it causes look like ordinary rejections. A prior adoption that is beaten
FAIRLY is `superseded` instead, and ideas rejected under it stay rejected.

## Running it

```sh
cd <praxis repo root>          # the package is not pip-installed; imports need this cwd

python -m knowledge.ml_registry.cli register-model-with-baseline \
    --space-file <state>.json --meta-json <meta>.json --ledger <project>/results.tsv

python -m knowledge.ml_registry.cli register-idea \
    --space-file <state>.json --model-id <id> --meta-json <idea>.json      # per hypothesis

python -m knowledge.ml_registry.cli supervise-campaign \
    --space-file <state>.json --model-id <id> --ledger <project>/results.tsv \
    --dispatch-script <trials>.json [--max-dispatches N] [--lesson-file lessons.jsonl]
```

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
  `sports_analysis`'s `src/stroke_lab/campaign.py` is the worked example.

Never resolve this by having the loop compute its own verdict. A verdict decided by the code that
wants to win is not a verdict, and the external ledger exists precisely to prevent it.

## Closing

Close reasons are an enum: `CLOSE_WON`, `CLOSE_MAX_TRIALS`, `CLOSE_BACKLOG_EXHAUSTED`,
`CLOSE_VOID_LIMIT` (3 consecutive voids), `CLOSE_TRIAL_TIMEOUT`.

**Decide `--budget-exhausted-ok` explicitly.** Without it a campaign that never improves can never
close, and it wedges the whole build set behind af-build's completeness gate. A campaign that
honestly found nothing is a RESULT, not a failure, and it must be able to say so.

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
