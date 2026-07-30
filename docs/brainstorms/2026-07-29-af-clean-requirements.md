# Requirements: `af-clean` — portable aggressive AI-slop cleanup

**Status:** af-plan output. Not admitted to Praxis. Hand-off target: `af-intake-plan`.
**Upstream:** [docs/ideation/2026-07-29-af-clean-ideation.md](../ideation/2026-07-29-af-clean-ideation.md)
(15 ranked ideas, taste reference, 15 resolved decisions R1–R15, research findings F-A–F-D).
**Rigor mode:** Rigorous. Decision mode: Collaborate.
**Review:** `ce-doc-review` round 1 complete (6 personas, 50 findings) — 2 fixes auto-applied,
31 applied on decision, 2 deferred to §7.

---

## 0. Goals and success criteria

`af-clean` exists to remove AI-authored slop and unreachable code from a repository
aggressively, without changing behavior. One sentence of positioning: **it is the remediation
arm for code that already exists — not a linter, not a gate, and not a style enforcer.**

Primary outcomes, measured per run on E1:

- **Operator-accepted findings per run.** The count of findings the operator applies rather than
  skips. This is the tool's usefulness signal.
- **False-positive rate on applied hunks.** The share of applied hunks the operator later reverts,
  or that the validation step has to repair. This is the tool's trust signal.
- **Slop density delta per module** across runs (from B7's census), which is the only measure that
  can answer whether the repo is actually getting better.

**The aggression dial is gated on measurement.** B22's required witness tier may only be lowered
when the calibration harness (IF7 — now in scope per §1.3) reports a false-positive rate below a
stated threshold on the affected finding class. No measurement, no lowering. This is what makes
OPEN-6's precision floor answerable rather than a guess.

Falling test-coverage percentage and falling assertion count are **expected success signals**
(R12), not regressions, and must never be used as outcome metrics.

## 1. Scope

### 1.1 What this is

A cleanup engine with two entry points and one shared rubric, plus a decomposable
validation+remediation step, plus the work needed to turn the existing `minimalism-dry` judge from
a report-only observer into a real gate.

Three agents in three roles, never collapsed (R2, R8):

| Role | Component | May it edit code? | May it run tests? |
|---|---|---|---|
| **Grader** | `minimalism-dry` graded judge (exists) | No | No (`"Do not re-run tests"`) |
| **Cleaner** | `af-clean` (new) | Yes | **Read-only measurement only** (B44) |
| **Fixer** | validation+remediation step (new, decomposable) | Yes | Yes |

**Role separation is enforced by context isolation, not by prompt text** (B46). The honor-system
reading — a single agent told which hat to wear — collapses the guarantee the moment that agent
holds both the rationale and the Bash tool.

### 1.2 Entry points

- **E1 — Human-invoked (PRIMARY by usage volume, R14).** `/af-clean [path…]`. Whole repo by
  default, or a caller-named subtree. Runs against **arbitrary repositories**, most of which never
  went through `af-build`. After cleaning, it invokes the validation step itself.
- **E2 — Axis-invoked (secondary).** Triggered as `minimalism-dry`'s remediation arm on graded
  failure during an `af-build` ticket, scoped to that ticket's diff. Does **not** invoke validation
  inline — `af-build` already validates at end of run (R8). Ships report-only (D8).

### 1.3 Explicitly in scope

- The `report_only = true` → `false` flip at `agent_factory/seeded_checks.toml:104`, with all three
  prerequisites as gating units (R13).
- A **path-predicate exemption layer**, which does not exist today (F-C).
- Amending `minimalism-dry`'s axis shape to be signed rather than shrink-only (R3, idea 11).
- Portable per-repo discovery: tooling, roots, exemptions, anchors (R14).
- A `af-clean`-owned Praxis space, self-bootstrapping (R15).
- **The calibration harness (IF7)** — promoted from implied feature to in-scope instrument,
  because §0's aggression gate, OPEN-6's precision floor, and D8's E2 flip all depend on it.

### 1.4 Explicitly out of scope

- Deprecating this repo's `frontend/` tree. Research (F-C) shows it has three live dependencies;
  it needs a human deprecation ticket, not a cleaner.
- Performance optimization of cleaned code. `af-clean` preserves behavior; it does not tune.
- Any change to `af-build`'s FIND→CLAIM→RESOLVE→BUILD→VERIFY→FINISH loop beyond the caller-side
  remediation branch, the coverage-authoring fix, and the frozen-entry migration (B36).
- Formatting/style enforcement. No formatter gate exists in this repo and adding one is separate.
- Multi-repo/batch invocation. One repo per invocation.
- Sandboxed or containerized execution of discovered commands — see §3.6 and OPEN-11.

### 1.5 Actors

- **Operator** — a human running E1 on a repo they own. Reviews a risk-stratified commit stack,
  accepts/rejects findings, and their rejections are durable.
- **Build worker** — the `af-build` agent hitting E2. No human in the loop, so only `enforce`-tier
  findings may auto-apply (B12) — and per D8, none do until calibration exists.
- **Grader** — the judge. Never sees the cleaner's rationale (B23).
- **Maintainer of the target repo** — may not be the operator. Their institutional memory (scars,
  intentional duplication) is what the guards protect.

---

## 2. Behaviors

Acceptance conditions are sketched where possible; `OPEN` marks ones af-intake-plan must force.

### 2.1 Discovery (portable, per repo)

- **B1. Detect the toolchain.** Identify languages, package managers, test runners, linters, type
  checkers present. *Accept:* on a repo with `pyproject.toml` + `package.json`, the report names the
  Python and JS toolchains and which of Vulture/Knip/jscpd/radon/semgrep are absent.
- **B2. Install missing detectors zero-install where possible.** Prefer `uvx <tool>@<pinned>` and
  `npx -y <tool>@<pinned>`, matching the existing `uvx ruff@0.15.20` CI pattern. Never mutate the
  target repo's lockfiles or dependency manifests to run a detector. *Accept:* a full run on a repo
  with none of the five tools installed leaves `git status` showing no manifest/lockfile change.
  Execution-trust posture: see §3.6.
- **B3. Derive the exemption manifest automatically.** From seven inputs: `.gitignore`;
  `.gitattributes` (`linguist-generated`, `linguist-vendored`); `@generated` / "DO NOT EDIT" /
  "auto-generated" markers; lockfile filenames; known vendor/build directory names; detected codegen
  output; and **a tool-side list of language-convention immutable/fixture directory names**
  (`migrations/`, `testdata/`, `__snapshots__/`, `fixtures/`). The seventh input is required because
  the first six cannot produce `migrations/` or `testdata/` on this repo — verified: `migrations/`
  is tracked and not ignored, `.gitattributes` carries only EOL pins, and only 1 of 16 migration
  files has any marker. *Accept:* on this repo the manifest contains `migrations/`,
  `praxis_kg_dump.sql`, `uv.lock`, `package-lock.json`, `frontend-react/public/mock-*.json`, the
  `skill_unification/sources/` tree, `go.sum`, `testdata/`, and — critically — `infra/cdk.out/`,
  **without any entry hand-authored in the target repo.** Exemption-manifest entries are surfaced
  as reviewable output, never silent exclusions (§3.6).
- **B4. Discover reachability roots by framework detection, not hardcoding.** Route decorators,
  app factories, tool/plugin registrations, CLI entry points (`[project.scripts]`, `package.json`
  `bin`/`scripts`), module `__main__` blocks, migration loaders, hook configs, test roots, and
  bundler entries. *Accept:* on this repo the root set includes the nested `@app.*` closures inside
  `knowledge/serve/app.py`'s `create_app`, the 59 `@mcp.tool()` functions, `praxis-mcp`, the 7
  `agent_factory/hooks/*.py` scripts, yoyo migrations, and `frontend-react/src/main.tsx`.
- **B5. Generate the dead-code-detector whitelist from B4.** Never run an unwhitelisted sweep.
  *Accept:* Vulture on this repo reports zero of the FastAPI closures and zero `@mcp.tool()`
  functions as unused.
- **B6. Enumerate the string-dispatch corpus.** Every string literal, config value, template
  reference, `getattr`/`importlib` argument, DI/registry key, and computed-key lookup table, with
  each entry tagged by the context it appeared in. Secret-shaped values are redacted before
  storage (§3.6). *Accept:* `build_completeness_gate.py` is never proposed for deletion despite
  having zero importers, because `hooks.json:8` names it inside a command string.
- **B44. Pre-clean read-only measurement pass.** Before any edit, collect the coverage map and
  execution evidence that B17's tri-state matrix and B22's witness tiers consume. This is
  measurement, not remediation: the Cleaner may execute the test suite read-only to gather it, and
  may not edit. Without this pass every unreachable symbol falls into the Uncovered column, making
  B17's delete cell and all of B18 unreachable in practice. *Accept:* a run reports a non-empty
  covered-and-unreachable set on a repo that has one.

### 2.2 Detection

- **B7. Deterministic census first, as an allocation function.** Run the detectors repo-wide, score
  slop density per file, and send only hotspots to LLM judgment (idea 4). *Accept:* the LLM-judged
  file count is materially smaller than total source file count, and the report states both.
- **B8. Publish an instrument × pattern matrix with a first-class uninstrumented list.** Patterns
  with no deterministic detector (comment terseness, single-responsibility, same-job identity) are
  labeled as judgment, not measurement.
- **B9. Same-job detection, not just lexical clone detection** (idea 15, R9). Maintain a job
  inventory in `af-clean`'s Praxis space and check candidates against it, so a job reimplemented
  with different identifiers is found. **Retrieval contract:** comparison is exhaustive within a
  bounded candidate set — same-signature-shape or same-call-graph-neighborhood buckets derived from
  the B7 census — and never via Praxis's `get_context`, which is documented as a sampling top-k that
  can silently drop a match and is therefore unusable for a completeness claim. **Same-job findings
  are `advise`-tier only** and never appear in an applied diff until IF7 reports a measured
  precision floor (§0, D9). *Accept:* two functions performing the same job with zero token overlap
  are reported as a single-source-of-truth violation.
- **B10. Comment triage by information gain, never by density** (idea 12, OPEN-1 pending).
  Deletable only if content words are a near-subset of annotated identifier tokens; a comment
  introducing absent tokens is presumed WHY and protected. Ambiguous comments survive. Keep-by-
  default protects a comment's **existence, not its accuracy**: a comment whose subject was edited
  in the same unit is re-checked against the new code and flagged when falsified — never silently
  deleted. See B26.

### 2.3 Classification and authority

- **B11. Every finding carries a located `file:line` instance.** No location, no finding.
- **B12. Evidence tier is computed per finding instance, and mechanically gates action.** A rule
  declares the **highest** tier its findings may reach; the individual finding's own witness (B22)
  determines the tier it actually gets. `enforce` → may auto-apply. `advise` → report only, never a
  diff. A rule→tier table alone cannot express D2, where the same same-job rule is auto-appliable
  on a lexical match and human-gated on a semantic-only one. *Accept:* an `advise`-tier finding
  never appears in an applied diff in either entry point.
- **B13. Findings are chunk-enumerated against one root question** (idea 8): how many independent
  things must a reader hold to predict what this unit does. A principle name is a diagnosis label,
  never the justification.
- **B14. Slop is signed** (idea 11). Every finding names its pole — bloat or fragmentation — and
  the remediation direction is declared before editing. Signedness is carried **on the finding**,
  not in the rubric's axis schema (B36).
- **B15. Conflicts resolve by observable discriminator or not at all** (idea 10). Where the canon
  genuinely splits, produce the observable (co-change history, parameter accretion) or drop the
  finding. Each principle carries its forbidden inference.

### 2.4 Reachability and deletion

- **B16. Reachability is code-derived primary, Praxis-surface enrichment secondary** (R15).
  *Accept:* on a repo with an empty surface set, `af-clean` reports "no surface oracle available"
  and **does not** treat any symbol as unreachable on that basis.
- **B43. Exemption governs editability, not visibility.** Exempt paths are never modified but are
  **always parsed** and always contribute reachability edges and roots. A symbol referenced only
  from exempt code is classified **Reachable** — never quarantined, never deleted. This is why
  `migrations/` legitimately appears in both B3's exemption manifest and B4's root set. *Accept:*
  a production symbol whose only caller is a yoyo migration is never proposed for deletion.
- **B17. Tri-state verdict, coverage as evidence not liveness** (R11). Reachability gates; coverage
  decides proven vs suspected. Consumes B44's measurement pass.

  | | Covered | Uncovered |
  |---|---|---|
  | **Reachable** | keep | keep + record test debt |
  | **Unreachable** | delete symbol **and** its exclusively-covering test | **quarantine** |

- **B18. Test deletion is a first-class output** (R12). Permitted **only** bound to an
  unreachable-symbol deletion in the same atomic unit. **The atomic unit is one
  (symbol, exclusively-covering-tests) pair**, carried as one reviewable hunk group with a
  machine-readable binding record naming the symbol each deleted test was bound to; the
  unbound-deletion check runs **per pair**, not per commit, so B25's deletion layer is one commit
  containing many independently-checkable bindings. Falling coverage % and assertion count are
  **expected success signals**, not regressions.
- **B19. Staged excision to a fixed point** (idea 3). Recompute reachability after each purge
  round. *Accept:* a helper reachable only through a deleted root is caught in round 2, not left
  for the next invocation.
- **B20. String-corpus quarantine — whole-token, dispatch-context match.** A symbol name, module
  path, or **file path fragment** quarantines when it appears as a whole token in the B6 corpus
  **and** in a plausible dispatch context (dispatch key, path, template reference,
  `getattr`/`importlib` argument). Tokenization splits string literals on path separators, dots,
  and whitespace, so `.../hooks/build_completeness_gate.py` yields the token
  `build_completeness_gate`. Incidental prose matches are recorded separately as low-confidence and
  are **not** folded into quarantine — otherwise short generic names (`run`, `main`, `get`) become
  permanently unpurgeable and deletability tracks naming style rather than reachability.
  *Accept:* the `${CLAUDE_PLUGIN_ROOT}/hooks/build_completeness_gate.py` shape quarantines
  `build_completeness_gate`; the token `run` appearing in a log message does not quarantine `run`.
- **B21. Scar detection on defensive code** (idea 13). Blame before removing try/except, guards,
  or odd special cases; a commit message matching fix/bug/regression/hotfix/incident or citing an
  issue demotes the finding to advisory and cites the commit.

### 2.5 Application

- **B22. The cleaner is not the applier** (idea 5). Witness-tiered:
  1. a named tool's located finding;
  2. AST no-reference **and** absence from the B6 corpus **and** execution evidence over a named
     observation window;
  3. a survived tombstone.

  No witness, no apply. **Aggression is configured by lowering the required witness tier** — but
  the dial has a hard floor: it may never go below tier 2, and the §3.1 must-not-happen guards are
  refused at **every** dial setting (a symbol in the B6 corpus, a test not bound to a same-unit
  symbol deletion, blame-flagged defensive code). Lowering the tier additionally requires the §0
  measurement gate. Tier 2 is stated with three conjuncts because AST-no-reference alone is the
  signal B6/B20 declare insufficient, and non-execution over an unbounded window is unfalsifiable.
- **B23. Blind verification.** The verifier receives only the diff and repo — never the cleaner's
  rationale — and must independently re-derive each hunk's safety argument.
- **B46. Role isolation substrate.** The Grader reuses the existing toolless `Complete` judge seam.
  The Cleaner and the B23 blind verifier each run in a **separate tool-enabled agent context**
  receiving only (diff, repo) — never the caller's transcript. Without this, E2's cleaner keeps the
  af-build agent's Bash tool and its own rationale in context, and §1.1's role table becomes prose.
  `OPEN-12` covers whether that context is a subagent or a fresh CLI invocation.
- **B24. Tied-probe bias control.** Seed each grading batch with a self-paired or cosmetically
  varied probe; systematic preference on tied probes discards the batch, **up to a discard cap
  (default 2)**. On exceeding the cap the run records a calibration failure, degrades the affected
  axis to `advise` tier for that invocation, and proceeds — it never discards indefinitely. The cap
  exists because A5 states the bias is systematic, and detection wired to unconditional discard is
  a non-terminating mode, not a degraded one.
- **B25. Risk-stratified commit stack** (idea 6): comments → covered-unreachable deletions →
  same-job consolidations → behavior-adjacent simplifications → **dead-import cleanup last**
  (dead imports are *created by* the deletion layers, so they cannot precede them).
  *Accept:* truncating the stack at any layer N leaves layers 1..N applied with the repo building
  and tests passing. **Reverting a middle layer in isolation is NOT guaranteed** — layers form a
  dependency chain because later layers edit text earlier layers produced, so the operator's
  affordance is **prefix truncation, not arbitrary revert.** Unwind mechanism: see §3.4.
- **B45. Findings volume cap per run.** Each run surfaces at most a configurable N findings per
  risk layer, selected by descending B7 slop-density score; the remainder defers to the next run
  via the B40 ledger. Default N = 25 per layer (flagged for override, D10). Without a cap, a first
  whole-repo run hands the operator hundreds of hunks across five layers, at which point they stop
  reviewing and either accept blindly or abandon the tool — and the operator is the only safety
  mechanism for every advise-tier and semantic-only finding.
- **B26. Second rubric on `af-clean`'s own diff** (idea 14): clever compression, abbreviated
  identifiers, lost WHY comments, over-collapsed procedures, error handling that now surfaces less
  information, and **comments the diff has falsified** — a surviving comment describing behavior
  the hunk changed, moved, or consolidated away. Gates the apply phase; scored independently. A
  true finding may have its fix rejected.
- **B27. Failed centralization is a stop signal** (RK4). A consolidation requiring a flag or branch
  per caller is rejected; centralize the identical part, leave the divergent tail at call sites.

### 2.6 Validation + remediation step (decomposable, R10)

- **B28.** Contents, in order: full test suite; typecheck + lint; AST reference sweep; reachability
  re-check; the query-resolved `building-validation` lane **when present**; the `minimalism-dry`
  re-grade; bounded remediation loop; coverage report (advisory, not a gate — R11).
- **B29. Callable standalone.** Attachable to any pipeline, not just `af-clean`.
- **B30. Raw command invocation.** No `just test`/`just lint`/`just typecheck` recipes exist here
  (F-D); the step must discover and call the real commands per repo. Execution-trust posture: §3.6.
- **B31. Bounded by iteration cap** (R5). Reuses `graded_loop`-style per-(ticket, validation)
  budgeting where available.

### 2.7 Gating the existing judge (R13)

- **B32. Path-predicate exemption layer — two-phase.** `_universal_exempt` gains a paths argument,
  evaluated twice: at pin time `start_ticket` supplies the ticket's **declared** paths, and at
  grade time the caller of `verify_graded_check` — which already receives the real `code_diff` —
  re-checks the **actual touched paths** and records a *pass-by-exemption* rather than a verdict
  when every touched path is exempt. The two predicates can disagree; **the grade-time result
  wins.** Two phases are required because `start_ticket` composes the contract immediately after
  `claim()`, before the worker writes a line, so touched paths are unknowable there.
  *Accept:* a ticket touching only `migrations/` is exempt without any human having set
  `meta.universal_exempt`. `OPEN-13` covers how a grade-time-exempted requirement is discharged so
  `all_validations_passed` does not stall on it.
- **B33. Coverage-authoring fix.** Either `af-build/SKILL.md` gains explicit universal-lane
  synthesis instructions, or `start_ticket` auto-pins the universal graded validation alongside its
  requirement. *Accept:* with `report_only=false`, a non-exempt ticket reaches
  `all_validations_passed` without a human authoring a covering validation.
- **B34. Cache-hit deadlock escape.** A failing cached verdict on an unchanged diff must consume
  budget or escalate; today it returns `should_block=False` and consumes nothing.
  **No-remediation-available case:** when the failing verdict's defects map only to `advise`-tier
  rules, the verdict is recorded as *unremediable* and escalated to a human surface **instead of**
  consuming budget toward `block()`. Budget consumption on unchanged diffs applies only where at
  least one defect had an `enforce`-tier remediation available — otherwise B34 replaces a livelock
  with a deadlock, since B12 forbids E2 from producing any diff for an advise-tier finding.
  *Accept:* a ticket whose diff never changes escalates within the cap instead of looping forever.
- **B35. `graded_loop` reset on pass boundary.** Add to `pin_requirements`/`release`. *Accept:* a
  ticket that goes incomplete and is re-picked starts a fresh iteration budget.
- **B36. Signed axis shape, plus a frozen-entry migration** (R3). Signedness rides on the finding
  (B14), **not** on the `Axis` schema — `Axis` is defined as `score >= threshold` with no way to
  fail high, so the axes are renamed and re-guided such that a fragmented change scores low on the
  same axis a bloated one does. This keeps rubrics already frozen onto in-flight tickets parseable.
  Separately, because `frozen_rubric_for` reads the rubric **and** `report_only` frozen onto the
  pinned validation at synthesis time and never re-reads `seeded_checks.toml`, both the flip and the
  axis re-guidance require **invalidating or re-pinning frozen entries on every open ticket**
  (keyed by `source_check_id`). `af-clean` refuses to read a pinned rubric whose axis guidance
  predates B36 rather than treating shrink-only scores as signed. The flip is a one-line config
  edit **plus** a pinned-entry migration.
- **B37. Two-tier anchors** (R14). Portable core + per-repo learned. Must include **negative
  anchors** — a legitimate `unknown`-narrowing guard, a deliberate documented silent catch, a
  twice-used thin primitive, a computed-key lookup table (F-D).
- **B38. Blast-radius cleanup.** Fix the 5 test failures **introduced by the flip** — baseline is
  `1 failed, 524 passed`, flipped is `6 failed, 519 passed`, so only 1 fails today. The five sites:
  `tests/test_manual_verify_gating.py:45,70`; `tests/test_seeded_checks.py:108` (a value-lock
  asserting `report_only is True`); `tests/test_unforgeable_caller_context.py:43,88`. Fix pattern is
  copy-paste from `tests/test_universal_ci_harness.py:90-102`. Note `agent_factory/tests/` is not in
  CI (root `testpaths` excludes it) — see OPEN-7.

### 2.8 Memory and compounding

- **B39. Self-bootstrapping Praxis space** (R15). Created per target repo; assumes no existing
  space or snapshot. Isolation model: §3.6.
- **B40. Findings ledger with sticky rejections** (idea 6), keyed by content hash + symbol id +
  rubric version. **Skip granularity:** an unchanged cleared file skips LLM judgment only when its
  **transitive dependency closure and the job inventory are also unchanged**; reachability (B19) and
  same-job matching (B9) are **always recomputed repo-wide**, because both are properties of the
  dependency graph rather than of a file's own bytes. **Rejection key granularity:** the content
  hash covers the **rejected symbol's own normalized source text**, not the containing file, so
  edits elsewhere in the file — including those made by later B19 rounds in the same invocation —
  do not expire it; rejections are additionally pinned for the duration of an invocation regardless
  of hash movement. A declined finding does not re-surface until its own code changes.
- **B41. Liar ledger.** Every reachability veto must name why it was reachable; codified as a rule
  or explicit root so the same false positive cannot recur.
- **B42. Promote recurring patterns to checks** (idea 7) by delegating to
  `af-intake-build-validation` — only where that snapshot exists. **Promoted checks enter at
  `advise` tier unconditionally.** Promotion to `enforce` requires an explicit human authoring step
  recorded against the rule; `af-clean` may never write or raise its own tier. Without this, the
  system can grant itself authority to auto-edit every ticket in the factory.

---

## 3. Edge states and failure classes

### 3.1 Must-not-happen (data loss / irreversibility)

Refused at every B22 dial setting.

- Deleting a string-dispatched entrypoint (the `build_completeness_gate.py` shape). Guard: B6, B20.
- Deleting a WHY comment. Unrecoverable from code. Guard: B10, keep-by-default.
- Deleting a guard that encodes a past incident. Guard: B21.
- Deleting a test **not** bound to an unreachable symbol. Guard: B18.
- Deleting a symbol reachable only from exempt code. Guard: B43.
- Consolidating two jobs that are not the same job. Highest-cost error under R9. Guard: B9 is
  advise-tier until IF7 measures a precision floor.
- Purging on an empty surface set. Guard: B16.

### 3.2 Empty / absent states

- No Praxis space → create it (B39). No surfaces → oracle unavailable, not "all dead" (B16).
- **No reachable or authenticated Praxis backend** → `af-clean` runs in a **degraded local mode**:
  the findings ledger (B40), liar ledger (B41), and job inventory (B9) fall back to an on-disk store
  **outside the target repo**, B16's surface enrichment reports "no surface oracle available", and
  the run states the degradation. It must **not** fail closed the way the factory hooks do — the
  common case for a repo that never went through `af-build` is no space and no credentials.
- No tests → every unreachable symbol lands in **quarantine**, and the run says so loudly.
- No git history → co-change and parameter-accretion discriminators unavailable; findings requiring
  them are dropped, not guessed (B15). `OPEN-3` covers the fallback.
- No detectors installable (offline, no network) → degrade to LLM-only with the uninstrumented list
  covering everything, and state the degradation.
- Empty diff in E2 → no-op, not a failure.
- Repo is a single file / has no recognizable framework → B4 finds no roots. **Must refuse to purge
  rather than treat everything as unreachable** (OPEN-5).

### 3.3 Partial failure

- Detector crashes mid-census → its patterns move to the uninstrumented list; run continues.
- Validation step fails after N cleanup layers applied → the commit stack (B25) means layers below
  the failure survive; report which layer broke. Unwind: §3.4.
- Run interrupted at repo scale → ledger (B40) makes it resumable.
- Remediation cap exhausted → escalate, never silently pass or loop.
- Tied-probe discard cap exhausted → degrade the axis to advise, proceed (B24).

### 3.4 Permission / environment

- **Read-only checkout, or a dirty worktree → refuse to apply, report only (E1 only).** In E2 the
  ticket's own uncommitted diff is the expected worktree state, so E2 refuses only on read-only
  checkouts and on modifications **outside the ticket's touched paths**. Without this scoping E2
  can never apply anything, since an in-flight ticket's worktree is dirty by construction.
- **Shared checkout with concurrent sessions** → no branch switching, no `git stash`, no
  `git reset`, at any time, including on failure paths and interrupts. (This repo is such a
  checkout.) **The only sanctioned unwind is `git revert` of the stack's head commits in reverse
  order on the current branch** — which is why B25's affordance is prefix truncation rather than
  arbitrary revert, and why reverting adds commits rather than removing them.
- Target repo has a CI coverage floor → deletions will trip it. None here, but `OPEN`: detect and
  warn on other repos.
- Monorepo with multiple projects → `OPEN-4`: is scope per-project or whole-tree?

### 3.5 Adversarial challenges recorded (not resolved — for af-intake-plan)

- **A1.** "Aggressive-then-validate" (R6) is only as safe as coverage. In untested regions no
  downstream gate exists. Mitigation: coverage-independent proofs stay in `af-clean`; only
  test-dependent proofs move downstream. **Unresolved whether that is sufficient.**
- **A2.** B9's same-job inventory has no precision evidence. Mitigated but not resolved by B9's
  advise-tier restriction and IF7.
- **A3.** R9 (centralize always) plus B22's aggression dial could produce a god-module even with
  B27's flag guard, because B27 catches *parameterized* failure, not *cohesion* failure.
- **A4.** Under R14, the portable anchor core has no calibration corpus. The only calibration data
  is this repo's report-only verdicts, which R14 says don't generalize.
- **A5.** The judge grades with verbosity bias while grading verbosity. B24's tied probes detect it
  but do not correct it; the discard cap bounds the consequence.
- **A6.** E2 auto-applies `enforce` findings with no human present. Mitigated by D8 (E2 ships
  report-only) and B42 (promoted checks cannot self-raise), not eliminated.
- **A7.** Nothing defines what happens when `af-clean` and the grader disagree persistently in E2 —
  the cleaner may be right and the anchors wrong (OPEN-9).
- **A8.** An 11.7 MB tracked `pg_dump` suggests the exemption manifest should also flag "why is this
  tracked?" as a finding, which is scope creep (OPEN-10).
- **A9.** B7's census allocates LLM attention by slop density, but B8's uninstrumented patterns are
  by definition invisible to that census — so the highest-judgment patterns are routed by a signal
  that cannot see them.
- **A10.** B9's job inventory is created fresh per target repo, so the first run on any repo has an
  empty inventory and finds no cross-file same-job violations — R9's posture is inert exactly when
  a repo is dirtiest, and compounds only over repeat invocations.

### 3.6 Adversarial target-repo challenges (the repo as attacker)

`af-clean`'s primary mode runs against arbitrary repositories with write and delete authority.
These are recorded, not resolved.

- **S1. Untrusted command execution (B2, B30).** Discovered test/lint/build commands and fetched
  detectors execute **in the operator's own privilege and network context, with no sandboxing.**
  Running `af-clean` on a repo is therefore equivalent to running that repo's own tooling. Stated
  as accepted risk; `OPEN-11` asks whether containerized execution is required before the tool is
  pointed at repos the operator does not control.
- **S2. Exemption and quarantine evasion (B3, B6, B20).** Exemption and quarantine signals are
  **repo-authored and therefore not adversarially robust** — a planted `@generated` marker or a
  stray string reference can protect code from review. Mitigation: the exemption manifest and the
  quarantine set are surfaced as reviewable evidence in the report, never as silent exclusions.
- **S3. Secret exposure (B6, B39, B40).** B6 enumerates every string literal and config value, and
  B40 persists findings to a store outside the target repo's access controls. Values matching
  common secret patterns (API keys, tokens, connection strings, private keys) are **redacted before
  being quoted into the corpus, findings, or ledger.** Secret-bearing files (`.env` and variants)
  are excluded from corpus enumeration.
- **S4. Praxis space isolation (B39).** Each target repo gets a **namespaced space keyed to repo
  identity, with no cross-space read access**, so one repo's job inventory, code excerpts, and
  string corpus cannot be read or reused by a run against a different repo. This is an acceptance
  condition on B39, not an aspiration.
- **S5. Detector supply chain (B2, D7).** Version pinning narrows but does not close the window —
  a package compromised at the pinned version still executes. No checksum verification or vetted
  mirror is currently specified.

---

## 4. Implied features surfaced

- **IF1. A dry-run/report-only mode as a permanent first-class mode**, not just a rollout phase.
- **IF2. A slop-density report per module** — *already produced as a byproduct of B7*, not
  separate work.
- **IF3. A test-debt map** — *already produced as a byproduct of B17's uncovered-reachable cell*,
  not separate work.
- **IF4. A "why is this tracked?" repo-hygiene lane** (committed scratch like `debug.log`, an
  11.7 MB SQL dump, five dead ESLint devDependencies). Deferred pending OPEN-10.
- **IF5. Dependency-level dead code** — unused packages, not just unused symbols.
- **IF6. CSS/asset reachability** — 4,551 lines of string-keyed classes are invisible to JS/TS
  tooling; deleting a component orphans its styles silently.
- **IF7. A calibration harness** — **now in scope (§1.3)**, because §0's aggression gate, OPEN-6's
  precision floor, and D8's E2 flip all depend on it.

---

## 5. Defaults taken (each flagged for override)

- **D1.** Ambiguous comments are **kept**. Basis: Ousterhout's 10–100× asymmetry. Contradicts the
  brief's surface reading — see OPEN-1.
- **D2.** Semantic-only same-job matches require human confirmation; lexical matches may auto-apply
  (subject to D9).
- **D3.** Squashed/message-less commit provenance is treated as **protective** (scar assumed).
- **D4.** The human entry point is **advisory + apply**: the validation step runs and renders
  findings, but does **not** enforce a go/no-go gate — the operator reviews and accepts or rejects
  independently. This is what distinguishes E1 from E2's axis-gated path.
- **D5.** Iteration cap defaults to the existing `DEFAULT_GRADED_ITER_CAP = 3`.
- **D6.** Rejections are keyed to the rejected symbol's own normalized source text and expire when
  **that symbol's** code changes, not on a timer and not on unrelated edits to its file.
- **D7.** Detectors are invoked zero-install (`uvx`/`npx -y`) with pinned versions.
- **D8. E2 ships report-only.** `enforce`-tier auto-apply in the unattended path is disabled until
  IF7 measures the enforce list's precision. Cross-referenced from B12 and A6. This does not
  weaken R13 — the *judge* still gates; only the *cleaner's* unattended edits are held back.
- **D9.** B9 same-job findings are `advise`-tier until IF7 reports a precision floor.
- **D10.** B45's per-layer findings cap defaults to N = 25.

---

## 6. Open decisions for af-intake-plan to force

- **OPEN-1.** Does D1 (keep-by-default comments) override the brief's "make comments terse"?
  *Checked:* the canon is explicit and asymmetric; the brief is explicit and opposite. Genuine fork.
- **OPEN-2.** Per-rule tier **ceilings** plus the witness predicate that earns `enforce` at
  instance level (reworded per B12 — a flat membership list cannot express D2).
- **OPEN-3.** Zero-history fallback for the co-change and parameter-accretion discriminators.
- **OPEN-4.** Monorepo scoping: per-project or whole-tree?
- **OPEN-5.** Does `af-clean` refuse to run, or degrade, when B4 finds no recognizable roots?
- **OPEN-6.** B9 precision floor — what match confidence permits consolidation at all? Also the
  **recall** floor, given B9's bounded-bucket retrieval contract.
- **OPEN-7.** Should `agent_factory/tests/` be added to CI as part of B38? Note that B33/B34/B35's
  acceptance criteria are **only** checkable there, so the flip's prerequisites are otherwise
  proven once locally and never re-proven.
- **OPEN-8.** Where does the validation step live — a new `af-*` skill, or a library both `af-build`
  and `af-clean` call? Note B29's "any pipeline" generality has no second confirmed consumer yet.
- **OPEN-9.** A7: resolution path when cleaner and grader persistently disagree in E2.
- **OPEN-10.** Does IF4 (repo-hygiene findings) ship, or is it deferred as scope creep?
- **OPEN-11.** Is sandboxed/containerized execution required before pointing `af-clean` at repos
  the operator does not control? (S1)
- **OPEN-12.** Is the isolated cleaner/verifier context a subagent or a fresh CLI invocation? (B46)
- **OPEN-13.** How is a grade-time-exempted universal requirement discharged so
  `all_validations_passed` does not stall on a permanently uncovered requirement? (B32)

---

## 7. Deferred / Open Questions

### From 2026-07-29 review

- **Should §2.7 (B32–B38) split out of this project?** Two reviewers independently argued yes:
  seven behaviors rebuild gating infrastructure for the *secondary* entry point, while the document
  itself states E1 is primary by usage volume, so the minimum shippable unit for the
  higher-volume human workflow is inflated by a `report_only` flip, a new path-predicate layer, a
  deadlock escape, a `graded_loop` reset, and five test fixes that serve none of E1's behaviors.
  **Counter-position (standing decision R13):** all three gating prerequisites are wanted inside
  this project. Recorded rather than applied — this asks to reverse an explicit human decision.
- **Should the document declare a phase-1 slice?** Proposed: scope B1–B8, B11–B15, B25 and IF1/IF2
  as report-only first — no applied diffs — and gate B9, B22–B24, B26, B37, B39–B42 on phase-1
  accept/reject data. Rationale: af-intake-plan otherwise receives 45 behaviors with no ordering,
  and the cheap high-value core (census + report) is what generates the calibration data every
  later behavior needs. **Deferred because sequencing interacts with R13** — a phase 1 that pushes
  §2.7 later contradicts the standing decision, so the phasing and the R13 question should be
  settled together.

---

## 8. Rigor mode

- `ce-ideate`: **ran** — six frames plus an axis-F recovery round, 58 raw candidates, 15 survivors.
- `ce-brainstorm`: **deliberately skipped** — subject was already specified past the point that
  skill addresses; recorded rather than silently omitted.
- Web research: **ran** — canon + empirical AI-slop taxonomy + LLM-judge reliability.
- Repo research: **ran** — 4 parallel passes; one empirically flipped the gate and measured the
  blast radius rather than reasoning about it.
- Adversarial pass: **ran** — A1–A10 recorded in §3.5, S1–S5 in §3.6.
- `ce-doc-review`: **ran** (round 1) — 6 personas (coherence, feasibility, scope-guardian,
  security-lens, product-lens, adversarial), 50 findings, 2 auto-applied, 31 applied on decision,
  2 deferred to §7, 6 FYI observations not actioned.
- Gap lenses: failure classes §3.1 fired; data-lifecycle §3.2 fired; rollback §3.3/§3.4 fired;
  permissions §3.4 fired; security §3.6 fired; who-pays-the-tradeoff — A6/D8 partially resolved.
