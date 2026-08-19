# fix: Close the curated-campaign seams in ml_registry

**Target repo:** praxis
**Date:** 2026-08-18
**Status:** implementation-ready

## Verdict

The methodology is improvable, and this was also ordinary first-run shakedown of a young
system. Both are true. What is *not* ordinary is that 275 passing tests could not see it.

The registry contains two stacked paradigms that share field names:

| | Autoresearch (R1–R17) | Curated campaign (bootstrap + composing loop) |
|---|---|---|
| Floor | 1σ of 4 metric values (`floor.compute_noise_floor`) | 2σ, often of a 4-seed mean (`bootstrap.measure_noise_floor`) |
| `baseline_throughput` | **mean of the metric** (`floor.py:102–109`) | **rows/sec**, used by VOID (`verdict.py:198–201`) |
| Loop | `supervise-campaign` + dispatch script | project `campaign.py` calling CLI verbs |
| Queue | `untried_backlog` (shrinks as ideas are judged) | staged backlog, composition, `depends_on` |

Existing tests construct fixtures where metric ≈ 1.0 and throughput ≈ 1200 *or* both equal
1.01, so the two meanings of `baseline_throughput` never disagree. That is the missing test
kind: **the documented operator path as a sequence, with numbers that distinguish shared
fields.**

PRs #26 (in-flight trial lock) and #27 (`staging.unreachable`) are the first two silent-failure
fixes. This plan lands them and closes the remaining high-leverage seams.

## Top 3 (ranked by defects prevented / cost)

1. **Golden-path contract test of the documented CLI sequence**, with F1-scale metrics
   (~0.68) and seq/s-scale throughput (~3.5). Catches defects 1–9 and 11 if the fixture
   includes skip / `depends_on` / a slower winner. No real training. ~150 lines.
2. **Stuck stage is an error, not exit 0.** `open_stage` + empty eligible queue after
   `unreachable()` is `StagingStuck`, not success. Plus #26's in-flight refusal. Catches
   11 and 12 — the silent class.
3. **One registration path.** Bootstrap output is valid input to `register-model-with-baseline`.
   Same floor formula, same meaning of `baseline_throughput`. `register-trial` copies
   `throughput`/`diff_lines` from the ledger the way `dispatch_trial` already does.
   Catches 3, 4, 8, 9 at the source.

## What we will not do

- **Typed `TrialRef` objects.** The golden-path test fails the same way a confused caller
  does. Prefix conventions in CLI `--json` output are enough.
- **Flatten stages.** The three staging defects share one cause (filtered ≠ answered).
  Fix the cause. Stages earn their keep when an arm costs minutes.
- **Per-config noise floor now.** None of the 14 defects. Record `seed_sem` (already
  emitted) and bump the floor after an adoption that raises it; do not add 4 extra
  runs per win.
- **A 5-second real training smoke test.** A fake ledger row is the right cost model.
  Training adds flake and does not catch more seams.
- **A process supervisor for orphans.** #26's in-flight refusal is the right layer.
  Process-group kill is skill text, not code.
- **Merge the composing loop into `supervise-campaign`.** Composition cannot be scripted
  up front. The project/registry split is drawn correctly; the registry-*internal* dual
  paradigm is the seam.

## Hypotheses, accepted or rejected

| Hypothesis | Verdict |
|---|---|
| Campaign smoke test (fake arm, full path) | **Accept the path, reject the training.** CLI sequence + fake ledger. |
| Typed / prefixed ids | **Reject as a first move.** `--json` already mints typed `trial-`/`idea-`/`model-` ids. |
| Reject unadjudicable campaign at registration | **Accept, already the bootstrap philosophy.** Apply it to the register seam. |
| Answered vs filtered as first-class | **Accept as `unreachable` + `StagingStuck`.** Do not invent a third status enum. |
| Drop stages | **Reject.** Keep stages; make stuck loud. |
| Project/registry split is the problem | **Reject.** Taste stayed local (VOID disable, stage list, composition). Vocabulary leaked from autoresearch. |

## Implementation units

### U1. Land #26 and #27 onto this branch — **COMPLETE (2026-08-18)**

**Goal:** In-flight trial lock and `staging.unreachable()` are on the working tree.
**Outcome:** #26, #27 and also **#28 (`reset-ratchet`)** merged to `main`. 287 tests green on main.
#28 was not in the original plan: the ratchet reached 2 of 3 on a live campaign from two
architecture rejections that could not bear on the adoption they would have rolled back (both
scored ABOVE the pre-adoption baseline). A stage boundary breaks the ratchet's inference.

### U2. Golden-path contract test (write first; it must fail)

**Goal:** One test runs the documented skill sequence as subprocesses against a fake v2 ledger.
**Files:** `knowledge/ml_registry/tests/test_campaign_path.py`
**Fixture (must distinguish the two meanings):**
- 4 baseline rows: metric 0.6700/0.6795/0.6809/0.6811, throughput 3.38/3.47/3.49/3.49
- one slower winner: metric 0.7034, throughput 3.24 (7% slower — the bones case)
- one parked row inside the floor
- backlog: skip-id, a `depends_on` that parks, a later-stage idea
**Assert:**
- `bootstrap-campaign --out-dir` then `register-model-with-baseline --meta-json model_meta.json` exits 0
- registered `noise_floor` is 2σ, not 1σ
- registered `baseline_throughput` is 3.38 (slowest), not 0.678 (mean F1)
- `register-trial` without caller-supplied throughput still resolves
- slower winner is ADOPTED, not VOIDED
- after the park, `unreachable` lets the next stage open; a stuck stage without that union is an error
- `resolve-verdict --trial-id` with the *idea* id fails naming the *kind* of id

**Execution note:** Write this test first. Every later unit is done when another assertion here turns green.

### U3. One registration path

**Goal:** Bootstrap output is valid `register-model-with-baseline` input, and they agree on the floor and on `baseline_throughput`.
**Files:** `knowledge/ml_registry/bootstrap.py`, `floor.py`, `cli.py`, `tests/test_bootstrap.py`, `tests/test_floor.py`
**Approach:**
1. `build_model_meta` / `bootstrap` emit `baseline_runs` (the 4 commits) and `sigmas`.
2. `register_model_with_baseline` honors `meta["sigmas"]` (default **1** so in-process R12 tests stay put).
3. CLI `register-model-with-baseline` loads full `LedgerRow`s and passes throughputs; recomputed `baseline_throughput` is `min(throughputs)` of the baseline runs, not `mean(metric)`.
4. In-process callers that pass only `dict[str, float]` keep today's mean-metric fallback — that is the R12 API, and it is not the operator path.
5. Do **not** retarget `adjudicate_trial` in this unit. It still uses `baseline_throughput` as a metric bar. The composing path uses `resolve-verdict`. Note it as deferred.

### U4. `register-trial` copies ledger measurements

**Goal:** A composing loop does not have to parrot `throughput`/`diff_lines` out of the trainer.
**Files:** `knowledge/ml_registry/cli.py`, `tests/test_cli.py`, `tests/test_campaign_path.py`
**Approach:** Same `setdefault` the supervisor already does in `dispatch_trial` (cli.py around the register-trial branch). The self-report check in `adjudicate_verdict` still runs; values agree by construction. An explicit override that *disagrees* still refuses.

### U5. Stuck stage is an error

**Goal:** An open stage with no eligible items after `unreachable()` cannot look like a finished campaign.
**Files:** `knowledge/ml_registry/staging.py`, `tests/test_staging.py`, skill
**Approach:** Add `StagingStuck` and `next_queue(...)`. It unions `unreachable` into answered, opens the stage, and raises if the stage still has unanswered members and zero eligible items. Callers that want the old "empty list" must catch it. The skill tells composing loops to treat `StagingStuck` as a missing skip/out-of-scope union, not as success.

### U6. VOID is opt-out, not a hacked constant

**Goal:** A CV campaign can disable the 5% speed gate without setting `baseline_throughput=0.01`.
**Files:** `knowledge/ml_registry/verdict.py`, `bootstrap.py`, `tests/test_verdict.py`, skill
**Approach:** `void_throughput_fraction` on model meta. Default `0.05`. `0` disables. Bootstrap accepts it. The slower-winner assertion in U2 uses the default and must still adopt (gate sits below the slowest baseline, not below every slower arm). A second case with `void_throughput_fraction=0` documents the CV opt-out.

### U7. Skill matches the path the test runs

**Goal:** Following `af-ml-supervise` cannot reproduce defects 2, 3, 5, 7, 9, 11.
**Files:** `agent_factory/skills/af-ml-supervise/SKILL.md`
**Approach:** Document `register-model-with-baseline --meta-json <path>` as the bootstrap consumer; `--json` on register-trial; never use `backlog` as the roster; treat `StagingStuck` as an error; VOID is opt-out via `void_throughput_fraction`; kill the process group, not `pkill -f`.

### U8. A stage must earn the right to close — **COMPLETE (2026-08-18)**

**Goal:** A stage cannot close having tested nothing, and the architecture axis cannot be thin by
accident.

**Why this was not in the original audit:** the audit catalogued defects that produced WRONG or
STUCK behaviour. This one produced a campaign that ran correctly and answered less than it
appeared to — which is not a defect any test could have caught, because nothing was broken.

**The evidence.** The architecture stage — the axis the stage order itself calls high-leverage —
held five authored ideas and produced **two** real comparisons:

| idea | outcome |
|---|---|
| M01 mlp | ran → rejected |
| M02 tcn | excluded by `--skip-ids` — never ran |
| M03 gru | re-measured the INCUMBENT; could only park |
| M04 transformer | ran → rejected |
| M05 composition | dead: `depends_on: [M02, …]` |

One skip cost two arms. The campaign then advanced and gave **augmentation four arms** — more than
the axis that decides what the model IS.

Worse, the axis was never diverse: every head (linear, MLP, TCN, GRU, transformer) is a sequence
model over *flattened* joints, while the skeleton-action-recognition literature had converged on
**graph** models. The tell was in the results all along — the only winning arm was `bones`, which
is 2s-AGCN's second stream. The campaign adopted a component of the SOTA architecture without ever
trying the architecture.

**Files:** `knowledge/ml_registry/staging.py`, `tests/test_staging.py`, skill.
**Delivered:** `stage_coverage()` / `thin_stages()` counting only arms that RAN and produced a
verdict from their own result, with `MIN_MEASURED_PER_STAGE = 3`. Advisory, not blocking — a thin
stage is legitimate when an axis genuinely has few options, but it must be REPORTED as thin rather
than presented as settled. Skill gains two rules: a prior loss transfers only if its ARGUMENT
transfers (and a skip silently kills its dependents), and model FAMILIES must be enumerated before
the architecture stage opens, with untried families named in the report.

**Deferred:** family membership is not machine-checkable and stays a reporting obligation.

## Silent vs loud (autonomous run, no human reading dry-run output)

| # | Defect | Autonomous time-to-notice |
|---|---|---|
| 11 | Staging filtered-as-unanswered | **Never** — exit 0, looks finished |
| 12 | Concurrent trials, last-wins | **Never** — two verdicts, last one stands |
| 7 | `backlog` shrinks, judged ideas look excluded | **Never** if used as roster |
| 10 | VOID discards a real winner | Looks like a void, not a win; campaign continues |
| 1, 4, 8 | Wrong floor / wrong throughput meaning | Verdicts look legitimate, just wrong |
| 9 | Missing self-report fields | Loud, but **after** the paid run |
| 2, 3, 5, 6 | CLI / id mismatches | Loud, before or at first verdict |
| 13, 14 | `pkill -f`, `git rm --cached` | Operator. Skill text only. |

## Remaining after this plan

- `adjudicate_trial` still treats `baseline_throughput` as a metric. Only `resolve-verdict` is the composing path.
- Campaign-local ratchet in `sports_analysis` (project, not this plan) still duplicates the registry ratchet and does not re-queue.
- Per-config floor: if an adopted arm's `seed_sem * 2` exceeds the registered floor, raise it. Cheap, not in this PR.
