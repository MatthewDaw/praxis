# Requirements: the `sports_analysis` build

Hand-off doc for `af-intake-plan`. Source of behavioral truth:
`docs/2026-08-23-build-plan-sports-analysis.md` (593 lines, T1–T9). This document does **not** replace
that plan — it is the exhausted, challenged, re-sized requirement set extracted from it, plus every
defect the rigorous pass found. Where the two disagree, this document is the later word and the plan
should be corrected to match before extraction.

**Rigor mode: Rigorous. Decision mode: Autonomous (defaults taken, flagged for override in §7).**

---

## 1. Scope

**In.** Everything the `sports_analysis` repo must hold before the Praxis engine plan
(`docs/2026-08-23-build-plan-praxis.md`) can start at R0:

- a tree where every file has one obvious home (`shared/`, `sports/<sport>/`, `contracts/`, `data/`,
  `runtime/`, `analysis/`, `campaigns/`)
- strict frozen pydantic contracts as the only shapes crossing a module boundary
- one `DataSource` template, and a registered adapter for **every input corpus in the bucket** — not
  only the ones a campaign names today
- the current spine registered in Praxis with `champion`/`production` aliases
- boundary and dead-code tests that keep the shape honest mechanically
- the three cross-repo call boundaries the engine consumes: structural spec validation, `DataSource`
  resolution, and the landing-commit writer with its inverse
- an authored campaign spec set

**Out.** How a model is discovered, search policy, adjudication, budgets, metric semantics, the runner,
telemetry, the sandbox — all Praxis, all after this plan closes. Also out: acquiring new data (the
catalog proved the bucket already holds what the campaigns need), and any campaign-specific code.

**Actors.**

| actor | what they do | what this plan owes them |
|---|---|---|
| the build worker (af-build) | takes one ticket red-to-green in one worktree, one ~80-min round | a ticket that fits a round and has a mechanically checkable acceptance |
| the person adding a corpus | writes a leaf adapter | a format base that already implements the hooks; a conformance suite that runs on registration |
| the person adding a sport | adds `sports/<sport>/` | the two-consumer rule, and the rename obligation priced in |
| the engine (Praxis) | calls across the seam | three stable library entry points, not CLIs |
| the reader | opens `git log` or the tree cold | one commit per campaign; no `_v2`; no dead code |
| CI | re-runs compat loads | `make check` and `make check-real` as the whole gate |

---

## 2. Feasibility: claims in the plan that are FALSE or STALE

Verified against the tree at commit `58e3f86d`, by command, not by reading the plan's prose. Each of
these changes a ticket's scope, so each must be fixed **in the plan** before extraction.

| # | plan claim | verdict | evidence |
|---|---|---|---|
| F1 | T1 deletes `pipeline/governed_slice.py` | **STALE** — does not exist | `find src -name governed_slice.py` → empty |
| F2 | T1 deletes `tracking_bench.py` | **STALE** — does not exist | `find src -name tracking_bench.py` → empty |
| F3 | T1 fixes `court_eval`'s source-map default "from the deleted `spikes/artifacts/`" | **FALSE on both halves** — `spikes/` exists at repo root, and `court_eval` already resolves through `runtime.paths.artifacts_dir()`, not a literal path | `ls -d spikes` → exists; `court_eval.py:160,214,237` all call `artifacts_dir()`; `paths.py:123` docstring: *"``spikes/artifacts/`` — the rendered evidence"* |
| F4 | the target tree treats `data/` as a **new Python package** (`data/__init__.py`, `source.py`, `registry.py`, `adapters/`, `files/`) | **COLLISION** — `data/` already exists at repo root, is **5.3 GB**, holds **149 tracked files** and **31 loose JSON manifests** at its top level, and is **not** a package | `du -sh data` → 5.3G; `git ls-files data \| wc -l` → 149 |
| F5 | the target tree's `docs/` line names `2026-08-23-ml-campaign-management-on-minimal-tree-plan.md` | **STALE** — that file was deleted when the plan was split | plan line 98 |
| F6 | "`positioning.py` alone carries 60 court references" | **STALE (58)** — immaterial, but the number is asserted and will be checked | `grep -ci court positioning.py` → 58 |
| F7 | the BoT-SORT tree is "8,219-line, 50-module" | **STALE (8,236 / 50)** — module count right, line count off by 17 | `find … -name '*.py' \| wc -l` → 50; `cat … \| wc -l` → 8236 |
| F8 | the tracker tree is reached through **one** import | **TRUE** | only hit outside the family: `tracking_spine.py:443` |
| F9 | `jersey_tagging.py` "no longer contains any jersey code" | **TRUE** — 106 lines holding `DEFAULT_MAX_REACQUISITION_COURT_WIDTHS_PER_SECOND`, `DEFAULT_COURT_SHAPE`, `DEFAULT_FPS`, `fps_from_timestamps`; the four `jersey`/`ocr` hits are prose | file read |
| F10 | `pydantic` is not a dependency today | **TRUE** | `grep pydantic pyproject.toml` → none |
| F11 | "Today's `contracts/` holds plain dataclasses (`geometry.py`, `predictions.py`, `failure.py`)" | **INCOMPLETE** — it also holds `artifacts.py` and `axes.py`, and **neither appears anywhere in the target tree** | `ls contracts/` |
| F12 | T1's acceptance numbers (`0.709` / corner `1.2°` / median `0.22 ft` on GAME5) | **PARTIALLY GROUNDED** — `0.709` appears in `court_fitness.py:93` as a committed table row for `al6PvGIEhR3kX9kvIsOv`; the corner and median figures are asserted nowhere in the tree | `grep -rn '0\.709'` |
| F13 | `campaign_state/` is disposable | **TRUE but loaded** — untracked (0 tracked files), and it currently holds `ml_registry`, `A01-DET`, `A01-FOOT`, `A02-MARKINGS-PIXEL-ARM`, `evidence`, `cache`. Deleting it destroys the live registry | `ls campaign_state` |
| F14 | `contracts/artifacts.py` exists to avoid importing `sports_analysis.experimentation.*` | **DEAD RATIONALE** — there is no `experimentation` package any more | `ls src/sports_analysis/` → `production`, `registry_root.py` only |

**F4 is the one that changes the most work.** The plan's `data/` package and the repo's existing 5.3 GB
`data/` directory cannot both be `data/`. Three of the 31 loose files at `data/` top level
(`dataset_registry.json`, `hudl_cv_registry.json`, `a01_det_source_inventory.v1.json`) are precisely the
ad-hoc registries the `DataSource` template is meant to replace, and the plan assigns them no
destination.

---

## 3. Contradictions inside the plan

Each is a place two sections disagree, so an extractor would mint two conflicting requirements.

- **C1 — four public methods or five.** §4 Rule 1 (line 193): *"`describe()`, `load()`,
  `split(manifest)`, `sample(partition, n, *, seed)` — and those **four** are final."* §4.1's code block
  defines **five** (`payload()` as well), and T2's acceptance (line 420) says *"the **five** final public
  methods."* `payload()` is the only way bytes leave a source, so five is right and Rule 1 is wrong.
- **C2 — where the campaign seam types live.** Three different files are named for the same types:
  `contracts/campaign.py` (line 92, line 305, line 549), `contracts/run_context.py` (line 313), and
  `contracts/campaign_spec.py` (line 330). One of them.
- **C3 — the stale doc name in the tree** (F5): the tree diagram still lists the pre-split plan file.
- **C4 — `shared/` may or may not survive.** T1's acceptance (line 416) reads *"`shared/` holds more than
  `models/` **or the level is deleted**"* — that is an unresolved architectural fork written as a pass
  condition. A build worker cannot satisfy an acceptance that permits two different trees. See §7 D1.
- **C5 — T2b's `data/files/` scope. CLOSED by D2** (stream-and-cache; `data/cache/` added to the tree). §4.1 says `data/files/` *"only ever holds inputs"*, and T2b scopes
  itself to inputs only. But S3-sourced corpora at 1,092.8 GB cannot live in `data/files/` (committed
  artifacts). The plan never says where a corpus's **bytes** are when an adapter reads them — local
  cache, S3 streaming, or a required prior download — now settled as stream-and-cache with cleanup at
  campaign close.

---

## 4. Ticket sizing: the plan's ten tickets do not fit ten build rounds

af-build's hard bound: one ticket goes red-to-green in **one worktree, one ~80-minute round**, or it
times out and strands partial work on a branch — a recoverable-only-by-orphan-branch-landing incident.
Judged against that, **four of the ten tickets will strand.** The re-sized set is 19 tickets.

| plan ticket | verdict | why | split into |
|---|---|---|---|
| **T1** | **TOO BIG — will strand** | six unrelated jobs in one round: delete 9 modules, add pydantic, convert `contracts/` to strict, move the whole tree, establish `shared/`, restate the re-acquisition gate over neutral coordinates, fix `court_eval` | **T1a–T1d** below |
| **T2** | **FITS** | template + registry + its first adapter is exactly the "mechanism and the thing that feeds it" unit the sizing rule says not to split | — |
| **T2b** | **TOO BIG by an order of magnitude** | ~5 format bases + ~117 leaf adapters + verifying each actually decodes real media. Its own text already contains the split seam | **T2b-1 … T2b-6** below |
| **T3** | **FITS** | four registrations against an existing registry | — |
| **T4** | **TOO BIG** | ~25 distinct boundary assertions in four unrelated groups, each needing debugging against a freshly-moved tree | **T4a–T4d** below |
| **T5** | **TOO SMALL** | one markdown page | merge into **T9** as its final step |
| **T6** | **TOO BIG** | a 50-module move *plus* pruning an 8,236-line tree to its reachable set *plus* restating `positioning.py` as `postprocess.py` | **T6a–T6b** below |
| **T7** | **FITS** | one validator, one refusal type, one test file | — |
| **T8** | **FITS (tight)** | commit writer + `RESULT.md` + the inverse + idempotence | — |
| **T9** | **FITS** | ~8 spec files, no code | — |

### The re-sized set (19 tickets)

**T1a — Delete the dead modules.** `contracts/governance.py`, `runtime/{catalog,acquire,dataset_registry,gpu_slots,selection}.py`,
`production/promotion.py`, `registry_root.py`, `court_film.py`, `court_zones.py`. Drop `governed_slice.py`
and `tracking_bench.py` from the list — they are already gone (F1, F2). Also decide `contracts/artifacts.py`
and `contracts/axes.py`, whose only stated rationale is dead (F14).
*Acceptance: each deletion is preceded by a recorded check that no real-footage test imports it; `make
check` and `make check-real` green; the orphan-module count does not increase.*

**T1b — pydantic, pinned, and the `Contract` base.** Add the dependency; write `Contract`
(`frozen`/`extra=forbid`/`strict`/`validate_assignment`); convert `geometry.py`, `predictions.py`,
`failure.py`. **This is the riskiest ticket in the plan** and it gets its own round precisely because
`strict=True` is where every sloppy numeric coercion at today's boundaries surfaces at once.
*Acceptance: the real-data regression suite passes bit-for-bit with `strict=True` on; every `contracts/`
class inherits `Contract`; the ndarray exception is implemented as a declared dump (checksum + shape in
JSON, bytes in the blob store) and tested both ways.*

**T1c — The pure move.** `axes/a01_tracking/*` and `axes/a02_court/*` → `sports/basketball/{tracking,court}/`;
`pipeline/basketball_spine.py` + `core.py` → `sports/basketball/spine.py`; `contracts/geometry.py` →
`shared/geometry.py`. Imports rewritten, no behavior touched.
*Acceptance: `git log --follow` shows renames not rewrites; `make check-real` reproduces every asserted
number bit-for-bit; the import-direction test (T4a) passes.*

**T1d — The rename obligation.** Restate the re-acquisition speed cap over normalized surface
coordinates instead of `CourtShape` and "court widths", and move what survives of `jersey_tagging.py`
(106 lines: the cap constant, the shape/fps fallbacks, `fps_from_timestamps`) into the player-tracker
family under a name that is not "jersey". **If the restatement changes any admitted re-acquisition on
real footage, it stays in `sports/basketball/` and the tree is corrected instead** — that branch is a
success, not a failure.
*Acceptance: either (a) the code sits under `shared/`, the no-sport-name grep is empty, and no admitted
re-acquisition changed on real footage; or (b) it sits under `sports/basketball/`, with the diff in
admitted re-acquisitions recorded as the reason. A run that produces neither fails.*

**T2 — The `data/` template.** Unchanged, plus: resolve C1 (five final methods), resolve F4 (where the
package lives, given the existing 5.3 GB `data/`), and implement D2 — `data/cache/<corpus_id>/`
gitignored, a manifest-driven `_iter_units()`, a manifest-derived fingerprint, per-unit byte checksums
recorded on first fetch, and the cache-clear entry point plus `make data-clean`.
*Added acceptance: a conformance run over a registered remote corpus fetches **no payload bytes** unless
`payload()` is called — asserted by a fetch counter, because this is the constraint that keeps
`make check-fast` from pulling the bucket; clearing the cache and re-fetching one unit reproduces its
recorded checksum; `make data-clean` leaves `campaign_state/` untouched and vice versa.*

**T2b-1 — The format bases.** Implement the bases that have ≥2 members, from a census of the catalog
first: `MotChallengeSource`, `CocoSource`, `FrameLabelCsvSource`, `VideoAnnotationSource`,
`CalibrationSource`. **The census comes before the code** — the plan's five are a hypothesis, and a base
written for a single corpus is forbidden by T2b's own acceptance (e).
*Acceptance: the census is committed as a table (corpus → format → member count); every base has ≥2
members; a base's four hooks are implemented once and the conformance suite runs against a fixture leaf.*

**T2b-2 — The campaign-consumed corpora (~16 leaves).** The 12 catalog rows marked `consumed by`, plus
the four re-pointings the audit found: `nfl-helmet-assignment` (declare the `test/` partition that exists
on S3), `soccernet-gsr` (replacing SoccerTrack v2, which offers scoring data and no training data),
`soccernet-calibration`, `soccernet-tracking`. **This is the ticket the engine is blocked on** — nothing
after it in the Praxis plan can start until it is green.
*Acceptance: every one of the ~16 registers, loads real media, and exposes both a fittable and a
scoreable partition; the four re-pointings are verified against S3 rather than against a DONE marker.*

**T2b-3 — `attribution_ok` + `broadcast_tos` (10 leaves).** The commercially cleanest tier.

**T2b-4 / T2b-5 / T2b-6 — `research_only` (77 leaves), batched by format base**, descending by holding
size. Batching by format rather than by size is what makes each batch one round: the slow part is not
writing a 20-line leaf, it is confirming decoded frames are reachable, and that verification is
identical within a format.
*Acceptance (each): every leaf in the batch registers and passes conformance, **or** is left
**unregistered with the reason recorded** — never stubbed, never half-registered.*

**T3 — Register the current spine.** Unchanged.

**T4a — Import-direction and dead-code tests.** Import direction; no `arms/`; orphan module; no
`arm/*` branch or `runs/*` tag; no `_v2/_new/_prod/best_`; no `utils/common/misc`; no tracked weights;
toggle-older-than-verdict; vague-name.

**T4b — Contract guards.** Every `contracts/` class inherits `Contract`; no boundary type defined
outside `contracts/`; no `@dataclass`/`TypedDict`/bare `dict`/`tuple` in a signature crossing a package
boundary; every public function in `data/`, `shared/`, `analysis/`, `runtime/` annotated with contract
types.

**T4c — Data-template guards.** `data.__all__` is exactly `("get_source", "list_corpora")`; no subclass
defines a public name or shadows a final method; no concrete adapter imported outside `data/adapters/`;
no path into `data/files/` outside `data/adapters/`; conformance parametrised over `list_corpora()`.

**T4d — Campaign-contract and model guards.** Exactly the four named files per campaign folder; no
lifecycle name or `Registry(...)` under `campaigns/`; no campaign id in `runtime/`, `contracts/`,
`data/`, `shared/`, `sports/`; weights only via `model_loader`; one `production` per `(family, sport)`;
`subclass-does-not-override-forward`.

**T6a — Move the tracker into its family and register it.** `git mv` is already done on `main` (commit
`ff518414`); what remains is the family shape — `model.py` / `preprocess.py` / `postprocess.py` — the
provenance docstring, and `NOTICE`.
*Acceptance: real tracking gates pass bit-for-bit on the same frames; the family passes the
no-sport-name grep or the T1d (b) branch is taken.*

**T6b — Prune to the reachable set.** Trace from `BotSort` at `trackers/bbox/botsort.py` and delete
what it cannot reach: the terminal UI (`utils/rich/core/ui.py`, 984 lines, and its `rich` dependency),
the visualization/display/formatting layer, and every Kalman variant, geometry mode, and appearance path
outside the reachable set.
*Acceptance: real tracking gates pass bit-for-bit; the orphan-module test finds nothing; removed line
count and the upstream commit recorded in the commit message.*

**T7 — Structural spec validation.** Unchanged. **Library call, not a CLI**, so Praxis R0 can compose it.

**T8 — The landing commit, `RESULT.md`, and the inverse.** Unchanged.

**T9 — Author the campaign set, and write `docs/RUNNING.md`.** T5 merged in as the final step, because
the page cannot be written before the thing it documents exists and it is not a round of its own.

---

## 5. Behaviors, with sketched acceptance conditions

Beyond the tickets, these are the standing behaviors the tree must exhibit. Each is a candidate
requirement in its own right; several currently have no named enforcer, which is noted.

1. **Two sports use it ⇒ it is in `shared/`, in the same change.** *Acceptance: a module imported by two
   `sports/<sport>/` trees and living outside `shared/` fails a test.* **No enforcer exists today** —
   §3.1 states the rule; nothing in T4 checks it. Candidate addition to T4a.
2. **Nothing under `shared/` names a sport.** *Acceptance: grep over `shared/` for the sport vocabulary
   returns empty.* Enforcer: T4a. **The word list is undefined** — "a surface-specific word from one
   sport's vocabulary" is not a checkable set. See §7 D3.
3. **A capability is a family under `models/`, never a sibling folder.** *Acceptance: no directory under
   `shared/` or `sports/<sport>/` other than `models/<family>/` contains a weight load or a `forward`.*
   **No enforcer named.**
4. **Weights load only in `runtime/model_loader.py`, by `(registered_model, alias)`.** Enforcer: T4d.
5. **A corpus with no adapter is invisible to the engine's survey.** *Acceptance: `make data-catalog`
   shows no input prefix without an adapter, and every `?` row is either registered or listed in
   `RUNNING.md` with a reason.* Enforcer: T2b acceptance (d). **This is a standing invariant, not a
   one-time check** — it regresses the moment a new prefix appears in the bucket. Candidate: make it a
   check, not a ticket acceptance.
6. **No source is privileged.** Owner footage is one `corpus_id` among many; no direct path anywhere.
   *Acceptance: grep finds no owner-corpus path outside its adapter.* Enforcer: T2 acceptance (c).
7. **Real data only; a missing corpus raises, nothing is synthesised, nothing is skipped.** Enforcer:
   the conformance suite (`load()` raises `CorpusUnavailable`).
8. **`parts.test` is readable only from `campaigns/<id>/evaluate.py`.** *Acceptance: `sample()` on a
   partition named `test` raises outside `evaluate.py`.* **The caller check is unspecified** — stack
   inspection is the only mechanism the plan implies, and it is fragile. See §7 D4.
9. **Every campaign lands exactly one commit, whatever its outcome.** Promoted → code + `RESULT.md`;
   `MEASURED`/`REFUTED`/`ABANDONED` → `RESULT.md` alone. Enforcer: T8.
10. **`main`'s history is one commit per campaign; a rejected idea leaves no code anywhere.** *Acceptance:
    no `arm/*` branch, no `runs/*` tag, no `campaign_state/` path in a tracked file.* Enforcer: T4a.
11. **Config toggles never land.** A winning arm lands with the flag removed and the winning behavior as
    the only behavior. Enforcer: T4a's toggle-older-than-verdict test. **The test's semantics are
    undefined** — "older than verdict" is not stated in terms of anything observable.
12. **The harness is frozen mid-campaign.** The spec digest is fixed at bootstrap and re-checked on every
    dispatch. **Enforcer is in the other repo** (the engine dispatches); this repo can only check that
    `spec.yaml` matches its recorded digest at validation time. Candidate addition to T7.
13. **A campaign may target a shared family, and a shared-family promotion is cross-sport.** A campaign
    declares which family it improves; when that family is under `shared/`, promoting it changes what
    **every** consuming sport loads. *Acceptance: a campaign whose target family is shared must be scored
    on corpora from every sport that consumes it, and `finalize`'s compat load must pass for each of those
    sports before the alias moves.* **Currently unowned, and this is a genuine gap** — §5 assumes a
    campaign belongs to one sport (`metrics/<sport>.yaml`, one alias per `(family, sport)`), so a
    cross-sport promotion has no gate today. A shared detector improved on basketball footage and promoted
    would silently change football inference with no football measurement anywhere in the record. See D11.
14. **Licence is recorded on the source**, so provenance is answerable from a `corpus_id` alone.
    Enforcer: T2b acceptance (b). Note: §4 Rule 7 explicitly declines a promotion gate on licence,
    consistent with the standing decision that rights are already cleared.

---

## 6. Edge states and failure classes

Enumerated per the gap lenses. Several have no owner in the plan.

**Data loss and irreversibility**

- **E1 — `campaign_state/` holds the live registry.** It contains `ml_registry` plus three campaign
  directories today (F13). The plan calls the directory "disposable" and "deleted after the landing
  commit". Deleting it as written destroys the registry. **Unowned.**
- **E2 — `data/` is 5.3 GB with 149 tracked files** (F4). Any T1 move that treats `data/` as a new empty
  package either clobbers or orphans it. **Unowned.**
- **E3 — T1b's `strict=True` conversion is not revertible per-boundary.** If the real-data suite fails,
  the failure is at an unknown number of boundaries at once. Needs a stated fallback: convert
  `contracts/` module by module with the suite green between each, rather than in one commit.
- **E4 — T8's landing commit half-writing.** The plan requires commit-and-alias to be atomic across two
  repos, which git cannot give. The stated mitigation is "a failed T8 call is a failed promotion", which
  covers alias-after-commit but **not commit-succeeded-then-crash**. Needs an idempotence key so a retry
  is a no-op, which T8's acceptance names but does not specify.
- **E5 — T6b deletes from an 8,236-line tree by reachability.** A dynamic import or a plugin registry
  inside BoT-SORT would make static reachability wrong, and the deletion is only recoverable from
  history. Needs: the reachability trace committed as evidence before the deletion lands.

**Empty, missing, partial**

- **E6** — a corpus that lists but whose media does not decode (the catalog's `no .mkv` rows). Owned:
  T2b says leave it unregistered with the reason recorded.
- **E7** — a corpus that decodes but whose annotations are empty or malformed. **Unowned** — conformance
  checks fingerprint, order, purity, and partitions, but not that a partition is non-empty or that
  labels parse. Candidate: add "every partition is non-empty and every unit's labels parse" to the suite.
- **E8** — a format base with exactly one member (forbidden by T2b (e), so the leaf implements the hooks
  directly). Owned.
- **E9** — a duplicate `corpus_id`. Owned: the registry refuses at import time.
- **E10** — a campaign naming a `corpus_id` that resolves but whose partitions do not cover its declared
  roles. Owned: T7.
- **E11** — `make data-catalog` failing because S3 is unreachable or credentials are absent. **Unowned**
  — the target is a hard gate in T2b acceptance (d) and it depends on the network, which a build round
  may not have. See §7 D5.

**Operational**

- **E12 — egress cost and rate limits.** 1,092.8 GB across 1,133,011 objects. If adapters stream from S3
  during conformance, every T2b batch re-pays egress; if they require a prior local download, the
  download is unowned work. See §7 D2.
- **E13 — network access inside a build round.** T2b's verification is inherently networked; the engine's
  arms are inherently sealed. These are different repos, so no contradiction, but the build worker needs
  credentials that the plan never mentions.
- **E14 — GPU availability.** Nothing in T1–T9 trains, so every ticket here is `device: cpu`. Worth
  stating so admission caps are right.

**Silent partial failure — the dangerous class**

- **E15 — a leaf adapter that registers and returns plausible garbage.** Conformance checks structure,
  determinism, and purity — all of which garbage passes. The project's own rule is *"render it, then
  believe it."* Nothing in T2b renders. **Candidate requirement: every registered adapter's acceptance
  includes one rendered sample from one unit, committed as evidence.** This is the single highest-value
  addition the pass found.
- **E16 — a `_group_key` that is wrong but well-formed.** It silently breaks both split purity (leakage
  across the train/test boundary) and the engine's rope (the bootstrap resamples over it). A wrong group
  key produces a *better-looking* number, so nothing downstream flags it. **Unowned.** Candidate: the
  suite asserts group count is strictly between 1 and unit count, and that no group spans two partitions.
- **E17 — T1c's move passing `make check` while changing a number no test asserts.** The real-footage
  gates cover court projection, detection, and tracking; anything outside those three is unprotected
  during the largest move in the plan.

---

## 7. Open decisions

Autonomous mode: each has a default taken and flagged. **Every high-regret fork is now settled by the owner (D1, D2, D11)**; the remainder carry defaults.
Those three and are recorded
below with the constraints that follow from them.

**D1 — Does `shared/` survive as a directory level? SETTLED: yes.** Decided by the owner, 2026-08-23.
`shared/` stays, and holds `models/` plus `geometry.py`. C4 is closed: T1's acceptance no longer permits
two trees. **Two consequences were decided with it:**

- **A model family may live in `shared/` with a single consumer, when it is intended to serve more than
  one sport.** This relaxes §3.1's count-the-consumers rule for families only, and the reason is sound: a
  family is a promotion unit carrying a registered model and an alias, so relocating it once a second
  sport arrives would move what production loads, mid-flight. Declaring intent up front is cheaper than
  the move. *Honest caveat, flagged not resisted: "intended to serve more than one sport" is the
  judgement call §3.1 was written to eliminate, and it is unfalsifiable as stated — every family's author
  will believe it. So the intent is made **declared and auditable** rather than vague: `registry.py` names
  the family's target sports, at least one of them not yet a consumer, and `audit_shared_models()` reports
  families whose declared targets stay unrealised. That converts an unfalsifiable claim into a visible
  stale one. It does not make it enforceable, and `shared/` will drift toward "everything" if nobody reads
  the audit.* The vocabulary obligation is **unconditional** — that is what actually protects the reader,
  and it applies whether the second consumer exists yet or not. The exception covers model families only;
  a plain module still earns `shared/` by having two importers.
- **The alias key was contradictory and is now resolved.** §4 Rule 2 said *"One `production` alias per
  `(family, sport)`"*, which cannot be right for a family whose single artifact serves every sport — that
  is one promotion, not N, and N aliases could point at different versions of one weight file. Resolved:
  a shared-artifact family is keyed by **`family` alone**; `(family, sport)` aliases exist only where a
  sport carries its own fine-tuned weights (`derived_from`); a family may not hold both forms at once.
  T4d gains an alias-key test.

**D2 — Where are a corpus's bytes when an adapter reads them? SETTLED: stream and cache, delete at
campaign close.** Decided by the owner, 2026-08-23. `data/cache/<corpus_id>/` is gitignored and
disposable; it is the only place remote bytes land; it is deleted when the campaign that filled it
closes. This is what makes ~1,093 GB reachable without a terabyte of local disk or an unowned download
step, and it closes C5.

**Three design constraints follow, and they are not optional — they are what keeps the decision from
costing the whole bucket in egress on every test run:**

- **`_iter_units()` must be manifest-driven, never byte-driven.** The conformance suite is parametrised
  over *every* registered source. If enumerating a corpus's units requires fetching its payloads, then a
  single `make check-fast` pulls the entire bucket. A unit is a record; only `payload()` touches the
  network. This is a hard constraint on every format base in T2b-1.
- **The corpus fingerprint is computed over the manifest, not the bytes**, for the same reason —
  `split()` must be deterministic against something cheap. Per-unit byte checksums are recorded as each
  unit is first fetched, which is what makes a re-fetch *verifiable* rather than merely repeated.
- **A deleted cache must be re-fillable to the same bytes.** Destroying the cache means reproducing a
  promoted result requires re-fetching. `RESULT.md` already records every `corpus_id` with its
  fingerprint (§5.2); the per-unit checksums make the re-fetch provably identical.

**Two costs, flagged rather than hidden:** (1) egress is re-paid whenever a cache is cold, so a campaign
re-run after cleanup pays again, and a T2b batch pays once per batch — acceptable, but it means "cheap to
re-run" is false for anything touching a large corpus; (2) a single campaign's declared corpora can still
exceed local disk even with cleanup, because cleanup happens at campaign *close*, not mid-campaign. **A
per-campaign cache budget is unowned** — the Praxis plan's §6.5 bounds per-arm wall clock, heartbeat, and
disk, so the disk bound belongs there, but it has to know this repo's cache root. That crossing is not
yet written in either plan.

**Two disposable roots, different owners, same moment.** `campaign_state/` holds the registry and arm
worktrees; `data/cache/` holds fetched media. Both are deleted around campaign close, by different calls,
and neither deletion may take the other's contents — which matters because `campaign_state/` currently
holds the live `ml_registry` (E1). A `make data-clean` target and a library entry point both exist: the
engine calls one, a person calls the other.

**D3 — What is the sport-vocabulary word list?** *(now unconditional per D1, so it carries more weight)* (Default taken: the grep list is `{court, basket, hoop,
paint, three-point, free-throw, backboard, key, baseline, sideline}` plus each sport's name, committed
as a data file `shared/_neutral_vocabulary.txt` so it is extensible without editing a test. Flagged:
"baseline" and "key" are also generic words and will produce false failures.)

**D4 — How does `sample()` know its caller is an `evaluate.py`?** (Default taken: not by stack
inspection. `RunContext` carries an explicit `may_read_sealed: bool` that only the engine sets when it
constructs the context for `score()`, and `sample()` refuses a `test` partition unless the calling
context says so. Flagged: this moves the guard into the contract, which means the sealed-split guarantee
depends on the engine constructing the context honestly — the sandbox in the other repo is the real
enforcement, and this is defence in depth, not the seal.)

**D5 — Is `make data-catalog` a build gate or a periodic check? CONFIRMED by the owner: periodic.** The
target stays and is re-runnable on demand when new data is added; **no ticket blocks on it**, and nothing
is held back waiting for the catalog to be complete. (Default taken and now confirmed: **periodic**, not a
gate. Making a networked S3 listing a condition of a ticket going green means a build round fails on a
credential or a rate limit rather than on the code. T2b's acceptance (d) becomes: the catalog was
regenerated once during the ticket, and the diff is committed. Flagged: this weakens the "no invisible
corpus" invariant to something a human has to re-run.)

**D6 — Where do the seam contract types live?** (Default taken: one file, `contracts/campaign.py`,
holding `CampaignSpec`, `RunContext`, `RunResult`, `Measurement` — resolving C2 in favour of the tree
diagram, which two of the four references already agree with. `run_context.py` and `campaign_spec.py`
are struck from the plan.)

**D7 — What happens to `contracts/artifacts.py` and `contracts/axes.py`?** (Default taken: `artifacts.py`
is deleted in T1a — its entire stated rationale was avoiding an import of `sports_analysis.experimentation`,
which no longer exists (F14); `LEDGER_HEADER`/`git_provenance` move to `runtime/` if anything still calls
them, and are deleted if nothing does. `axes.py` is read and split: the Protocols that describe surviving
axes move to `contracts/predictions.py`, the rest goes. Flagged: neither file appears in the target tree,
so this is a gap being filled rather than a decision being changed.)

**D8 — Does the two-sport rule get an enforcer?** (Default taken: yes, a T4a test — a module imported
from two different `sports/<sport>/` trees while living outside `shared/` fails. Flagged: with one sport
in the tree the test is vacuous today, which is the right time to write it.)

**D9 — What evidence does an adapter owe? SETTLED (owner override): the entire dataset, not a sample.**
My default was one rendered sample per registered corpus. Overridden 2026-08-23: an adapter must
enumerate and validate its **complete** corpus — every unit, every partition — not a sampled subset.
Rationale: a sampled check cannot distinguish a corpus that loads from one that loads *partly*, and a
half-covered adapter is exactly what makes a data gap invisible (T2b's own stated failure).

**This collides with D2 and the collision is not yet resolved.** D2 requires that conformance fetch **no
payload bytes** — otherwise `make check-fast`, parametrised over every registered source, pulls the whole
bucket. Full-dataset validation over ~1,093 GB cannot both be complete and byte-free. The reconciliation
that satisfies both, and what T2 should implement unless overridden: **completeness is asserted over the
manifest, correctness is rendered over a bounded set.** Every unit is enumerated, counted, and checked to
resolve to a fetchable address (no bytes); a bounded number of units per corpus is actually fetched and
rendered as committed evidence. That makes "the adapter covers the whole dataset" a full-set claim and
"the bytes are what they claim to be" a sampled one. **Flagged: if the owner means every unit's bytes must
be fetched and rendered, the egress and wall-clock cost of T2b rises by roughly the size of the bucket,
and D2's cleanup makes that cost recur.** Owner's call.

**RESOLVED 2026-08-24 (owner: "go"):** the manifest-complete / render-bounded reading is adopted.
Concretely, and this is what T2 and T2b are extracted against:
- **Completeness is a full-set claim, asserted without moving bytes.** Every unit in the corpus is
  enumerated, counted, and confirmed to resolve to a fetchable address. A corpus that cannot enumerate
  its whole unit set is refused registration. No sampling in this half.
- **Correctness is a bounded claim, asserted by rendering.** A bounded number of units per corpus is
  actually fetched and rendered, and the render is committed as the adapter's evidence.
- **`make check-fast` moves zero payload bytes.** Asserted by a fetch counter in T2's acceptance, which
  is the mechanical guard that keeps the conformance suite from pulling the bucket.

**D10 — `check-real` in `make check`?** (Default taken: no. Today `check: check-collect check-real-footage
check-fast` and `check-real` is separate. Every ticket's acceptance names both explicitly. Flagged: two
commands means a worker can forget one, so a `check-all` target is worth adding in T9.)

**D11 — What gates a cross-sport promotion? SETTLED: score every consuming sport.** Decided by the
owner, 2026-08-23. A campaign improving a family under `shared/` declares a scoring corpus **per
consuming sport**, and `finalize` refuses to move the alias until each has a measurement. Rationale: the
alias is one artifact serving N sports, so promoting on one sport's evidence changes N-1 sports untested,
which the project's "render it, then believe it" rule forbids.

Scope of the obligation, so it is checkable rather than aspirational:

- **Consumer, not declared target.** A sport that *imports* the family owes a measurement. A sport merely
  **declared** as a target under D1's single-consumer exception owes nothing yet — there is nothing to
  regress — but owes one the moment it becomes a consumer. `audit_shared_models()` is what makes that
  transition visible.
- **A sport-specific family owes exactly one measurement.** The rule costs nothing outside `shared/`.
- **Enforced in two places.** `validate` (T7) refuses a spec that targets a shared family without a
  scoring corpus per consuming sport — a structural check this repo can make. `finalize` (T8) refuses to
  move the alias without a measurement for each — the runtime half.

**The cost, stated plainly: this makes shared-model campaigns materially more expensive than
sport-specific ones**, and that is a deliberate price on the thing that has the widest blast radius. Two
second-order effects worth watching: (1) it creates an incentive to keep a model sport-specific to avoid
the bill, which is the opposite of the consolidation `shared/` exists for; (2) with only basketball in the
tree today, the rule is **vacuous until a second sport lands** — which is the right time to write it, but
it means it will be untested when it first bites.

**Still open, no default, needs the other repo:** nobody owns writing `families` into a spec. The Praxis
plan §6.9 records it as its last open design question; T9 authors specs and would be the natural writer,
but the operator set per stage is engine policy. This crosses the seam and is the one question neither
document can close alone.

---

## 8. Build order

Corrected DAG. The plan's numbering is not a valid order: T5 as written cannot precede T6/T7/T8 (it
documents them), and T4's guards cannot pass before the tree they describe exists.

```
T1a ─▶ T1b ─▶ T1c ─┬─▶ T1d ─▶ T6a ─▶ T6b
                   │
                   ├─▶ T2 ─▶ T2b-1 ─▶ T2b-2 ─┬─▶ T2b-3 ─▶ T2b-4 ─▶ T2b-5 ─▶ T2b-6
                   │                          │
                   │                          └─▶ T7 ─▶ T9
                   ├─▶ T3
                   └─▶ T4a ─▶ T4b
                              T4c  (after T2)
                              T4d  (after T7)
                       T8    (after T3)
```

- **Parallel-safe:** `T3` ‖ `T4a` ‖ `T2` once `T1c` is green. `T2b-3…6` are pairwise independent.
  `T6a/T6b` are independent of the whole `data/` branch.
- **The critical path to unblocking Praxis R0 is `T1a → T1b → T1c → T2 → T2b-1 → T2b-2 → T7`** — seven
  rounds. `T2b-3` through `T2b-6` are *not* on it: the engine only needs the corpora its registered
  campaigns name. **This contradicts the plan's "T1 through T9 complete and green, then R0 onward."**
  Recommended correction: R0 waits on the critical path, and the `research_only` long tail runs in
  parallel with early engine work. Flagged as a change to the plan's own sequencing claim.
- **Every ticket here is `device: cpu`** (E14). Nothing in this plan trains.
- **`verify: manual`** for D1 (an architecture decision) and for T9's `RUNNING.md` step. Everything else
  is `verify: automated`.

---

## 9. What was NOT challenged

Stated so a clean-looking section is not mistaken for an exhausted one:

- The §3.2 contracts design was reviewed for contradictions and edge cases but **not** for whether
  strict frozen pydantic is the right choice — that was settled in an earlier session and is not
  reopened here.
- §5.2's arm/verdict/finalize lifecycle was checked for internal consistency only; its *semantics* are
  the engine's and are out of scope.
- The data catalog's numbers (1,133,011 objects / 1,092.8 GB / 159 unreferenced prefixes) were taken from
  `docs/data-catalog.md` and **not** re-verified against S3 in this pass. They were cross-checked
  against `aws s3 ls --summarize` when generated.
- The 8 drafted campaign specs were not re-read in this pass; T9's scope rests on the earlier audit.

## 10. Rigor mode

- **Rigor: Rigorous in substance, single-threaded in execution.** Six parallel read-only agents were
  dispatched across four lenses (adversarial falsification, the five gap lenses, feasibility-vs-tree,
  ticket-sizing/DAG). **All six terminated on upstream `529 Overloaded`** — none returned a report. All
  four lenses were therefore **run inline by hand**, which is why §2's evidence column is command output
  rather than an agent's summary, and why §3's contradictions cite line numbers from a direct read.
  What this costs, stated plainly: a single reader ran every lens, so the independence a fan-out buys —
  one agent blind to what another found — was not obtained. §2, §3, §4 and §8 are grounded in commands
  and are as reliable as their evidence. §5, §6 and §7 are one reader's judgment and are the sections a
  fresh pass is most likely to add to. **Re-running the adversarial and gap-lens passes when the API
  recovers is worth doing before extraction.**
- **Decision mode: Autonomous.** Eight defaults taken and flagged (§7 D3–D10); **D1, D2 and D11 settled by the owner rather than defaulted**
  as high-regret rather than defaulted.
- **`ce-brainstorm` / `ce-ideate`: skipped, explicitly.** The source is a hardened 593-line plan that
  already went through `ce-ideate` prior-art research, an eight-agent audit, and two cold-eye passes.
  Re-running the front-end on it would generate nothing.
- **`ce-doc-review`: NOT RUN.** It dispatches a multi-persona panel as subagents, which is exactly what
  is failing. This is a **skipped required step**, recorded rather than silently omitted: af-plan
  mandates it on every run. It should run on this document before `af-intake-plan` extracts, and its
  findings folded into §7.
