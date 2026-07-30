---
date: 2026-07-29
topic: af-clean
focus: a new /af-clean skill — aggressive AI-slop removal, dead-code purge, DRY/SRP enforcement, terse comments, reachability-based purge of unsurfaced code
mode: repo-grounded
---

# Ideation: `/af-clean` — aggressive AI-slop cleanup for the agent factory

## What the skill is

A new `agent_factory/skills/af-clean/` skill that aggressively cleans AI-generated code:
removes redundant try/except wrappers, deletes dead code, makes comments succinct,
enforces DRY and single-responsibility, and simplifies for readability. Aggressive but
behavior-preserving — aggression concentrated on AI slop, dead code, and known Claude
Code complaints.

Two user-stated requirements beyond the above:

1. **Reachability purge is first-class.** It must aggressively find dead functions and
   anything not surfaced at all in user-facing parts of the app, and purge those.
2. **Dual entry, one engine.**
   - **Human-invoked** over the entire repo, or over a path named at invocation time —
     including code that never went through `af-build`.
   - **Axis-invoked** by the existing `minimalism-dry` check after an `af-build` ticket,
     scoped to that ticket's diff, as that check's *remediation arm* (the check currently
     only judges; it has no way to fix).

Repo-scale operation (thousands of files, one invocation) is the design center for the
human entry point.

## Grounding Context

### Codebase context

Praxis — Python 3.12 + FastAPI backend (`knowledge/`), React dashboard (`frontend-react/`),
AWS CDK (`infra/`), plus `agent_factory/` (a Claude Code plugin: skills + hooks). Praxis is
a knowledge graph turning AI coding sessions into reviewed, queryable knowledge
(Postgres/pgvector), exposed over MCP. Also present: `docs/`, `specs/`, `scripts/`,
`tools/`, `migrations/`, `examples/`, `session-capture/`, and an older `frontend/`
alongside `frontend-react/`.

House style for af-* skills: `agent_factory/skills/<name>/SKILL.md`, multi-line frontmatter
`description` stating what the skill does, an explicit NEVER clause naming the adjacent
failure it exists to prevent, and routing pointers to sibling skills. **State lives ONLY in
Praxis** (facts/snapshots) — no JSON status files or lockfiles. Verification is expressed as
query-resolved checks: a check owns its own `meta.applies_to` predicate (tags / `["*"]`
wildcard / surface bind) plus a `run` command whose non-zero exit is failure; tickets never
name checks. `af-build` runs FIND→CLAIM→RESOLVE→BUILD→VERIFY→FINISH and ends with a
cold-eyes `ce-*` review panel. Focused single-purpose af-* skills run 125–244 lines;
orchestrators run 1000+.

### Prior art inside the repo (the decisive finding)

- **`agent_factory/seeded_checks.toml` already ships a universal `minimalism-dry` graded
  judge**, injected into every non-exempt ticket. Axes: minimalism 0.8, deduplication 0.8,
  dry 0.75, `confidence_floor = 8`. It carries **literal good/slop code anchors** (three
  good, three slop: speculative abstraction with one impl; copy-paste transform at every
  call site; dead code as computed-never-used locals plus an always-False flag). Judge
  prompt: *"Fail only on a LOCATED (file:line) instance"* and *"Do not re-run tests"* —
  subjective grading and exit-code checks are deliberately separate lanes.
  **`af-clean` is the remediation arm of this gate, not a second definition of slop.**
- `agent_factory/docs/plans/2026-07-22-001-feat-universal-minimalism-gate-plan.md`
  (completed): shipped **report-only first**, then flipped to gating with a one-line change.
  **Exemptions (`meta.universal_exempt`) are first-class** because a subjective fail on
  unchangeable generated/vendored code is content-hash-cached and **deadlocks the ticket
  forever**.
- Global `clean-code` skill — the house-style source the anchors came from. Cut-list:
  try/except that catches and re-raises with no new information; defensive null checks on
  inputs that can't be wrong (validate at boundaries only); helpers extracted for a single
  caller ("three callers earns the helper"); docstrings restating the function name;
  comments explaining WHAT instead of WHY; `# === HELPERS ===` banners; AI-tell vocabulary.
  Anti-DRY brakes: premature abstraction is worse than light duplication; two byte-identical
  bodies representing different concerns stay separate. Scope brake: don't widen scope.
  Skip-list: throwaway scripts, scratch files, generated code, vendored third-party.
- `agent_factory/docs/agent-coding-factory-reference.md` §5 Risks: **structural erosion over
  iterations (SlopCodeBench: 77% of runs)** → mitigate with per-iteration complexity-delta
  gates and structural sensors, i.e. a measured delta rather than an LLM's unaided opinion.
  **Self-preference bias** → an agent must not be sole judge of its own cleanup.
  **Reward/verification gaming** → the grader must not be the cleaner, or it will delete
  tests to shrink the diff. **Hallucinated APIs = 38% of failures** → pre/post AST validators.
- Praxis surface bindings exist as MCP tools (`praxis_ensure_surface`, `praxis_bind_surface`,
  `praxis_surfaces_for_requirement`, `praxis_surface_coverage`,
  `praxis_requirements_for_surface`, `praxis_checks_for_surface`) — surfaces are user-facing
  screens/endpoints bound to requirements. Plus `praxis_record_episode`,
  `praxis_record_outcome`, `praxis_add_insight`, `praxis_save_snapshot`.
- `docs/solutions/` currently holds only **3 entries** — nearly empty.
- Installed but not factory-integrated: `ce-simplify-code`, `ce-code-simplicity-reviewer`,
  `ce-maintainability-reviewer`, `/simplify`, `/review`, `/codex` (adversarial second
  opinion via the OpenAI Codex CLI).

### Measured facts about this repo

- **71 `getattr(` call sites across 616 tracked `.py` files.**
- `agent_factory/hooks/hooks.json:8` dispatches `build_completeness_gate.py` by string —
  **nothing imports it.** A naive Vulture/Knip purge deletes the factory's own enforcement
  machinery first.

### External prior art

- `atj393/code-cleanup` (Claude Code plugin): lead agent + 7 specialist subagents (dedup,
  dead-code, types, cycles, error-handling, AI-slop) + a final 11-dimension reviewer.
- SilenNaihin `/refactor` gist: `jscpd` → `knip` → LLM simplify → delete obsolete files →
  run tests → one isolated commit (a clean revert boundary).
- `dabit3/deslop`: detect-only git-diff scanner, severity-scored, CI exit-code gate.
- Ripplex: `semgrep + oxlint + jscpd + knip` on every PR.
- A crowded cluster of near-duplicate slop cleaners (ai-slop-cleaner, anti-ai-slop,
  desloppify) — none best-in-class. **The space is crowded but shallow on rigor.**
- Tooling map: dead code → Knip (JS/TS, now preferred over ts-prune) / Vulture / ruff;
  duplication → jscpd, pmd-cpd, similarity-ts; complexity → radon, eslint sonarjs; custom
  patterns → semgrep. **No deterministic tooling exists for comment terseness or
  single-responsibility** — those stay LLM judgment.
- arXiv 2604.23340: GPT-4 deleted three unrelated switch-cases during a refactor and broke
  test suites.
- **The unguarded gap in all prior art:** "tests still pass after deletion" is not proof of
  safety — it can equally mean the deleted code was under-tested.

## Taste Reference — the substantive canon

Deliberately **de-emphasizes Martin's *Clean Code* (2008)**: its most-quoted rules
("extract till you drop", 2–4 line functions, comments-as-failure) are now widely argued to
*cause* the over-fragmentation and one-caller-helper sprawl that reads as slop. It appears
below only where modern sources rebut it.

### Modern frameworks — operative content

**Ousterhout, *A Philosophy of Software Design* (2018/2021).** Complexity = f(dependencies,
obscurity); symptoms are change amplification, cognitive load, unknown unknowns.
- **Deep modules** (simple interface, powerful implementation — Unix file I/O hides huge
  complexity behind ~5 calls) over **shallow modules** (interface complexity ≈ functionality).
- **Classitis** — the belief that more/smaller classes is better design; produces shallow
  classes that add complexity without hiding anything.
- **Information leakage** (one design decision reflected in multiple modules) named one of
  the most important red flags. Ask: "what's the simplest interface that covers all my needs?"
- **Tactical tornado** — the prolific dev who ships fast but tactically, leaving a wake of
  destruction for maintainers. Describes an unsupervised coding agent exactly.
- **Define errors out of existence** — design interfaces so common-case semantics absorb
  what would otherwise be edge cases, instead of proliferating error handling.
- Comments are a **design tool**, not a failure signal; missing-comment cost claimed at
  **10–100x** wrong-comment cost.

**Dan North, CUPID (2021)** — explicit successor to SOLID, framed as properties to gravitate
toward rather than principles to comply with. **C**omposable (small surface,
intention-revealing, minimal deps); **U**nix philosophy (does one thing well — explicitly
rebuts SRP: "content and format change together," and separating rendering from logic often
creates artificial seams); **P**redictable (deterministic, observable); **I**diomatic
(language/team convention); **D**omain-based (types and directories mirror the problem
domain — `patient_history/`, not `models/views/controllers/`).

**Grug Brained Developer (2022).** "Complexity very very bad." Primary weapon: **"no."**
**FACTORY FACTORY FACTORY** satirizes stacked abstract-factory/visitor/bridge/proxy as the
canonical over-engineering failure. **80/20 solution** — 80% of value at 20% of complexity
cost, accept it isn't polished. Don't factor too early; wait for natural cut-points with
narrow interfaces. Mostly integration tests over unit tests, written after prototyping.

**Cognitive load (zakirullin/cognitive-load).** Working memory holds ~**4 chunks**; past
that, comprehension collapses. Argues fewer/deeper modules over many shallow ones,
self-descriptive names over cleverness.

**Locality of Behaviour (Carson Gross / htmx).** A unit's behavior should be obvious from
that unit alone. Explicitly trades against DRY and Separation of Concerns: strategic
duplication that keeps related logic co-located beats the spooky-action-at-a-distance that
premature DRY-ing causes.

**Sandi Metz, "The Wrong Abstraction" (2016).** "Duplication is far cheaper than the wrong
abstraction." Failure pattern: dev A extracts an abstraction; dev B faces a near-fit
requirement and *distorts* it with parameters/conditionals instead of reverting. Fix:
**"the fastest way forward is back"** — inline, flag per caller, delete what's unneeded.

**Rich Hickey, "Simple Made Easy".** *Simple* (one braid; objective) vs *easy* (near-at-hand;
relative). **Complecting** = braiding concerns so neither can be touched without
understanding/risking the other. Incidental complexity is self-inflicted.

**Tef, "Write code that is easy to delete, not easy to extend."** Most features change
within months or get cut; optimizing for deletability rather than speculative extensibility
is the more honest bet. Directly relevant to a deletion tool.

**Kent Beck, *Tidy First?* (2023).** Tidyings are small, reversible, **structure-only**
changes, reviewed separately from behavior changes so diffs stay cheap to reason about.

**Fowler smells live in LLM output:** speculative generality, shotgun surgery, divergent
change, feature envy, primitive obsession, middle man, long parameter list.

**Proverbs with real content:** Go — "a little copying is better than a little dependency",
"clear is better than clever"; Zen of Python; rule of three; YAGNI; Chesterton's fence;
"code is read far more than written."

### Where the authorities conflict (load-bearing)

| Conflict | Positions |
|---|---|
| Function/module size | Martin's tiny functions vs Ousterhout's deep modules — extreme decomposition creates shallow interfaces and entanglement, forcing readers to jump across many small methods. In their recorded debate (`github.com/johnousterhout/aposd-vs-clean-code`), Martin's decomposition of an Ousterhout method ran **3–4x slower** before he fixed it; Martin conceded Clean Code 1st ed. gave no guardrail for recognizing *over*-decomposition. |
| Comment density | Martin: an admission of failed naming. Ousterhout: necessary abstraction carriers, missing-comment cost >> wrong-comment cost. Neither fully concedes; Martin did find a real error in one of Ousterhout's comments. |
| DRY vs locality vs wrong-abstraction | Three camps agree DRY-as-dogma is dangerous, for **different reasons**: Metz fears premature abstraction from *accidental* duplication; Gross fears SoC scattering related logic; classic DRY still wins when duplication reflects one true domain concept. |
| When abstraction is earned | Rule-of-three / YAGNI vs up-front-design schools arguing some abstractions must exist before instances because retrofitting costs more. Grug splits it: wait for natural cut points, accept 80/20. |
| Error handling | Ousterhout's define-errors-out-of-existence vs defensive-programming instincts vs Go/Rust explicit propagation. **Key insight: broad defensive try/except is a smell precisely because it fails all three schools at once** — neither designed-out, nor explicit, nor minimal. |
| Configurability vs simplicity | CUPID's small surface and grug's "no" oppose the older instinct to make functions maximally configurable "for the future" — which AI resolves badly by adding parameters nobody calls. |

### AI/LLM slop taxonomy, with empirical backing

- **GitClear** (211M lines, 2020–2024): copy-pasted code **overtook refactored ("moved")
  code for the first time in 2024**; moved-code share fell 24.1% → 9.5%; duplicated blocks
  rose ~8x in 2024; churn (revised within 2 weeks) rose 3.1% → 5.7%. Framed as a
  "maintainability gap."
- **DORA 2025**: AI adoption correlates with *higher* individual code-quality and
  docs-quality scores (25% more AI use → +3.4% code quality, +7.5% docs quality) but *also*
  delivery instability — bugs/developer up 54%, incidents/PR up ~243%. "AI amplifies
  existing team practices, doesn't fix them."
- **arXiv 2605.22976** — "LLM Code Smells": 73.55% of 692 LLM-integrating OSS projects show
  ≥1 LLM-specific smell across 171k files.
- **arXiv 2605.02741** — "AI-Generated Smells": a distinct **machine signature** of technical
  debt, a *reasoning-complexity trade-off* where **more capable models produce MORE method
  bloat** on complex logic, packing it into single procedural blocks — the inverse of the
  human failure mode (poor encapsulation).
- **Practitioner convergence** (deslop, Aviator, "AI slop has a shape" genre) on specific
  tells: over-broad try/except swallowing errors silently; comments restating code; verbose
  docstrings on trivial functions; speculative abstraction for a single call site;
  backward-compat shims with zero real callers; stdlib reimplementation; over-parameterized
  functions with unused knobs; "enhanced/robust/comprehensive" naming inflation; excessive
  logging; redundant null/type guards on trusted paths; dead feature flags; mock-asserting
  tests; section-banner comments; duplicated near-identical helpers across files.

### LLM-judge reliability (constrains any taste grader)

- Pairwise beats absolute for relative discrimination **but has no floor** — a uniformly-bad
  pair still yields a winner.
- Absolute scoring needs **calibrated anchors** and dimension-labeled criteria, not one
  holistic score.
- Documented biases: **position bias** (mitigate by order-swapping); **verbosity bias**
  (judges reward longer output regardless of value — acutely dangerous when grading
  verbosity itself); **self-preference** (same-family favoritism — hide model identity).
- Converged best practice: **require a cited file:line instance**, never a bare verdict.
  Unanchored verdicts are where verbosity and position bias leak in hardest. This is exactly
  what `seeded_checks.toml` already enforces.

### Empirical readability research (thin — say so plainly)

- A **275-participant study**: reduced nesting measurably decreases comprehension time and
  increases bug-finding confidence.
- Eye-tracking on extract-vs-inline for novices: **mixed, task-dependent** — no clean
  mandate for either aggressive extraction or deep modules.
- Full-word identifiers reliably beat abbreviations.
- **Bottom line: tiny-functions and deep-modules are both argued from experience and
  cognitive-load theory, not from controlled comprehension experiments.** The debate outruns
  its evidence base, and the rubric should say so.

### Sources

- https://github.com/johnousterhout/aposd-vs-clean-code
- https://dannorth.net/blog/cupid-for-joyful-coding/
- https://grugbrain.dev/
- https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction
- https://github.com/zakirullin/cognitive-load
- https://htmx.org/essays/locality-of-behaviour/
- https://programmingisterrible.com/post/139222674273/write-code-that-is-easy-to-delete-not-easy-to
- https://www.gitclear.com/the_ai_code_quality_maintainability_gap
- https://www.gitclear.com/ai_assistant_code_quality_2025_research
- https://dora.dev/insights/balancing-ai-tensions/
- https://arxiv.org/abs/2605.22976
- https://arxiv.org/html/2605.02741
- https://arxiv.org/pdf/2605.09227

## Topic Axes

- **A. Slop taxonomy & shared rule source** — staying in lockstep with `minimalism-dry` and `clean-code` rather than forking taste into a third divergent definition.
- **B. Detection substrate** — deterministic tooling under the LLM pass; which tool maps to which pattern; what stays irreducibly LLM judgment.
- **C. Reachability & purge of unsurfaced code** — finding what no user-facing entrypoint reaches; Praxis surfaces as oracle; dynamic-dispatch blind spots.
- **D. Safety & behavior preservation** — test gates, coverage-aware deletion, AST validators, exemptions, rollback, separating cleaner from verifier.
- **E. Invocation, scope & repo-scale operation** — dual entry, scoping, chunking, budget, resumability, what's recorded in Praxis, write boundary.
- **F. Substantive taste content** — which named principles the rubric encodes, how authority conflicts resolve, how qualitative judgment becomes decidable and stays honest.

## Ranked Ideas

### 1. Coverage-witnessed tri-state deletion — never a binary dead/alive verdict
**Description:** Every candidate gets one of three verdicts. **Covered + unreferenced →
delete** (the green suite genuinely testifies). **Uncovered + unreferenced → quarantine**,
never auto-deleted, emitted as a test-debt finding. **Live → keep.** Sharpest mechanism:
replace the body with a loud tombstone, run the suite with coverage, read a *three*-way
signal — failure means live; pass-with-coverage-hit means a test executed it but asserted
nothing (a test-quality finding); pass-with-zero-coverage means silence, not safety.
**Axis:** D
**Basis:** `external:` the one gap in all surveyed prior art — "tests still pass after
deletion" is not proof of safety; it equally means the code was under-tested. Precedent:
arXiv 2604.23340, GPT-4 deleted three unrelated switch-cases during a refactor and broke
suites.
**Rationale:** Dissolves the aggressive-vs-safe tradeoff instead of splitting it — aggression
is unbounded on bucket one and zero on bucket three. Also the only real differentiator
against an already-crowded field.
**Downsides:** Requires a coverage run as a hard precondition; tombstone-per-symbol means N
suite runs unless batched/bisected. Will honestly report "I don't know" on a large fraction
of a repo like this one.
**Confidence:** 90%
**Complexity:** Medium
**Status:** Unexplored

### 2. af-clean defines zero rules of its own — rubric compiled from `seeded_checks.toml`
**Description:** SKILL.md carries procedure only; taste loads at runtime from the TOML
anchors plus the `clean-code` cut-list and its anti-DRY brakes. A drift test fails if
SKILL.md restates a rule the TOML owns. Human accept/reject during a run writes back as a
new anchor pair, so a cleanup session sharpens the per-ticket gate.
**Amended by F4:** the TOML's axes are the *wrong shape* (unidirectional shrink). This
becomes "read from the TOML **and amend its axis shape first**."
**Axis:** A
**Basis:** `direct:` `agent_factory/seeded_checks.toml` already ships the `minimalism-dry`
judge with literal three-good/three-slop anchors, `confidence_floor = 8`, and the house rule
"Fail only on a LOCATED (file:line) instance."
**Rationale:** Dual entry makes this close to mandatory — the per-ticket arm *is* the axis
check's remediation arm, so divergent taste means cleaning a file could make its own gate
fail. Turns af-clean from a third competing definition into the write-back path that raises
af-build's floor.
**Downsides:** Anchors were tuned for ticket diffs, not whole files. Commits the team to the
TOML as the factory's canonical taste artifact — a bad anchor then fails every ticket.
**Confidence:** 88%
**Complexity:** Low-Medium
**Status:** Unexplored

### 3. Surface-rooted reachability, guilty-until-proven-reachable, with a dynamic-entrypoint ledger and staged excision
**Description:** Roots come from real entrypoints (FastAPI routes, MCP tool registrations,
console scripts, `package.json`, React router targets, CDK, cron) unioned with Praxis surface
bindings; everything unreached is a candidate. Three guards: **string-corpus quarantine** (a
bare symbol name appearing in any string literal, config value, or template → quarantine,
not purge — asymmetric costs justify treating evidence-of-liveness as sufficient without
proof); a **persistent liar ledger** where every human veto must name *why* it was reachable,
codified as a semgrep rule or explicit root so the same false positive is impossible next
run; and **staged excision** — recompute reachability after each purge round, because helpers
reachable only *through* a dead root become dead once it's gone. The surface cross-check is
three-way, so af-clean also reports stale bindings and missing ones, repairing the graph
rather than only consuming it.
**Axis:** C
**Basis:** `direct:` **71 `getattr(` sites across 616 tracked `.py` files**, and
`agent_factory/hooks/hooks.json:8` dispatches `build_completeness_gate.py` by string —
nothing imports it, so a naive Vulture/Knip purge deletes the factory's own enforcement
machinery first.
**Rationale:** The highest-severity failure af-clean can produce, and exactly where
Knip/Vulture are blind by construction.
**Downsides:** Most expensive survivor. Quarantine gets noisy on short/common names. Staged
iteration multiplies runtime.
**Confidence:** 78%
**Complexity:** High
**Status:** Unexplored

### 4. Deterministic census first — the meters are the allocation function, not the verdict
**Description:** Run Vulture/ruff, Knip, jscpd, radon, and custom semgrep rules repo-wide as
a cheap census; score slop density per file; send only hotspots to LLM judgment, and only for
the irreducibly-subjective patterns. Ship an explicit instrument × pattern matrix with a
first-class **uninstrumented list**, so the LLM's unique contribution is a required output
section rather than an optional flourish. Worth prototyping: a comment-restatement detector
scoring token overlap between a comment and the identifiers it precedes, minus an
external-referent whitelist (`because`, ticket IDs, URLs, units/invariants).
**Axis:** B
**Basis:** `external:` Ripplex runs `semgrep + oxlint + jscpd + knip` on every PR;
`direct:` no deterministic tooling exists for comment terseness or single-responsibility.
**Rationale:** The only route to honest whole-repo scope inside a finite context budget.
Without it, af-clean gets evaluated on the easily-verified part and quietly becomes an
expensive `ruff` wrapper.
**Downsides:** Commits the repo to maintaining a Python+TS toolchain; the comment heuristic's
false-positive rate is unproven and should be measured on real Praxis code first.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 5. The cleaner is not the applier — witness-tiered apply, blind verification
**Description:** The LLM proposes; a deterministic applier lands only proposals carrying a
machine-checkable witness. Tier 1: a named tool's located finding. Tier 2: AST no-reference
proof plus positive execution evidence. Tier 3: a survived tombstone. No witness, no delete —
downgraded to report. **Aggression becomes a legible knob: lower the required witness tier.**
Hardenings: the verifier receives only the diff and repo — never the cleaner's rationale — so
it must independently re-derive each hunk's safety argument; any hunk touching a test file or
reducing total assertion count is auto-rejected structurally; `/codex` is a natural
adversarial Defender. **Plus a null control:** seed each grading batch with **tied probes** (a
version paired against itself, or a cosmetic variant). Systematic preference on tied probes →
discard the batch. Order-swapping detects position bias but nothing in the standard
mitigation set *measures* whether bias survived.
**Axis:** D
**Basis:** `direct:` `agent-coding-factory-reference.md` §5 — self-preference bias means an
agent must not judge its own cleanup; reward gaming means "the grader must not be the cleaner,
or it will delete tests to shrink the diff"; hallucinated APIs are 38% of failures → pre/post
AST validators.
**Rationale:** Passing the rationale along is what makes two-agent review theater. Withholding
it is the cheap mechanical change that makes the second opinion independent.
**Downsides:** Roughly doubles read cost per deletion; discarding a batch on a failed probe
costs real tokens at repo scale.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 6. Risk-stratified commit stack + a Praxis findings ledger with sticky rejections
**Description:** Instead of one cleanup commit (correct for a small refactor, unreviewable at
repo scale), emit an ordered stack by risk class — comment edits → dead-import removal →
covered-unreferenced deletions → DRY consolidations → behavior-adjacent simplifications — so
the human can stop the stack at any layer and keep everything below it. Revert granularity
should match review granularity. Every finding is a Praxis fact keyed by content hash +
symbol id + rubric version: unchanged-and-cleared files skip entirely, making run two cheap
and a long run resumable; **a declined finding becomes a durable rejection** so af-clean stops
re-litigating intentional duplication every invocation.
**Axis:** E
**Basis:** `direct:` the completed minimalism-gate plan shipped report-only then flipped with
a one-line change; state lives only in Praxis; subjective verdicts are already
content-hash-cached.
**Rationale:** What kills cleanup tools isn't detection quality — it's that nobody dares merge
a 400-file diff, and that run two is identical noise.
**Downsides:** Inherits the known content-hash deadlock hazard the team already got burned by;
rejection scope and expiry need deciding (too loose silently suppresses real slop forever, too
tight evaporates on a whitespace change). Commit taxonomy is a taste argument.
**Confidence:** 82%
**Complexity:** Medium
**Status:** Unexplored

### 7. Cleanup output is negative work — each recurring pattern becomes a build-validation check
**Description:** For every pattern af-clean fixes more than once, it delegates to
`af-intake-build-validation` to author a `run`-command check that makes the pattern
un-reintroducible. A one-time O(repo) pass becomes a permanent O(1) constraint, and the
ledger's history makes af-clean the repo's structural-erosion instrument.
**Axis:** E
**Basis:** `direct:` SlopCodeBench — structural erosion in **77% of runs**, mitigated by
measured deltas rather than an LLM's unaided opinion. `af-intake-build-validation` is the sole
writer of `building-validation`, and checks own their own `meta.applies_to` predicate.
**Rationale:** Answers "is af-build making this codebase worse over months," which the factory
currently cannot check.
**Downsides:** A cleanup skill that can gate all future builds is a permissions conversation,
and it collides with the single-writer lock unless strictly delegated. Build last.
**Confidence:** 70%
**Complexity:** Medium
**Status:** Unexplored

### 8. One root question: cognitive load. Named principles demoted to *diagnoses*
**Description:** The rubric has exactly one root question — "how many independent things must
a reader hold in mind to predict what this unit does?" — and a finding is only well-formed if
it **enumerates the chunks**. Information leakage, classitis, complecting, the wrong
abstraction, non-local behavior become *labels on a diagnosis*, never the argument. "Violates
CUPID" stops being a complete sentence.
**Axis:** F
**Basis:** `direct:` working memory holds ~4 chunks; past that comprehension collapses
(zakirullin). It's the one frame every modern authority shares — Ousterhout's complexity =
f(dependencies, obscurity), Hickey's complecting, and locality-of-behaviour are all statements
about what a reader must hold at once.
**Rationale:** A flat checklist of ten principles hands an LLM ten independent licenses to
find something — a false-positive generator. One root with a countable unit forces every
finding through a bottleneck a human can check in seconds, and gives conflicting principles a
common currency to be *traded off* in rather than merely listed. This is the answer to
"open-ended guidelines": not a longer list, one question with a canon attached.
**Downsides:** Choosing the root question fixes the skill's whole voice — every later rule
inherits it, so it can't be deferred. Chunk-counting is itself a judgment, just a far more
legible one.
**Confidence:** 85%
**Complexity:** Low-Medium
**Status:** Unexplored

### 9. Evidence tier buys edit authority — `enforce` vs `advise`, hard-filtered at apply
**Description:** Each rule carries an evidence tag that mechanically determines what af-clean
may *do* with it. `enforce` (auto-apply) is reserved for near-zero-regret, judgment-free
findings: zero-reference dead code, try/except that re-raises unchanged, section banners,
docstrings restating the signature, nesting reduction. `advise` (report only, never a diff)
covers everything the canon argues about — extract vs inline, module depth, cross-file DRY,
whether an abstraction is "wrong." In the af-build ticket-diff entry point **only `enforce`
may auto-apply**, since no human is in the loop to read the advisory half.
**Axis:** F
**Basis:** `direct:` only reduced nesting has real experimental support (275-participant
study) and full-word identifiers beat abbreviations; extract-vs-inline eye-tracking is mixed
and task-dependent; tiny-functions and deep-modules are *both* argued from theory, not
controlled experiments.
**Rationale:** "Aggressive" and a thin evidence base are compatible only if aggression is
*allocated* rather than uniform — and the allocation key is regret cost, lowest exactly where
the experts don't disagree. Most rubrics express confidence as prose hedging, which a model
flattens into uniform authority; binding the tier to a permission makes the epistemic label
mechanically consequential.
**Downsides:** The membership of the `enforce` list *is* the product, and one wrong entry is a
repo-wide bad refactor.
**Confidence:** 87%
**Complexity:** Low-Medium
**Status:** Unexplored

### 10. Encode the conflicts as discriminators and forbidden inferences — never as a winner
**Description:** Two mechanisms. Each conflict carries a **discriminating question with an
observable**, not a preference — DRY vs locality resolves by asking whether the duplicated
sites *co-change in git log* (one domain decision → DRY) or drifted independently (shared
shape only → leave alone). No observable → the finding is dropped, not guessed. And each
principle ships bolted to its **forbidden inference**: "deep modules" may not justify pushing
a body past a length or nesting ceiling; "the wrong abstraction" may not justify inlining
something with three or more live callers. One deterministic sensor to build here: the wrong
abstraction is detectable from **parameter accretion in history** — a helper whose parameter
list, boolean flags, and branch count grew across commits while its call-site count barely
moved, with each caller passing a distinct flag combination. That is Metz's exact failure
pattern, invisible to a snapshot judge, and it prescribes the repair direction ("the fastest
way forward is back").
**Axis:** F
**Basis:** `direct:` the three anti-DRY camps agree DRY-as-dogma is dangerous for *different*
reasons; and in the recorded Ousterhout↔Martin debate Martin's decomposition ran 3–4x slower
before he fixed it while Martin conceded Clean Code gave no guardrail against
*over*-decomposition. Both sides' unqualified rule is known to misfire.
**Rationale:** A principle handed to an LLM as an unqualified maxim is a license, not a
constraint — the model will find the reading that authorizes the largest visible improvement.
**Downsides:** Co-change history doesn't exist for freshly generated code, which is af-clean's
main target — needs a stated fallback for zero-history duplicates.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 11. Slop is signed: bloat and fragmentation are opposite poles; direction declared before editing
**Description:** Replace the single shrink-oriented "minimalism" scalar with a signed axis
around interface-to-implementation depth. A finding is invalid unless it names which pole the
code sits at, because the two remedies are exact opposites — an undirected "make it smaller"
push converts a monolith into helper sprawl and calls it progress. Backed by a mechanical
brake: a **no-net-collapse invariant** rejecting any edit that reduces line or file count
while pushing a function past a nesting/length ceiling or raising mean branch count.
**Axis:** F
**Basis:** `direct:` arXiv 2605.02741's machine signature — a reasoning-complexity trade-off
where **more capable models produce *more* method bloat** on complex logic, packing it into
single procedural blocks, the inverse of the human failure mode. The practitioner tell-list
simultaneously contains speculative abstraction for one call site (fragmentation) and
over-parameterized verbose blocks (bloat).
**Rationale:** The shipped `minimalism-dry` axes are all unidirectional-shrink, and on
Claude-authored code the dominant defect is bloat *within* units — which a shrink-only rubric
mistreats by extracting rather than restructuring. The cleaner's natural success metric
(smaller diff, fewer files) is precisely what method bloat optimizes, so something outside the
LLM must hold the other end. **This is a direct amendment to a gate already in production.**
**Downsides:** Picking the ceilings is a taste call with teeth — too tight blocks legitimate
consolidation, too loose makes the invariant decoration.
**Confidence:** 83%
**Complexity:** Medium
**Status:** Unexplored

### 12. Comment triage by information gain, with keep-by-default stated out loud
**Description:** Implement "terse comments" as per-comment classification, never a density
target. Keep anything recording WHY, a non-obvious invariant, a cost, or a rejected
alternative. Cut paraphrases of adjacent identifiers, section banners, and docstrings
restating the function name — operationalized as: a comment is deletable only if its content
words are a near-subset of the identifier tokens it annotates; a comment introducing tokens
absent from surrounding code is presumed to carry WHY and is protected. **Ambiguous comments
survive.**
**Axis:** F
**Basis:** `direct:` the comment conflict is unresolved but isn't a tie — Ousterhout puts
missing-comment cost at **10–100x** wrong-comment cost, an explicit asymmetric loss function.
"Comments restating code" is a top-tier AI tell across three independent write-ups, and DORA
found AI *raises* docs-quality scores (+7.5%) — so volume isn't the defect, redundancy is.
**Rationale:** Comment stripping is where an aggressive cleaner does its most *irreversible*
damage — deleted WHY is unrecoverable from the code — and a naive reading of "make comments
succinct and terse" points straight at it.
**Downsides:** **Contradicts the surface reading of the brief** — needs explicit sign-off. The
lexical-subset test will false-positive on well-named code carrying necessary WHY.
**Confidence:** 78%
**Complexity:** Low
**Status:** Unexplored

### 13. Scar detection — git provenance as a Chesterton's-fence brake on defensive-code removal
**Description:** Before removing anything in the defensive family (try/except, null guard, odd
special case, narrow branch), blame the lines and inspect the introducing commits. A commit
message matching fix/bug/regression/hotfix/incident, or referencing an issue or PR → treat as
a **scar, not slop**: demote to advisory and cite the commit. In ticket-diff mode it also
catches a build agent re-removing a guard a prior ticket added.
**Axis:** F
**Basis:** `reasoned:` a redundant guard and a hard-won guard are *lexically identical* in the
code and distinguishable only in history; provenance is the only cheap oracle that separates
them, and it already sits in the repo. `direct:` over-broad defensive try/except is the #1
convergent slop tell — exactly the family af-clean will be most aggressive about.
**Rationale:** The most aggressive rule in the skill is the one most likely to delete
institutional memory. This gives it a deterministic brake that costs nothing when the code
really is slop.
**Downsides:** Commit-message quality varies; squashed merges and message-less commits need a
stated default (permissive or protective).
**Confidence:** 82%
**Complexity:** Low
**Status:** Unexplored

### 14. A second rubric aimed at af-clean's own diff — cleanup-flavored slop
**Description:** af-clean carries a second taxonomy enumerating the distinct slop *an
aggressive cleaner produces*: clever compression (nested comprehensions, ternary chains,
walrus golf) where a plain statement was clearer; identifiers shortened to abbreviations; WHY
comments lost; procedures over-collapsed; error handling removed such that a failure now
surfaces less information than before. This second rubric gates the apply phase and is scored
independently of the first — a finding can be a true slop hit and still have its fix rejected.
**Axis:** F
**Basis:** `direct:` Go's "clear is better than clever"; full-word identifiers reliably beat
abbreviations in comprehension studies. `reasoned:` nothing in the input-side taxonomy
penalizes tidy-but-worse output, so a cleaner optimizing only against it converges on
compressed code that grades well and reads badly — the failure mode needs its own named
taxonomy or it is invisible to the system.
**Rationale:** The difference between removing slop and translating it into a second dialect
no existing check in the repo can see.
**Downsides:** Whether this rubric can *veto* fixes or only annotate them determines whether
af-clean is net-conservative or net-aggressive in practice.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 15. A job inventory in Praxis — semantic same-job detection, not lexical clone detection
**Description:** R9's rule ("if a job is covered by one function, that logic is never written
again anywhere else") is a *semantic* claim, and no tool in the detection substrate can check it.
`jscpd`, `pmd-cpd`, and `similarity-ts` find **lexical** clones — near-identical text. They are
blind to the same job implemented twice with different variable names, different control flow, or
in a different language. That blindness is precisely where the signature AI failure lives: the
model re-implements a helper that already exists because it never knew it was there. So `af-clean`
needs a **job inventory** — a queryable index of "what jobs does this repo already know how to
do, and where does each live" — and a same-job check against it, not just a clone report. Praxis
is the natural home: it is already a graph with embeddings (pgvector), already the factory's
single source of truth, and already holds surfaces and requirements the jobs bind to. Each
invocation enriches the inventory, so the check gets sharper over time.
**Axis:** B (with A and E adjacency)
**Basis:** `direct:` R9 defines the unit of identity as the job, not the text, and `direct:` the
tooling map contains no semantic-duplicate detector — jscpd/pmd-cpd/similarity-ts are all
lexical. `external:` GitClear's finding that copy-pasted code overtook refactored code in 2024
and duplicated blocks rose ~8x measures the *lexical* half only; the re-implementation half is
unmeasured because nothing cheap detects it.
**Rationale:** Without this, `af-clean` enforces R9 only where the duplicate happens to look
alike — which is the easy half and not the half that accumulates. It is also the one requirement
here that genuinely needs Praxis rather than merely using it, which makes it a fit for this repo
specifically rather than a generic cleanup tool.
**Downsides:** The most speculative item in the doc. Same-job identity is fuzzy at the edges, and
an embedding-similarity index will produce false "same job" matches that, under R9's aggressive
posture, become *wrong consolidations* — the highest-cost error class in the whole design.
Needs a precision floor and probably human confirmation before consolidating on a semantic-only
match.
**Confidence:** 60%
**Complexity:** High
**Status:** Unexplored

## Resolved Decisions

**R1. Invert, don't replace — `minimalism-dry` stays; `af-clean` becomes its remediation arm.**
(2026-07-29) The graded judge keeps its detector-and-grader role; `af-clean` fixes what the
judge locates; the judge then re-grades independently. Rationale: replacing the judge with the
cleaner would collapse grader and cleaner into one agent, which
`agent-coding-factory-reference.md` §5 forbids outright ("the grader must not be the cleaner,
or it will delete tests to shrink the diff") and which idea 14 exists to defend against.
They are also different lanes mechanically — `kind = "graded"` (subjective, axes/thresholds/
anchors) vs the exit-code `run` lane a check shelling out to `af-clean` would occupy.

**R2. Grader and cleaner are separate agents.** (2026-07-29) Locked. Ideas 5 and 14 are load-bearing,
not optional hardening.

**R3. Amend `minimalism-dry`'s axis shape before `af-clean` reads it.** (2026-07-29) Resolves the
former open decision #3. All three current axes (minimalism 0.8, deduplication 0.8, dry 0.75)
point the same direction — shrink — and idea 11's evidence says the dominant defect in
Claude-authored code is bloat *within* units, which a shrink-only rubric answers by extracting
helpers, manufacturing fragmentation slop. A depth/fragmentation axis that can fail in the
*other* direction lands first.

**R4. The cleaning step must actually gate.** (2026-07-29) `seeded_checks.toml:104` still reads
`report_only = true` — the check has never blocked anything. Flipping it to `false` is now an
explicit requirement of this work, not a later option. See newly-opened questions N1–N5 below:
the flip is what makes most of the remaining risk real.

## Open Decisions

### Newly opened by the gating flip (R4) — mostly now resolved

- **R5. Deadlock escape = iteration cap.** (2026-07-29) Resolves N1. A slop finding `af-clean`
  cannot fix within the cap stops consuming iterations and escalates rather than deadlocking the
  ticket. Cap value and escalation target still to specify.
- **R6. Clean runs BEFORE validation, deliberately aggressive.** (2026-07-29) Resolves N2's
  ordering. De-slopping runs first and pushes hard on deduplication and decentralization;
  downstream validation is what catches and repairs whatever that breaks. This is an explicit
  posture choice — aggression up front, safety downstream — not an oversight. See risk RK1.
- **R7. CI blast-radius audit is a prerequisite unit.** (2026-07-29) Resolves N3. The full-suite
  audit lands FIRST, before the gate flips, mirroring the prior plan's U4.
- **R8. Cleaning and validation are separate steps.** (2026-07-29) Resolves N4.
  - `af-clean` never runs tests or validation fixes. It is a mutation engine.
  - **Validation becomes its own decomposable, optionally-attachable step with remediation
    bundled in** — it runs the checks, reruns the tests, and fixes what breaks.
  - When `af-build` triggers `af-clean`, validation does NOT run inline — `af-build` already
    validates at the end of its run.
  - When a human invokes `af-clean` directly, it must trigger that centralized validation step
    itself, since no `af-build` run exists to do it.
  - Net effect: three distinct agents — grader (`minimalism-dry`), cleaner (`af-clean`),
    fixer (validation+remediation step). Reinforces R2.
- **N5. Remediation wiring exists?** Still unverified: whether `af-build`'s loop supports
  triggering a step on graded-check failure, or whether that path has to be built. Answerable
  from the repo.

- **R9. One job, one home — centralization by JOB IDENTITY, in small single-purpose files.**
  (2026-07-29) The precise formulation, superseding a looser first pass at this decision:
  - **Single source of truth per job.** If a job is covered by one function, the logic for
    running that job is never written again anywhere else. Re-implementation is the defect.
  - **Small, single-purpose files.** Centralization is emphatically NOT god-modules or
    dumping-ground utils. Units stay small and do one thing.
  - **Centralized by file structure.** Discoverability comes from organization — a job has an
    obvious home you can find — which is CUPID's Domain-based property, not a flat `utils/`.

  This is a *semantic* rule, not a lexical one: the unit of identity is the **job**, not the
  text. Two byte-identical bodies serving different jobs legitimately stay separate; two
  differently-written implementations of the same job are a violation even with zero textual
  overlap. Idea 10's co-change discriminator is therefore not a two-way decision procedure —
  same-job means centralize, full stop — and it survives only as a detector for the failed
  case in RK4.
- **R10. The validation step is more than running tests.** (2026-07-29) Confirmed. Contents:
  (1) full test suite, (2) typecheck + lint, (3) AST reference sweep, (4) reachability re-check
  (staged excision means round N's deletions create round N+1's orphans), (5) the query-resolved
  `building-validation` check lane, (6) **the `minimalism-dry` re-grade — moved here from
  `af-build`'s lane**, (7) bounded remediation loop under R5's iteration cap, (8) coverage
  report per R11.

- **R11. Coverage is an EVIDENCE oracle, never a liveness oracle — so it does not gate
  deletion.** (2026-07-29) Coverage cannot be a deletion gate, because a test exercising a
  symbol is not proof any user path reaches it; tests routinely cover dead code, so gating on
  coverage would *protect* dead code that happens to have a test. The two signals are
  orthogonal and both are needed:
  - **Reachability from user-facing entrypoints** (routes, MCP tools, CLI, component tree, cron,
    unioned with Praxis surface bindings) answers *is it used?* — this is the liveness oracle
    and the thing that gates.
  - **Coverage** answers *does the suite have any authority over this deletion?* — it decides
    whether a deletion is **proven** or merely **suspected**, and never whether the code is live.

  | | Covered | Uncovered |
  |---|---|---|
  | **Reachable** | Live — keep | Live — keep; record test debt |
  | **Unreachable** | **Dead with a test tombstone** — delete the symbol *and* its exclusively-covering test; coverage here makes the deletion provable, not protected | Dead but unproven — **quarantine** (idea 1's third bucket) |

- **R12. Test deletion is a first-class cleanup output, and the unreachable+covered cell is
  expected to be POPULOUS.** (2026-07-29) The stated expectation is many features with good test
  coverage that the app never uses; in every such case both the feature and its tests go. Three
  consequences:
  - Idea 5's structural rule ("auto-reject any hunk touching a test file or reducing total
    assertion count") is too blunt as written — it would block the main event. It inverts to:
    **a test deletion is permitted only when bound to an unreachable-symbol deletion in the same
    atomic unit; a test deletion not so bound is auto-rejected.** Binding, not abstention.
  - **Falling coverage percentage and falling assertion count are EXPECTED SUCCESS SIGNALS**, not
    regressions. Any metric-based guard keyed on either is invalid here.
  - **A CI coverage floor would fight this work.** Deleting a well-tested unused feature removes
    covered lines, so a repo-wide coverage-percentage threshold trips on `af-clean` doing exactly
    what it is supposed to do. Whether such a floor exists is a research item (see R7 scoping).

  This supersedes the loose reading of idea 1 in which "covered + unreferenced → delete" left
  the test standing. It also revives raw candidate C3 (delete the symbol and its test together),
  which the first critique pass rejected as inviting test deletion. Narrow carve-out with a
  checkable guard: **a test may be deleted only inside the same atomic unit as the unreachable
  symbol it exclusively covers, never as an independent action** — which keeps idea 5's
  structural rule (auto-reject any hunk touching tests or reducing assertion count) intact for
  every other case.

- **R13. Gating confirmed, with all three prerequisites in scope for this project.**
  (2026-07-29) The flip lands in this project, not as a follow-on. The three prerequisites are
  gating units: (1) the **path-predicate exemption layer** (F-C — none exists today; `_universal_exempt`
  needs a diff-paths argument and `contract_with_floor`/`start_ticket` must supply touched paths),
  (2) the **coverage-authoring fix** so a gating universal actually gets a pinned covering
  validation (F-A blocker 1), (3) the **cache-hit deadlock escape** (F-B — a cache hit currently
  hard-codes `should_block=False` and consumes no iteration), plus the `graded_loop` reset bug.
  The measured 5-test blast radius is a sub-hour cleanup, not a risk.

- **R14. `af-clean`'s PRIMARY use is other repositories — portability is a first-class
  requirement.** (2026-07-29) This reframes a large amount of the design. Praxis is where it's
  being built, not where it will mostly run. Consequences:
  - **The F-D slop inventory is illustrative, not load-bearing.** The `frontend-react/` findings
    (zero `console.*`, no catch-and-rethrow, good comment hygiene, no router, no dynamic imports)
    describe *this* codebase. None of it generalizes, and the "static reachability is unusually
    trustworthy here" advantage definitely does not.
  - **Nothing about the environment may be assumed.** Tooling presence, language mix, framework,
    test runner, lint config, and directory conventions all have to be **discovered per repo**.
    The zero-install pattern this repo already uses (`uvx ruff@0.15.20`, `npx -y`) is the model.
  - **Reachability roots must be discovered, not hardcoded.** This repo's root set (FastAPI
    closures inside one `create_app`, `@mcp.tool()` decorators, yoyo migrations, Claude Code
    hooks, SKILL.md-invoked CLIs) is idiosyncratic. A portable engine needs framework detection
    (FastAPI/Flask/Django/Express/Next/Rails/…) plus a generic decorator-and-registry sweep.
  - **The exemption manifest must be auto-derived, not authored.** Per repo, from `.gitignore`,
    `.gitattributes` `linguist-generated`/`linguist-vendored`, `@generated`/"DO NOT EDIT" markers,
    lockfile names, vendor/build directory conventions, and detected codegen output.
  - **The Vulture whitelist must be generated, not hand-written** (F-D makes an unwhitelisted run
    unusable here; it will be unusable elsewhere for different reasons).
  - **Anchors split into two tiers:** a portable core set that encodes the canon and the
    empirical AI-slop taxonomy, plus per-repo learned anchors accumulated from local accept/reject.
    A single repo-tuned anchor set cannot ship.
  - **The gated `af-build` axis matters only where `af-build` runs.** It stays in scope per R13,
    but it is now the secondary entry point by usage volume, and the human/repo-scale entry is
    primary. Design priority should follow that, reversing the emphasis the ideation doc opened with.

- **R15. Praxis reachability is a hard dependency; any particular space or snapshot is not.**
  (2026-07-29) `af-clean` may require that Praxis is reachable, but must assume **nothing** about
  what lives in it. It creates its own space per target repo. It may not depend on
  `prd-<project>`, `planning-validation`, `building-validation`, or any surface bindings existing.
  The safety-critical consequence: **an absent or empty surface set means "no oracle available",
  never "nothing is reachable."** Reachability is therefore **code-derived primary** (framework
  detection + entrypoint discovery + decorator/registry sweep) with **Praxis surfaces as optional
  enrichment when present**. Idea 15's job inventory and idea 6's findings ledger both live in
  `af-clean`'s own space and are self-bootstrapping — they do not require `af-intake-plan` to have
  ever run on the target repo.

### Risks opened by these decisions

- **RK1. Aggressive-then-validate has a blind spot in untested code.** R6's posture is only as
  safe as validation's coverage. Where tests don't reach, no downstream gate catches the
  breakage — which is precisely idea 1's finding that "tests still pass" can mean "nothing
  tested it." Mitigation to specify: keep the *coverage-independent* proofs (AST no-reference,
  reachability, string-corpus quarantine) inside `af-clean` where they cost nothing, and let
  only the test-dependent proofs move downstream to validation.
- **RK3. RESOLVED by R9's job-identity framing.** The apparent conflict with the repo's own
  `clean-code` brakes ("premature abstraction is worse than light duplication", "two
  byte-identical bodies representing different concerns stay separate", "three callers earns the
  helper") dissolves once the unit of identity is the *job* rather than the *text*. Those brakes
  govern **coincidental** duplication — different jobs that happen to look alike — and R9 governs
  **same-job re-implementation**. They are compatible and address disjoint cases. One
  clarification still owed to the anchors: "three callers earns the helper" must be read as
  applying to speculative extraction of a *new* abstraction, not to consolidating an existing
  job that is already implemented twice.
- **RK4. The one mechanical failure mode of maximal centralization** (not a taste disagreement):
  a shared helper that accretes a boolean flag or branch per caller is centralized in name only
  — every caller becomes coupled to every other caller's edge cases, and the parameter-accretion
  signature from idea 10 is exactly its fingerprint. Proposed guard, which honors R9 rather than
  softening it: **a centralization that requires adding a flag/branch per caller is a FAILED
  centralization** — that's the stop signal, not the cue to add flag #4. Centralize the parts
  that are genuinely identical; leave the divergent tail at the call sites.
- **RK2. Ideas 1 and 5 partly relocate.** Under R8, the coverage-witnessed verdict (idea 1) and
  the witness-tiered applier (idea 5) split across two components. Which half lives in
  `af-clean` and which in the validation step needs deciding explicitly, or the safety property
  falls between them.

### Carried forward

- **C1. Does idea 12 (keep-by-default comments) override the brief's "make comments terse"?**
  The evidence says be aggressive on redundant comments, conservative on ambiguous ones.
  Still needs explicit sign-off.
- **C2. `enforce` list membership** (idea 9) — the list *is* the product.
- **C3. Rejection scope and expiry** (idea 6) — too loose suppresses real slop forever, too
  tight evaporates on whitespace.
- **C4. Zero-history fallback** (idea 10) — the co-change and parameter-accretion sensors are
  blind on freshly generated code, `af-clean`'s main target.
- **C5. Squashed/message-less commit default** (idea 13) — permissive or protective.
- **C6. Anchor coverage.** The anchor set is three good + three slop exemplars, all Python, all
  diff-sized. Repo-scale work on `frontend-react/` TSX has nothing to grade against.
- **C7. Does the human entry point gate at all,** or is it always advisory-plus-apply with no
  pass/fail verdict?
- **C8. Exemption coverage today.** Which trees actually carry `meta.universal_exempt`? Under
  gating this list stops being cosmetic.

## Research Findings (2026-07-29)

### F-A. The gating flip is NOT a one-line change — two independent blockers

1. **Coverage-authoring gap.** Flipping `report_only=false` moves `minimalism-dry` out of
   `M_REPORT_ONLY_REQUIREMENTS` and into `required` (`agent_factory/hooks/_ticket_state.py:663`),
   so `coverage_gap` (`:617`) now demands a *pinned validation covering it*. But
   `af-build/SKILL.md` never mentions the universal lane, `contract_with_floor`, or
   `minimalism-dry` at all — so no worker would author one, and **every non-exempt ticket would
   fail `all_validations_passed` on a permanent coverage gap.** Fix: either SKILL.md gains
   synthesis instructions, or `start_ticket` auto-pins the universal graded validation alongside
   its requirement.
2. **No working exemption escape.** See F-C.

**Empirically measured blast radius (agent actually flipped the flag and ran the suite):**
baseline `1 failed, 524 passed` → flipped `6 failed, 519 passed`. **Exactly 5 new failures**,
each a 2–4 line edit, ~30–60 min total:
`tests/test_manual_verify_gating.py:45,70`, `tests/test_seeded_checks.py:108` (a deliberate
value-lock asserting `report_only is True`), `tests/test_unforgeable_caller_context.py:43,88`.
Four fail on the coverage gap, not on a judge call. **The prior plan's claim that "*every* test
that drives a ticket to `finished`" would break overstates it by a wide margin** — U4 did its job.

**The offline judge harness already supports gating mode.** `agent_factory/tests/conftest.py:31,36`
(`stub_pass_judge`/`stub_fail_judge`, parsing axis names out of the real prompt), and
`tests/test_universal_ci_harness.py:84,105` already drive a *gating* universal to pass and fail
end-to-end offline. The fix for the 5 tests is copy-paste from `:90-102`.

**`agent_factory/tests/` is NOT in CI.** Root `pyproject.toml:51` sets
`testpaths = ["knowledge", "frontend/tests"]`, so `pytest -q` collects 1737 tests and **zero**
from `agent_factory/tests/`. All 5 failures would have been invisible to CI.

### F-B. No remediation hook exists — but the scaffolding is good

- **Exists and reusable:** the graded path (`rubric.py:159 evaluate()`, `graded_verdict.py:125
  grade()`, `hooks/_graded_verify.py:86 verify_graded_check()`); **frozen rubrics** (pinned at
  pin-time, `_ticket_state.py:539-547`, so editing the TOML mid-ticket cannot move the target);
  verdict recording with **located defects that already carry `file`/`line`/`problem`/`remedy`**
  (`rubric.py:67-75`) — exactly the input `af-clean` needs; a real **per-(ticket,validation)
  iteration cap** (`DEFAULT_GRADED_ITER_CAP = 3`, `_graded_verify.py:36,121-127`) plus a
  **defect-count monotonicity** non-convergence detector (`:128-131`); the `block()` HITL tier;
  and a prose correction loop already triggering on any failing pinned validation
  (`af-build/SKILL.md:708-716`).
- **`grep -rn remediat` over `agent_factory/**/*.py` returns ZERO hits.** Nothing dispatches
  anything on failure.
- **The insertion seam is caller-side, not in library code.** `verify_graded_check` returns a
  `GradedResult`; SKILL.md routes `should_block → block()` (`:602-603`). The remediation branch
  slots in at `not r.verdict.passed and not r.should_block` → invoke `af-clean` with
  `r.verdict.defects` → re-call with the new diff so the hash changes. No library change needed.
- **Two latent bugs that R5 must fix:**
  - A **cache hit hard-codes `should_block=False`** (`_graded_verify.py:109-116`) and consumes no
    iteration. So an unchanged diff loops forever: `passed=False`, cap never trips, `block()`
    never reached. This is the deadlock, and it is R5's actual implementation site.
  - **`meta.graded_loop` is never reset anywhere.** `pin_requirements` clears `pinned_checks` but
    not `graded_loop`, so after a ticket goes incomplete and is re-picked, `iters` is still at 3
    and the first fresh grade blocks immediately.
- **There is no exit-code executor in this repo.** The `run` string is *data*; the agent executes
  it with its own Bash tool (`SKILL.md:585-592`) — no defined cwd, env, or timeout. Invoking
  `af-clean` through that lane would be an agent shelling a skill, a pattern used nowhere here.

### F-C. Exemptions are effectively dead code — and this is the real precondition

- `_universal_exempt` (`_ticket_state.py:1407-1413`) is **per-ticket only** — a truthy
  `meta.universal_exempt`, or a tag in `_UNIVERSAL_EXEMPT_TAGS = {"vendored","generated","config"}`
  (`:1394`). No per-file, per-path, per-hunk, or per-requirement exemption exists.
- **Nothing writes it.** `universal_exempt` appears in five files, none of them a skill.
  `af-intake-plan/SKILL.md:238` tells authors to use `meta.tags` for *identity*, with no hint
  those three literals are load-bearing. **Nothing is exempt today.**
- **No path-predicate layer exists** — no `fnmatch`, no glob list, no `.gitignore`/`.gitattributes`
  consultation, no `@generated` sniffing. `.gitattributes` is EOL-pinning only, with no
  `linguist-generated` markers to piggyback on.
- **Consequence: a path-predicate exemption layer must be BUILT before the flip.**
  `_universal_exempt` needs a diff-paths argument, and `contract_with_floor`/`start_ticket` need
  to supply the ticket's touched paths.
- **Exemption manifest candidates found:** `migrations/` (append-only history — "Never edit
  `0000_initial.sql`"); `praxis_kg_dump.sql` (**11.7 MB tracked `pg_dump` output**, 350× the next
  largest file); `uv.lock`, `package-lock.json`; `frontend-react/public/mock-*.json` (generated by
  `scripts/export-*.py`, ~200 literal "Auto-generated" strings);
  `knowledge/evals/cases/matt/skill_unification/sources/{gstack,compound-engineering}/` (85 files,
  7.7 MB — the repo's only genuinely vendored tree); `session-capture/wrapper/go.sum` and
  `testdata/`; six fixture directories (hand-authored but assertion-coupled).
- **`infra/cdk.out/` contains full duplicate copies of the repo tree** in four `asset.<sha>/`
  bundles. A repo-wide walk that doesn't exclude it will grade the same file five times and
  report phantom duplication.
- **`debug.log` is committed scratch** — 990 B of Chromium DNS warnings with Windows path
  separators, from another machine. Delete candidate, not an exemption.
- **`frontend/` is NOT dead — exempt it, don't purge it.** Three live dependencies: root
  `pyproject.toml:51` puts `frontend/tests` in `testpaths` (7 test files, half the declared pytest
  surface); `justfile` `observability-proxy` runs `frontend.phoenix_proxy.app:app`; and
  `frontend/mock_data.py` is the *canonical upstream* of frontend-react's mock mode
  (`scripts/export-mock-candidates.py:3` says so verbatim). Only `frontend/services/`, `models/`,
  `components/api_keys_panel.py`, `eval_mock_bridge.py` are genuinely superseded. A minimalism
  judge would correctly flag those and be **wrong** about the other three. Needs a human
  deprecation ticket, not a subjective gate.

### F-D. Toolchain reality, and the actual slop inventory

**Present and enforced:** `ruff` with **default rules only** (`pyproject.toml:53-57` has no
`[tool.ruff.lint]`) → `F401` unused-import, `F841` unused-local, `F821` undefined-name are ON;
`ARG`, `ERA`, `C901`, `SIM`, `RUF` are OFF. Baseline is **green**. `tsc` with `strict`,
`noUnusedLocals`, `noUnusedParameters` on all three TS projects. `mutmut` (report-only, 3 files) —
**the precedent for how this repo introduces a quality tool.**

**Absent:** Vulture, Knip, ts-prune, jscpd, radon, semgrep, mypy/pyright, pre-commit, any
formatter gate, any coverage threshold anywhere (**no `fail_under`, no `--cov`, no
`coverageThreshold`, no codecov — `pytest-cov` isn't even a dependency**). Nothing will fight a
deletion-heavy cleanup on coverage percentage, confirming R12.

**Installed but dead: ESLint.** Five devDependencies (`eslint`, `@eslint/js`,
`typescript-eslint`, `eslint-plugin-react-hooks`, `eslint-plugin-react-refresh`) and **no config
file anywhere** — `lint` is `tsc -b --noEmit`. The eslint deps are themselves slop, and a day-one
`af-clean` finding.

**No `just test` / `just lint` / `just typecheck` recipes exist.** The validation step must call
raw commands: `uvx ruff@0.15.20 check .`; `uv run --no-sync pytest -q`;
`cd agent_factory && ../.venv/bin/python -m pytest -q` (its own venv lacks `httpx` and dies at
collection); `cd frontend-react && npm run lint && npm run test`; `cd infra && npm run build`.

**Vulture would be unusable here without a whitelist — this is a hard precondition.** All FastAPI
routes are **closures nested inside `create_app`** (`knowledge/serve/app.py:484-3389`, no
`APIRouter` anywhere), so handlers have zero call sites and are reached only by decorator
side-effect. Same for ~30 `@mcp.tool()` functions in `knowledge/mcp/server.py`. Of ~13 root
categories on the Python side, **five are reachable only via side-effect or prose**: FastAPI
closures, MCP decorators, yoyo migrations, Claude Code hooks (7 scripts invoked from
settings.json), and SKILL.md-invoked tools (5 CLIs). An unwhitelisted run reports several hundred
false positives.

**Real slop found in `frontend-react/` (evidence for anchors):**
- `src/components/ui/GitHubRepoLink.tsx` — complete 27-line component, **zero references**. The
  anchor case for reachability purge; invisible to `noUnusedLocals` because it's an export.
- **9 dead exports** in `src/api/apiClient.ts` (`listEvalScopes:579`, `listCachedEvalCases:607`,
  `regenerateEvalCache:655`, `loadEvals:689`, `renameSnapshot:819`, `getProductivityStatus:508`,
  plus 3 types) — an entire eval-cache API surface built and never wired to UI.
- `postRegenerateEvals:529` — **test-only export**, all four references in
  `ingestClient.test.ts`. The exact R11 unreachable+covered cell, live in the repo.
- **Byte-identical duplicate including its docstring:** `clusterLabel` in `CandidateTable.tsx:16-20`
  and `CandidateCards.tsx:17-21`, plus `PAGE_SIZE_OPTIONS` and `DEFAULT_PAGE_SIZE` in both.
- **The repo's most-duplicated expression: `err instanceof Error ? err.message : String(err)`,
  38 occurrences across 18 files** (11 in `App.tsx` alone), with two drifted variants. The best TS
  DRY anchor available, and diff-sized.
- `ApiConflictError`/`ApiClientError` **declared twice** — as classes in `apiClient.ts:26,37` and
  as interfaces in `types/candidate.ts:54,59`, hand-synced.
- One-caller thin wrappers: `ContentSplit.tsx` (15 lines), `AppShell.tsx` (13), `LoadingSkeleton.tsx`
  (19), `LegendFlowStrip.tsx` (43), `LegendEdgeLine.tsx` (29).
- `useGraph.ts:10-15` — 4th positional param `state` never passed; returns `refreshGraph` that no
  caller consumes; `graph?.source` at `:53` guards a value the `useMemo` makes non-nullable.

**Patterns I assumed and that are simply ABSENT — do not write anchors for these:**
`console.*` → **zero occurrences** in `src/`. Redundant catch-and-rethrow → **none found**; all 40
`catch` blocks do real work, and `App.tsx:294-297` is a *deliberate* silent catch with a stated
reason. Dead feature flags → none. Unused props/params → none (tsc already enforces). Comments
restating code → rare; density is 4–15% and overwhelmingly *why*-comments, several carrying
requirement IDs (`(R16)`, `(R21)`) that are traceability, not slop.

**So the anchors need NEGATIVE examples, which the Python set lacks entirely.** The four patterns
most likely to trigger a false slop verdict in this codebase are all *correct* code: legitimate
`Array.isArray`/`typeof` guards narrowing genuinely-`unknown` wire payloads (~40 in
`contextClient.ts`, `jsonlParser.ts`, `graphModel.ts`), the deliberate silent catch, a reused thin
primitive (`EmptyState.tsx`, two callers), and `Record<union, string>` lookup tables reached by
computed key. With axis thresholds 0.8/0.8/0.75 and `confidence_floor = 8`, a miscalibrated anchor
blocks real tickets the moment the flip lands.

**`src/index.css` is 4,551 lines of string-keyed BEM classes, invisible to every JS/TS
reachability tool.** Deleting `GitHubRepoLink.tsx` orphans `.github-repo-link` and nothing reports
it. Treat CSS as unanalyzable or pair deletions with a class-name grep.

**One genuine advantage:** the React app has **no router and no dynamic imports** — `grep` for
`import(`, `React.lazy`, `react-router` returns zero. Single entry `src/main.tsx`. Static
reachability is unusually trustworthy on the TS side. The only complications are the
`components/viz/index.ts` star-export barrel and the `Record<>` lookup tables.

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Perfect-undo scratch-branch + bisect-revert substrate | **Unsafe in this repo** — several Claude sessions share this checkout, so branch switching and resets are off the table |
| 2 | Exercise orphan hypotheses via `af-wireframe` surface enumeration | Basis doesn't support it — wireframes are inert HTML and exercise no backend code |
| 3 | Scope by review budget instead of by path | Contradicts the stated invocation contract (whole repo, or a path named at invocation) |
| 4 | Deletion docket that never deletes | The skill must actually clean; report-only survives as a rollout phase inside idea 6, not as the product |
| 5 | Exportable org-level "taste pack" via `praxis_copy_snapshot_to_org` | Premature — one repo hasn't earned portable taste, and a wrong promotion deadlocks tickets elsewhere |
| 6 | Slop etiology via `git blame` + Praxis episode attribution | Expensive; better as a brainstorm variant of idea 7 than a first build |
| 7 | Mine git history for human-deleted agent code → auto-generate semgrep rules | Highest novelty, but needs a labeled corpus this repo may not have yet; folded as a future feeder to idea 7 |
| 8 | MEL-style expiring deferral ledger | Real distinction (schedule vs material), but subsumed by idea 6; extra vocabulary not yet earned |
| 9 | Forest-thinning stands + density-ranked treatment register | Mostly subsumed by idea 4's census and idea 6's ledger |
| 10 | Reachability graph as the *primary* artifact | Subsumed by idea 3 |
| 11 | Delete a symbol and its only-caller test together | Kept as a classification inside idea 1; standalone framing invites test deletion, the exact gaming risk §5 Risks warns about |
| 12 | "House dialect" — local Praxis precedent outranks the canon | Already what ideas 2 and 6 do together |
| 13 | Separate deletion evidence-tier ladder | Duplicates ideas 1 and 5 |
| 14 | ~24 further near-duplicates across the convergence clusters | Duplicate a stronger survivor |

All six axes carry survivors: A=1, B=1, C=1, D=2, E=2, F=7.

## Next Step

Not ready for planning. Route the chosen spine through `ce-brainstorm` first —
recommended spine: **ideas 8 + 9 + 2** (the taste architecture: one root question,
evidence-tiered authority, single rule source) settled *before* **idea 1** (what
"aggressive but safe" means mechanically). Idea 3 is the largest and deserves its own pass
once those are fixed.
