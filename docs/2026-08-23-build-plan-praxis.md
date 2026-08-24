# Build plan: `praxis` (the research engine)

Status: axes agreed, design in progress. Companion to `2026-08-23-build-plan-sports-analysis.md`.

**Every ticket in this document is built in the `praxis` repo.** Work that touches the
`sports_analysis` tree — structural spec validation, the landing commit, `RESULT.md`, and authoring the
campaign specs — is T7/T8/T9 of the sports_analysis plan, not a ticket here. That split is not cosmetic:
`af-build` runs one worker per ticket in a single-repo worktree, and a check can only run against the
tree it was pinned to, so a ticket spanning both repos has no verifiable acceptance.

**Scope: this document governs how a model is found.** Survey, proposal, iteration, adjudication, and stopping.
It owns the `spec.yaml` schema's *semantics*, search policy, budgets, and metric definition. It does not say where a
file goes, what may import what, or what a payload looks like — that is the sports_analysis plan. The two meet at one
seam: a campaign folder holds four files, and a promoted result lands as one commit.

## 1. The phases

Five phases, run in order, plus one discipline that is not a phase because it gates every transition between them.

```
  SURVEY ──▶ SELECT ──▶ ITERATE ──▶ CONVERGE ──▶ PROMOTE
     │          │          │           │            │
     └──────────┴────ADJUDICATION──────┴────────────┘
            noise floor · sealed evaluation · author is not judge
```

A cold start runs all five. A warm start runs all five too — the incumbent simply enters SURVEY as a rung-0
candidate, so "is there something better than what we already have" is answered by the same comparison as
everything else.

### Phase 1 — Survey

 What already solves this, and how much of it can we reuse? Output is *working baselines measured on
our data*, not a document. Candidates are ranked by reuse level, and a lower rung requires a cited failure of the one
above it:

| rung | what it means | entry rule |
|---|---|---|
| 1 | existing weights work as-is | adapter only |
| 2 | existing implementation, fine-tuned on our data | pinned dependency |
| 3 | published work that **fully describes** a solution, built from the description | ancestry recorded: cite the paper |
| 4 | novel code | must cite the rung-3 result that failed |

Rungs 1–2 are existing code; rung 3 is an existing *description* complete enough to build from. Two admission
tests keep rung 3 honest, because a paper that fails either is rung 4 wearing rung 3's clothes:

- **Completeness.** Architecture, loss, and training recipe are all specified. If the schedule or the loss has to be
  invented, this is novel work with a citation attached — say so, and inherit rung 4's burden.
- **Setting match.** The claimed setting is ours, or the mismatch is named and argued. A method solving broadcast
  footage is not evidence about a fixed-rig panorama.

The failure mode both tests guard against is documented: NAS reproducibility studies where random search matched the
published method — the reimplementation was measuring something other than what the paper claimed.

Search surfaces: HuggingFace Hub, timm, torchvision, OpenMMLab, Ultralytics, BoxMOT. **Not** Papers-with-Code —
its API is dead: `paperswithcode.com/api/v1/papers/` returns HTTP 200 but serves the HuggingFace trending-papers
SPA, HTML rather than JSON.
Reused code never enters as an unmarked copy — the `vendor/botsort` fork is the counter-example. A rung-3 build
records the paper it came from in the module docstring, the same way a fork records its upstream commit.

This phase is where the **between-families** question is settled: is this a keypoint-plus-homography problem or a
direct-regression one? Ablation cannot answer that, because ablation only measures blocks inside a pipeline that
already exists. Several approaches are drafted and measured against each other and against the incumbent.

### Phase 2 — Select

Producing candidates is the easy half. Choosing among them is where the ladder either bites or
becomes decoration. The rule:

1. **Same protocol, stated budget.** Every candidate is measured on the same split with the same evaluation, under a
   declared budget policy — otherwise the comparison is about effort spent, not approach quality. A rung-1 adapter
   trains for zero minutes and a rung-4 build for an hour; that difference has to be a stated choice, not an accident.
2. **Floor first, then select.** Compute the noise floor before looking at the ranking. Choosing the maximum of N
   noisy measurements overstates the winner — with four candidates and an unstable metric, "best" can mean "luckiest".
3. **Everything within one floor of the top is tied.** Ties are not broken by the leaderboard.
4. **Ties resolve *down* the ladder.** Among tied candidates the lowest rung wins — least code. This is the rule that
   makes the ladder real; without it the shiniest result wins and reuse is advisory.
5. **Confirm the winner on a second seed** before it becomes champion.
6. **Carry at most one runner-up**, and only when it is a different family. The winner at low tuning is not
   guaranteed to be the winner at high tuning, so one alternative stays alive to convergence; everything else dies.

Selection happens on the search split. It never touches the sealed split — that is what the sealed split is for.

**The survey's second output: a technique pool.** Choosing a family is half of it. The same upfront pass also
gathers *modifications worth trying inside* that family — a deformable-attention variant where vanilla attention is
used, a gradient-boosted head where a forest is, a loss or a niche method that worked in an adjacent area. These are
ordinary arms when they run; what makes them a Phase 1 concern is that finding them requires searching the literature,
and the loop must not.

Two reasons the gathering is upfront rather than mid-run:

- **The loop stays offline.** Sandbox practice makes default-deny egress the single most important network control,
  and an agent that trains and evaluates must not have a network path. Doing all retrieval before the sealed loop
  starts means the loop never needs one. Network lives in this pass and nowhere else.
- **It is reviewable at hour zero.** The pool is an artifact a human can read before a six-hour run, rather than a
  mid-run search whose results nobody sees.

Each pool entry carries a **transferability triple**, and an entry without one is not queued:

1. what setting it was proven in,
2. how that setting differs from ours,
3. the mechanism by which it should still help here.

The third is the one that does the work. "It is standard practice" is not a mechanism — flip augmentation is standard
everywhere and cost this project 0.037 macro-F1, because label-preserving is not feature-preserving when side cues
carry signal. Fresh is also not the same as good: retrieval returns far more unvalidated technique than validated,
so the triple is the filter, not the ranking.

The pool is refreshed only on the stagnation trigger, in a network-enabled pass outside the loop — never per-arm.

### Phase 3 — Iterate

Two halves, alternating until the stagnation trigger fires.

**Propose.** Ideas are generated *and regenerated* from the survey's technique pool rather than from model priors:
ablation picks *where* to change, the pool supplies *what* to try there. Proposals are conditioned on what the
journal already shows, and re-planning fires on evidence, not on a schedule. The failure this guards against is a
large idea list authored up front and executed to exhaustion long after its premise stopped holding.

**Run.** One arm at a time from the champion. The design work here is the **operator set** — what an arm is allowed
to change — because operators matter more than search policy: a controlled comparison found evolutionary and
tree search gave no significant gain over greedy, while richer operators did.

### Phase 4 — Converge

The chosen shape and recipe, trained properly — no new ideas, no new arms, no new decisions. Ends when the run
finishes and is measured against the sealed split. If it fails to beat the shorter runs that preceded it, it is
simply not promoted and the earlier version stands; that is why converging late is the conservative order.

### Phase 5 — Promote

Sealed evaluation, compatibility load against HEAD, the `production` alias, and the single landing commit — the seam
into `2026-08-23-build-plan-sports-analysis.md`, whose §5.2 owns what that commit contains.

**The ordering matters and neither document owned it.** `registry_finalize` refuses to run if git HEAD
has moved, so the landing commit cannot precede it. The runner calls `finalize` first — sealed
evaluation, compat load, alias move — and writes the commit only after it returns. An earlier draft
claimed Praxis covered this phase entirely; it does not.

### Adjudication — not a phase, the gate on every transition

Every arrow in the diagram is an adjudication decision. Four rules, all of which are gaps against Praxis as built:

1. **The floor is computed before the comparison, never after.** Choosing the maximum of N noisy measurements
   overstates the winner. This is why the previous attempt failed: 34 trials adopted nothing because the metric was
   7.6× noisier than its sample size warranted, making two sigmas a 7-point bar no arm could clear. A campaign whose
   floor is not measured first has not started.
2. **The arm's author never writes its verdict, and cannot reach the labels.** Structural, not conventional: the
   scorer runs in a separate process, the sealed labels sit on a read-only mount outside the arm's writable tree, and
   the arm's only output is a predictions file — so no code path exists by which it can print or write its own score.
   Every surveyed engine except two lets the arm self-report, and adopt/reject on a zero threshold against one seed
   is the norm (`if best_improvement <= 0.0`). Neither is acceptable for an unattended run.
3. **Adopt means clearing the floor, not beating the champion.** Two-sided: `delta > floor` adopts,
   `delta < -floor` rejects, between them parks.
4. **Telemetry is what makes a six-hour run readable in five minutes.** Champion record with reproduction
   instructions; the full experiment log with each arm's delta and training diagnostics, not just its score; and a
   **dead-end registry** — tested axis, direction, performance change, rejection reason. Because rejected code is
   deleted, that registry is the only surviving trace of an arm, so it carries a diff summary. It is also what makes
   thirty rejections legible as "the bar is unclearable" rather than "the ideas were bad".


## 2. What we reuse, and what we build

**Decision: build on Praxis, borrow mechanisms, adopt no new runtime engine.** Every capability we want exists
somewhere, but spread across four repos under three incompatible licences, and no single adoption reaches the whole
loop. Depending on all four costs more setup than the ~2,000–2,800 lines the gaps represent.

### Reused as-is — Praxis `knowledge/ml_registry` (8,473 lines after the purge, 16,625 before)

| phase | status | mechanism |
|---|---|---|
| ITERATE | covered | `supervisor.py` serial one-arm dispatch, `staging.py` ordered stages, `ideate.py` idea seeding |
| PROMOTE | **partial** | `services/registry_finalize.py`, `registry_aliases.py`, capability sentinels in `storage/registry.py` verify and move the alias — but **nothing writes the landing commit, and `registry_finalize` aborts if git HEAD moves** (`:73, :105, :119, :171`). See §6.1. |

No MLflow. Praxis already is the registry; a second one would be two authorities for one question, and it addresses
none of the gaps below.

### Built here (~2,000–2,800 lines)

| gap | size | after component libraries |
|---|---|---|
| **SURVEY + SELECT** | ~700–1000, new modules | Praxis has no rung or reuse ladder (`family` is an opaque TEXT column) and every adjudication path is pairwise-against-champion — `adjudicate_against_champion` refuses without a `champion` alias, so no N-way comparator exists. The *statistics* are free (below); the tie-to-promote policy, the ladder, and seed budgeting are not. |
| **Sealed-evaluation enforcement** | **~50–100**, was ~400–700 | Collapsed. See below — the containment is an OS feature already on this machine. What remains is the seal protocol itself: which split is sealed when, and who holds the predictions. |
| **Stagnation stop, technique pool, hour-6 telemetry** | ~350–500 | Storage and viewer are free (`trackio`). The stagnation rule, the dead-end novelty check, and turning a retrieved technique into a runnable diff are not. |

Revised total: **~1,100–1,600 lines**, plus **~390** for the registration gates in §5 that the campaign
stress-test forced — call it **~1,500–2,000**. Still well under the original ~2,000–2,800, because the containment
collapse (Gap 2) more than paid for the gates.

### Component libraries — verified, and they change the estimate

- **Containment: `sandbox-exec` (macOS Seatbelt), zero install.** Verified by execution on this machine
  (`/usr/bin/sandbox-exec`, binary dated 2026-06-24, Darwin 25.5.0): a four-line profile made the sealed label file
  unreadable via both `/tmp` and `/private/tmp` (`PermissionError`), denied all network (`NET DENIED`, `HTTP
  DENIED`), and still permitted writes to the predictions directory. No Docker needed — the Docker CLI is present
  but its daemon is not running, and colima, OrbStack, podman and limactl are all absent. This is the single
  biggest correction: sealed evaluation was sized as the second-largest build item and is mostly an OS feature.

  **Hazard, reproduced here: Seatbelt rules must use the canonical realpath, and fail *open* silently when they do
  not.** A profile denying `/var/folders/.../sealed.txt` blocks nothing, because the file resolves through
  `/private/var/folders/...` — while network-deny and predictions-write keep working, so the sandbox looks healthy.
  With `os.path.realpath` in the profile, both path forms raise `PermissionError`. The seal protocol therefore opens
  every run with a **positive control**: attempt to read the sealed file inside the sandbox and refuse to start the
  campaign unless that read raises. A seal that is only assumed is not a seal.
- **Selection statistics: `baycomp`, and possibly `deepsig`.** Both are in *our* setting — repeated seeds on one
  dataset — rather than the Demšar many-datasets setting that `autorank` and `critdd` assume (`critdd` is not even
  on PyPI). `baycomp.two_on_single` / `CorrelatedTTest` takes a `rope=` region and returns a literal **probability
  of a tie**, which is exactly Phase 2's "within one floor of the top is tied" rule with a principled threshold
  instead of a hand-rolled one. `deepsig.multi_aso` gives an N-model ε_min matrix for the N-way case, but it is
  GPL-3.0 (confirmed on PyPI, 1.2.8) and has had no release since 2023. Verified directly: `trackio` 0.36.0 and
  `baycomp` 1.0.3 install from PyPI; `critdd` 404s.
- **Telemetry: `trackio`.** MIT, `pip install trackio`, SQLite-backed, `trackio show` viewer, no account, no server,
  active as of 2026-08-21. Covers the champion record and experiment log. `guildai` is dead (no release since
  2023-02), `sacred` requires MongoDB, `neptune` requires an account.
- **Retrieval: barely collapses.** OpenAlex now runs a credit-priced limiter (~100 free full-text searches per
  window at 10 credits each); Semantic Scholar returned HTTP 429 unauthenticated on both attempts, so a free API
  key is effectively mandatory. Either way you get a *search client* and nothing resembling technique extraction.

### What still has to be hand-written

The tie-to-promote threshold policy; seed budgeting; turning a retrieved technique into a runnable diff; the
dead-end novelty check; the seal protocol (as distinct from the sandbox that enforces it); the champion/challenger
state machine; and unattended-run resilience. The math, the storage, and the containment are free — the *policy* is
not, and policy is where the previous attempt failed.

### Mechanisms lifted, not depended on

Each of these is a design we copy; none becomes a dependency.

- **Ablation-conditioned refinement** (MLE-STAR's `sub_agents/refinement/` chain) — the mechanism behind Phase 3's
  "ablation picks where, the pool supplies what". *Do not adopt the package*: it was deleted from
  `google/adk-samples` on 2026-07-23 and is Gemini/Vertex-locked.
- **Metrics the proposer cannot read** (ShinkaEvolve's `private_metrics`) — Phase 2 and adjudication depend on this,
  and it is the industry's soft spot: every other engine lets the arm print its own score.
- **Literature retrieval for Phase 1** (AI-Scientist-v2's `semantic_scholar.py`) and **multi-seed evaluation**
  (`multi_seed_eval.num_seeds: 3`). Licence is use-restricted, so the design transfers and the code does not.
- **A structured idea pool as input** (RD-Agent's `DS_IDEA_POOL_JSON_PATH` / `DSIdea` schema) is the closest thing to
  our technique pool, but RD-Agent is Linux-only — fatal on this machine.

Two findings worth keeping because they contradict what is widely assumed:

- **MLE-STAR really does retrieve from the web** — *"first leverages external knowledge by using a search engine to
  retrieve effective models"*, implemented as `model_retriever_agent(..., tools=[google_search])`. But it fires once
  at t=0 and returns *models*, not techniques; its refinement loop retrieves nothing. **RD-Agent's "R" step does not
  retrieve** — it works from priors and implements PDFs handed to it. AIDE retrieves nothing at all.
- **Nobody covers CONVERGE or PROMOTE.** Those are ours in every scenario, whichever engine is chosen.

### Two Praxis behaviours that contradict this design

Decisions, not bugs — they need a ruling before build starts.

1. **`floor.py` refuses a second-seed confirmation run** (`:48`, `:804`) on the deliberate principle that
   deterministic repeats never become additional samples. Phase 2's "confirm the winner on a second seed" rule,
   taken from AutoScientists' noise-aware promotion gate, cannot be implemented without overriding that stance.
2. **`floor.py` has no cold-start path** — a floor requires ≥4 repeats of an existing baseline commit. Cold-start
   campaigns therefore cannot compute a floor until Phase 1 produces the first champion, which is consistent with
   §3 but means Phase 2 selection for a cold start runs against a floor derived from candidates rather than an
   incumbent.

## 3. Start regimes

Both are supported and adjudicate differently.

- **Cold start** — no incumbent. No champion means no floor to compute, so `delta > floor` is undefined; the win
  condition is beating a trivial or deterministic baseline. Phase 1 produces the first champion.
- **Warm start** — an incumbent exists. The floor is computable from repeated champion runs, arms are deltas, and
  adopt means `delta > floor`.

The transition point (when a cold-start campaign begins behaving like a warm one) is a decision, not a derivation,
and belongs in the spec.

## 4. Open questions

Resolved by the evidence and recorded above: which engine (none — build on Praxis, lift mechanisms); whether a
runner-up approach survives (yes, at most one, of a different family); whether the tools retrieve literature (only
MLE-STAR, once, for models not techniques); the `floor.py` second-seed conflict (no code to override — 13 lines of
prose); metric stability and label availability (now §5.1 and §5.7, executable gates rather than open questions);
and containment on macOS (`sandbox-exec`, verified by execution, with the realpath fail-open hazard named in §2).

Still open:

- **Who writes `families` into a spec** — carried to §6.6.

Evidence: `docs/ideation/2026-08-23-automl-prior-art-ideation.html` and the dossiers it cites.

## 5. Registration gates

Stress-testing the design against eight drafted campaign specs found that **none could be run to a verdict**
unmodified: one had an incumbent, none had a noise floor with live provenance, seven declared zero material stages,
seven could never promote, three had no sealed partition, three had no training corpus. Every fix below is generic —
no campaign gets a special case, and three of the eight are *refused* by these gates rather than accommodated, which
is the evidence the gates belong here.

Nothing deleted from Praxis comes back. Each mechanism is smaller than the code that was removed, and in two cases
the removed code was the wrong answer: the historical importer is what produced the inherited floors whose
provenance cannot be verified, and a derived dependency DAG is far more than is needed to pin one artifact version.

### 5.1 The metric contract (~150 lines) — the gate R0 was missing

R0 as first written was a table in a document. A table does not stop a campaign from registering. The metric
contract is the executable form, checked at registration, refusing the campaign otherwise:

- **The operating point is frozen at declaration.** A metric that re-selects its own threshold per arm — a
  recall-at-fixed-precision, any constrained argmax — measures a different quantity each time it runs. This is the
  defect behind the 34-trial failure, and it appeared in two of the eight specs. Freeze the threshold in the spec,
  or the metric is refused.
- **Effective sample size is checked at every aggregation level, not just the top.** A three-level nested macro over
  three scenes, or a macro-average over two sources, reports a number whose variance no one has looked at. Each
  level declares its minimum; each is checked.
- **The result is a scalar, and its direction agrees with the win condition.** A distribution over zones is not
  something the adjudicator can compare.

### 5.2 The rope comes from the data, not from run repeats (~80 lines)

Replace champion-run-repeats as the source of the comparison threshold with a **bootstrap of the metric over the
scoring corpus's own `split_unit`** — which four of the eight specs already declare. One change, three fixes:

- **Cold start stops being a special case.** A floor no longer requires a champion to repeat, so a campaign with no
  incumbent can compare candidates from its first measurement. The SELECT phase's tie test becomes defined at the
  moment it is first needed.
- **Deterministic incumbents stop refusing.** A deterministic arm yields σ=0 and a zero floor, which the existing
  implementation rejects outright — precisely where a baseline is most likely to be deterministic.
- **Inherited floors become recomputable rather than inherited.** Four specs carry floors whose only provenance is
  an importer that no longer exists. Under this rule they are not restored, they are re-measured.

### 5.3 Campaigns terminate in an outcome, not only in a promotion (~40 lines)

Seven of the eight specs can never promote — their value is a measurement, not a model. With PROMOTE as the only
exit, they cannot finish. Every campaign closes on a terminal **outcome record**:

`PROMOTED` (an alias moved) · `MEASURED` (the question was answered; no model was authorised) · `REFUTED` (the idea
was tried and lost) · `ABANDONED` (stopped without an answer, with the reason).

This also reconciles the two definitions of "done" the plan carried unnoticed: the spec's `win_condition` and the
stagnation trigger. The win condition decides *which* outcome; the stagnation trigger decides *when* to stop
looking.

### 5.4 Stage outcomes (~30 lines)

A stage closes as `ADVANCED` (an arm cleared the rope), `STAGNANT` (arms ran, none cleared), or `VACUOUS` (no arms
existed to run). Seven specs declare stages with no material families, and without this vocabulary "we skipped it"
and "there was nothing to try" are the same state — which is exactly the distinction a stagnation rule needs.

### 5.5 Upstream artifacts are pinned, not looked up (~30 lines)

A campaign that consumes another's output names the producing campaign and artifact type in `requires`; the runner
resolves it to a **concrete version at claim time** and records that version in the run. Two of the eight specs
consume upstream detections with nothing pinning the version, and two more name `oof_for` ids that do not exist —
so a re-run of the producer silently changes the consumer's inputs. Resolution failure refuses the claim.

### 5.6 Disposition consistency (~20 lines)

A spec that declares it produces no weights while also emitting a checkpoint is internally contradictory, and one of
the eight is exactly that — `measurement_only_no_weights` alongside `learned_escalation: true` and a checkpoint
artifact, trained on one corpus and scored on another. This is not a licence rule and makes no claim about law: it
checks the spec against itself, and refuses a spec that disagrees with itself.

### 5.7 Data readiness (~40 lines)

Every corpus a spec declares must actually load at registration, or the campaign is refused rather than started.
Several of the eight are recorded as launch-blocked or annotations-only; an engine cannot discover a model for a
metric with no data behind it, and finding that out at hour three is worse than finding it out at registration.


## 6. Running the whole thing unattended

The five phases describe one campaign. What follows is what turns a set of campaigns into a build you can trigger
and walk away from.

### 6.1 The runner (~80 lines)

`supervise_campaign` takes a single `model_id`, and the multi-campaign controller was deleted in the purge — of its
950 lines the audit measured ~66 as surviving, the rest being the dependency DAG, retry/backoff, cost admission and
lease plumbing this design drops. It is not restored. What replaces it is a loop: read the registered campaigns,
pick the next eligible one, run it to a terminal outcome (§5.3), move on. `max_active` is a plain integer the runner honours; the deleted controller's
compatibility predicate is not reintroduced.

**A campaign refused at registration is skipped and reported, never fatal.** One bad spec must not cost a night's
run; the refusal and its reason land in the run report, and the loop continues.

### 6.2 Resume by re-derivation, not by journal (~40 lines)

The deleted runtime tracked attempts in files of its own. Do not restore that. The registry already records what was
claimed, dispatched, and adjudicated, so **resume re-derives position from the registry** rather than keeping a
second journal that can disagree with the first. Restarting after a fix picks up at the last adjudicated arm; an arm
that was in flight when the process died is re-dispatched, because a claim with no verdict is not an answer.

Resume depends entirely on the registry surviving a kill, so it is opened in WAL mode and every verdict
is committed before the next arm is dispatched. A registry corrupted mid-write loses the run, and no
amount of re-derivation recovers it.

### 6.3 The operator surface — restored, not written

`report.py` is back (`campaign-status` verb): campaign status in one read-only pass, plus `diagnose`,
`acknowledge_diagnosis`, and `idea_verdicts`. It was deleted as test-only, which was true of its reachability and
false about its worth — the verb that called it had already been removed by an earlier pass. Its docstring records
why it exists: *"a stage silently wedged"* went unnoticed during the first real campaign, and that is exactly the
stuck-versus-slow distinction an unattended run needs. **The purge's one real mistake was observability, not
mechanism** — worth remembering the next time a delete pass runs.

`trackio` (R2) holds the per-arm record; `campaign-status` answers "how is it going". Those are different questions
and both are needed.

### 6.4 Containment — what the seal does not cover

The seal in R1 governs the **training run**: sealed labels unreadable, no egress, writes confined to a
predictions directory. Three things sit outside it, and a gap-lens pass found neither document mentions
any of them.

**The proposer is not sandboxed at all.** The agent that *writes* arm code has repo write access, the
network, the full environment, and hours unattended — while only the code it produces is confined. It
runs under its own profile: writable paths limited to the arm worktree, no access to `data/files/` or
the sealed split, and no ability to modify the harness that will score it. An arm that can edit its own
scorer has no seal at all.

**The technique pool is an injection surface.** Phase 1 retrieves from the open web and Phase 3 feeds
that text to a code-writing agent. The transferability triple is a *quality* filter — proven where, how
it differs, why it should still help — and does nothing about a retrieved abstract carrying
instructions. Pool entries are data: they are stored as quoted text with their source URL, never
interpolated into a prompt as instruction, and an entry whose text contains imperative directives at
the proposer is dropped with the reason recorded.

**The positive control proves one leg of three.** R1 asserts the sealed file is unreadable. It does not
assert that egress is denied or that writes outside the predictions directory fail — and the fail-open
this project already hit was exactly that asymmetry: a profile built from a non-canonical path blocked
nothing while network-deny still worked, so it looked healthy. The control exercises **all three legs**
at every campaign start, and any leg that does not raise refuses the launch.

### 6.5 Liveness — the deadlock nobody bounded

A run that hangs never finishes, so it never increments the stagnation counter, so R6 can never fire.
Three bounds, all enforced by the runner rather than by the arm:

- **A per-arm wall-clock cap.** An arm exceeding it is killed and recorded VOIDED on throughput, which
  *does* advance the counter. The budget number matters less than the fact that a hang terminates.
- **A heartbeat.** An arm that stops reporting progress for longer than its declared cadence is treated
  as hung, killed, and voided — a process that is alive but wedged is indistinguishable from a crash
  without one.
- **A disk budget per campaign.** Checkpoints accumulate; a full disk fails every subsequent arm for a
  reason nothing in the loop reports as a data problem.

### 6.6 Undoing things

- **`unpromote` is one operation.** Reverting the landing commit does not move the `production` alias
  back, so a bare revert leaves the repo and the registry disagreeing silently. Promotion and its undo
  are each atomic: alias and commit move together, or neither does.
- **A registered campaign can be de-registered.** A spec that passed the gates and should not have is
  currently permanent, which strands the runner. De-registration closes it `ABANDONED` with a reason.
- **`campaign_state/` deletion is guarded.** It holds the registry and the arm worktrees; deleting it
  before the landing commit destroys the run. The runner deletes it, after the commit exists, and never
  by hand.
- **The dead-end registry and the rejected-arm diff blobs live outside `campaign_state/`.** They are
  described as an arm's only surviving trace, and a trace stored in the directory that gets deleted is
  not a trace. They go to the `trackio` store, which persists.

### 6.7 Build order: the sports_analysis plan finishes first

**No interleaving.** `2026-08-23-build-plan-sports-analysis.md` runs T1 through T9 to completion and
green, then this plan starts at R0. An earlier draft interleaved them; that was wrong twice over — R0's
registration gates need corpora that load through one interface (T2/T2b), and R1 has no campaign folder
to seal until T1 has built the tree.

Sequencing this way also dissolves a defect the seam review found: R0 could not meet its acceptance when
scheduled early, because it needed specs to gate. Those specs are now authored in the other repo (T9),
before any engine ticket starts, so R0 has a real set to run against on its first pass.

What the sports_analysis plan hands over: a tree with `contracts/`, `data/`, `shared/`, and `sports/` in
place; a registered `DataSource` for every corpus a campaign consumes, each verified to load and to
expose both a fittable and a scoreable partition; the spine registered in Praxis with `champion` and
`production` aliases; and boundary tests that keep the shape honest; a structural spec validator R0 composes (T7); a landing-commit
writer and its inverse that R7 and R8 call (T8); and an authored campaign set (T9). R0 begins against that.

One thing crosses back: `docs/RUNNING.md` in the other repo (T5) documents only what exists at handover —
the checks, the catalog, how to add a corpus. It gains the runner section after R8, as a documentation
change in that repo rather than a ticket here.

### 6.8 Authoring the campaign set — the other repo's job

The eight drafted specs do not survive §5's gates as written: three should be refused outright, and the
rest need a frozen operating point, a declared `split_unit`, an explicit disposition, and pinned upstream
artifacts. Rewriting them is **T9 of the sports_analysis plan**, not a ticket here — a spec is a file in
that tree, and the engine stays ignorant of what any campaign measures. This document only specifies what
a spec must satisfy (§5); it never authors one.

### 6.9 Still open

**Nobody owns writing `families` into a spec.** The ladder needs an operator set per stage; the technique pool
(Phase 1) is the obvious source; the binding between them is unspecified. This is the last open design question in
this document.


## 7. Tickets, in order

Each ends with its acceptance criteria met and the existing suites green, **entirely within the `praxis`
repo**. Sizes are the build estimate from §2, reduced from earlier drafts where a ticket's repo-side half
moved to the sports_analysis plan.

**R0 — The policy gate (~300 lines).** Not a document — the executable *policy* gates of §5, run before a
campaign may register: the metric contract (§5.1), disposition consistency (§5.6), and data readiness
(§5.7). Plus the measurement R0 originally called for, now as an input to §5.2's rope rather than a
table: for each campaign, the metric's bootstrap over its scoring corpus's `split_unit`.
**Structural validation is not here.** Whether the spec parses, carries its required keys, matches its
folder, and names resolvable corpora is T7 in the sports_analysis repo; R0 *composes* that validator as
a library call and adds policy on top of it. The gate refuses if either half refuses.
Acceptance: a spec whose metric re-selects its threshold per arm is refused, naming the field; a spec whose
aggregation bottoms out below its declared minimum sample at any level is refused, naming the level; a spec that
declares no weights while emitting a checkpoint is refused; a structural refusal from T7 is surfaced verbatim
rather than restated; and each refusal names what would make it pass. Run against the T9-authored set, at
least one is refused — that is the gate working, not a bug. **This gates every ticket below.**

**R1 — The seal (~50–100 lines).** A Seatbelt profile built from `os.path.realpath`, denying reads of the sealed
split and all network, permitting writes only to a predictions directory. The orchestrator scores predictions in a
separate process outside the sandbox; the arm's only output is a predictions file.
Acceptance: (a) a **three-legged positive control** runs at campaign start — reading the sealed file must
raise, an egress attempt must fail, and a write outside the predictions directory must fail; any leg that
does not raise refuses the launch (§6.4); (b) a test asserts the profile is rejected when built from a
non-canonical path, since that combination fails open silently; (c) an arm that attempts network egress fails; (d)
an arm cannot write anywhere but the predictions directory; (e) no code path exists by which an arm reports its own
score.

**R2 — Telemetry (~150 lines over `trackio`; the status surface is already restored).** `report.py` and the
`campaign-status` verb are back in Praxis (commit `8cfb9fd0`) — that half of §6.3 needs no work. This ticket adds
the per-arm record. `pip install trackio` (0.36.0, MIT, SQLite, no server). Wire the
three records: champion (hyperparameters plus reproduction instructions), experiment log (every arm, metric delta,
training diagnostics), and the **dead-end registry** (tested axis, direction, performance change, rejection reason,
diff summary — the only surviving trace of a deleted arm).
Acceptance: after a fixture run of ≥5 arms, `trackio show` renders all three; a reader can tell within five minutes
whether a result is real; every rejected arm appears in the dead-end registry with a reason.

**R3 — Selection (~400–600 lines).** `pip install baycomp` (1.0.3). Implement Phase 2: the reuse-rung ladder as a
first-class field (Praxis's `family` is an opaque TEXT column), the N-way comparison, and the tie policy.
**The stored `noise_floor` field is retired in the same change.** `verdict.py:301-304` adopts against a
value stored at registration; the rope is computed per comparison. Leaving both live means two thresholds
decide one verdict. The rope wins and the field goes.
**The rope comes from §5.2** — a bootstrap of the metric over the scoring corpus's `split_unit`, not from champion
run repeats — so `baycomp.two_on_single` / `CorrelatedTTest` gets a principled `rope=` even with no incumbent. Ties
resolve down the ladder. Also lands the terminal outcome record (§5.3), stage outcomes (§5.4), and artifact pinning
(§5.5).
The plan's old open question here is closed: `floor.py`'s "refusal" of second-seed confirmation is 13 lines of
prose with no enforcing code, and its cold-start path is 0 lines. There is nothing to override and nothing to
preserve.
Acceptance: a fixture of N candidates with known σ selects correctly; two inside the rope report tied and resolve to
the lower rung; a deterministic candidate (σ=0) does not refuse; a cold-start campaign with no champion computes a
rope and selects; a campaign that cannot promote closes `MEASURED` rather than hanging; a stage with no material
families closes `VACUOUS`; a consumer whose upstream artifact version cannot be resolved refuses its claim.

**R4 — Survey (~300–400 lines).** A retrieval client (OpenAlex or Semantic Scholar; the latter 429s unauthenticated,
so obtain a free key — and note OpenAlex meters full-text search at ~100 free queries per window). Produce the
technique pool: entries carrying the transferability triple, refusing any entry that lacks one. Plus the harness
that reproduces N candidate baselines on our data.
Acceptance: a campaign yields ≥3 measured baselines from different families plus a pool of ≥10 techniques, each with
all three triple fields; an entry missing a mechanism is rejected by the loader, not by review.

**R5 — Iterate (~200 lines over Praxis).** Feed the pool into the `IdeaGenerator` seam (`supervisor.py:185`, not `ideate.py` as earlier drafts said) so proposals draw
from vetted techniques rather than model priors, conditioned on ablation results. Praxis's `supervisor.py` and
`staging.py` already own dispatch and stages; this is a wiring ticket.
Acceptance: proposals cite pool entries by id; an ablation result changes which block subsequent proposals target;
no proposal originates outside the pool without being recorded as such.

**R6 — Stagnation stop (~100 lines).** No improvement in the last 10 experiments closes the stage and triggers a
pool refresh in a network-enabled pass outside the loop. Praxis's watchdogs detect axis fixation only.
Acceptance: a fixture run with a deliberately unclearable bar stops at 10 and reports why, rather than exhausting
its budget.

**R7 — Converge and promote (~80 lines of wiring).** Phase 4 runs the chosen shape at full length; Phase 5
is `registry_finalize.py` and `registry_aliases.py` moving the `champion` and `production` aliases, and
then **calling** the sports_analysis-side landing-commit writer (T8) — this ticket does not write the
commit or `RESULT.md` itself, and owns no git operation in that tree. Alias and commit move together or
neither does, which this ticket enforces by treating a failed T8 call as a failed promotion.
Acceptance: an end-to-end fixture campaign runs R1–R6, moves both aliases, and reports the commit the T8
call produced; a T8 failure leaves both aliases unmoved; a converge run that fails to beat its
predecessor leaves the earlier version in `production`.

**R8 — The runner and resume (~120 lines).** §6.1 and §6.2: a loop over registered campaigns that runs each to a
terminal outcome, skipping and reporting a refused registration rather than halting; resume re-derives position
from the registry, with an in-flight claim carrying no verdict re-dispatched.
Also lands the §6.5 liveness bounds (per-arm wall clock, heartbeat, disk budget) and the §6.6 undo
operations: de-registration, guarded `campaign_state/` deletion, and `unpromote` — which reverts the
aliases here and calls T8's inverse for the commit, the same paired-or-neither rule as R7.
Acceptance: a fixture portfolio of four campaigns — one of which is refused at registration — runs unattended to
four terminal outcomes; an arm that hangs is killed, voided, and advances the stagnation counter; an arm
that stops heart-beating is treated the same; `unpromote` returns both the commit and the alias together; killing the process mid-arm and restarting resumes at the last adjudicated arm with no
duplicate verdicts; `campaign-status` distinguishes a wedged stage from a slow one.

## 8. Definition of done

Every campaign authored by T9 passes the §5 gates — metric contract, disposition consistency, data readiness — and
carries a rope derived from its own scoring corpus. A fixture campaign runs unattended end to end —
survey, select, iterate, converge, promote — with the seal's positive control passing at start, all three telemetry
records populated, and one landing commit. Then the real thing: **the R8 runner is triggered once and every registered campaign
reaches a terminal outcome unattended** — promoted, measured, refuted, or abandoned with a reason — with
`campaign-status` legible throughout and the promoted results landed by T8 in the tree the sports_analysis plan describes.
No new runtime engine and no second registry: Praxis plus `sandbox-exec`, `trackio`, `baycomp`, and one retrieval
client.
