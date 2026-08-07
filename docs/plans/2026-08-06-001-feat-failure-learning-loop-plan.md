---
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
date: 2026-08-06
status: requirements-complete
---

# Failure Learning Loop - Plan

## Goal Capsule

**Objective:** Close the af-build learning loop: every failure — found by the merger's auto-regression or by Matt debugging in any repo — becomes a durable lesson in the shared cloud Praxis store plus (where provable) an enforcing check bound narrowly to the affected work, and the regressed tickets cannot finish their rerun until the new check passes. Lessons transfer across all projects; enforcement widens only on recurrence evidence; the lifecycle runs without a human review queue. The speculative validation-authoring skills are fully retired.

**Product authority:** this doc (from the 2026-08-06 ce-ideate → ce-brainstorm → af-plan session; ideation record at docs/ideation/2026-08-06-failure-learning-loop-ideation.html).

**Open blockers:** none — Open Decisions below are for af-intake-plan to force, not blockers on admission.

## Summary

One ingestion API with two callers: the merger (machine, strict) and humans via a new `/af-learn` skill (lenient, works from any repo, supports bulk authoring). Ingestion = lesson (always) + check (when provable or human-directed) + narrow binding + regress matching tickets + rerun gated on the new check. Lifecycle is fully automatic: widen on cross-scope recurrence with fresh proof, re-prove quiet checks instead of retiring them, auto-suspend false-positive checks. `af-intake-build-validation` and `af-intake-plan-validation` are deleted as skills; their reusable write machinery becomes the ingestion API's internals.

## Problem Frame

Seven successive hand-authored wireframe checks were gamed because they were speculative predictions measuring proxies; findings died in dead wires (17-round re-dispatch churn); post-run failure evidence evaporates in sentinel files; lessons documented in prose (R26) recurred (R37). The system gradient favors passing checks over correct work, and prediction has empirically lost to observation. Verified current-state facts: check resolution has only tag/wildcard/surface lanes (no ticket-identity binding); regressed workers read `meta.regression_detail` by convention, not injection; per-ticket checks run serially with no cost tiering; per-ticket regression outcomes DO persist on ticket facts; round-level verdicts live only in a transient sentinel file.

## Key Decisions

All session-settled (user-confirmed in dialogue, 2026-08-06):

- **KD1 (session-settled: hard constraint).** All knowledge stores live in the deployed cloud Praxis service — an org-level shared learnings space mounted read-only into every project. Laptop-only knowledge is forbidden; file/Obsidian layers are regenerable projections, never canonical. Rejected: git-markdown-canonical corpus.
- **KD2 (session-settled).** Learnings are post-hoc only: build → find → ingest → regress → rerun. Speculative pre-authoring of checks/lenses is retired. Rejected: predictive validation authoring (empirically failed).
- **KD3 (session-settled).** `af-intake-build-validation` and `af-intake-plan-validation` are **fully retired as skills**: skill directories deleted, dead code purged, reusable parts (section-locked snapshot writes, check schema validation, regress-matching-work plumbing) extracted into the ingestion API, which becomes the sole writer of validation content. Rejected: demote-but-keep as human-facing commands.
- **KD4 (session-settled).** Two-layer model: the **lesson is global** (shared org space, deduped against a failure-class taxonomy); the **check activation is narrow** — bound to the specific regressed ticket(s) + observed surface via a NEW mandatory ticket-identity lane. Widening (surface → tag → universal) is automatic on recurrence evidence and requires a fresh proof in each new scope before gating there. Rejected: broad-by-default bindings; failure-class-tag bindings.
- **KD5 (session-settled).** Fail-then-pass proof with **split leniency**: during af-build auto-insertion the machine channel is STRICT (no proof → no gating check; lesson still lands); every human-directed path is LENIENT (checks the user asks for are authored and inserted without oversight; proof is attempted and recorded — `proven`/`unproven` — but never blocks insertion). Rejected: uniform strict (kills retrospective complaints); uniform lenient (unproven machine checks).
- **KD6 (session-settled).** Auto-activation with **no review queue**; the whole lifecycle is automatic. Human review shapes nothing on the critical path. Rejected: docket/disposition review gate (earlier ideation shape).
- **KD7 (session-settled).** **Re-prove, don't retire:** gating checks never auto-archive on silence — a quiet check periodically re-proves against its retained bad artifact (still fails it → keep; artifact unavailable → demote to report_only). False positives get their own signal: a check that repeatedly regresses the same ticket with no relevant change auto-suspends and flags the operator; a manual kill switch exists. Rejected: archive-on-silence (retires working deterrents); no-retirement.
- **KD8 (session-settled).** Security stance: **accept + cheap anchors.** (1) `run` bodies hash-pinned at proof/insertion time — the executor refuses drifted bodies; (2) drafter evidence is data, never shell-interpolated into commands; (3) lesson text injected into rebuild contracts is provenance-marked untrusted data; (4) machine-drafted run bodies are untrusted output of an untrusted-input-fed drafter — they must validate against a declared command template/allowlist at schema-validation time (the E12 validator is the enforcement point), so evidence-steered drafting cannot produce an arbitrary command that hash-pinning then legitimizes. No sandbox. Documented accepted risk (solo operator, own infra). Rejected: sandboxed execution; human review of machine checks.
- **KD9 (session-settled).** Human channel is a dedicated **`/af-learn` skill callable from any Claude session in any repo**, wrapping the same ingestion API; supports single complaints and bulk authoring. Rejected: af-build-only intake; bare MCP tool.
- **KD10 (session-settled).** **The worker sees the lesson:** on re-claim, the regression reason + lesson are automatically injected into the rebuild contract (replacing read-by-convention). "Resolved with the new knowledge in place" means knowledge-in-context AND gate-enforced.

## Requirements

### Ingestion API (the single write path)

- **R1.** One ingestion API owns the full sequence: classify/dedup lesson against the taxonomy → write lesson to the shared org space → draft check → attempt fail-then-pass proof → bind → activate → regress matching tickets → record proof artifacts. It is the ONLY writer of validation content (post KD3).
- **R2.** The lesson always lands, in both channels, even when no check can be produced (knowledge is never lost to a drafting failure). A `check-undraftable` lesson is never enforcement-dead prose: it binds to its observed surface and is injected as provenance-marked context into any future ticket touching that surface (within the D7 cap), so the documented lesson-written-then-repeated failure mode (R26→R37) cannot recur by construction.
- **R3.** Dedup: an incoming failure matching an existing lesson's class does not duplicate the lesson; it attaches evidence to it and counts as recurrence (feeding R14 widening). An archived/suspended check whose class recurs is resurrected with its history — dedup must never conclude "already known, no action" while nothing is gating (the dedup-deadlock rule).
- **R4.** Every ingested check records provenance: channel (machine/human), source evidence pointers, proof status (`proven`/`unproven`), proof artifacts, hash-pinned run body, drafting transcript pointer.

### Machine channel (merger auto-regression)

- **R5.** When the merger regresses a ticket, ingestion fires in the same motion: regression without ingestion is not a legal state (when Praxis is reachable; see Edge E11). Acceptance: after any merger-driven regression, a lesson exists whose evidence links the regression_detail; observable via the org space.
- **R6.** Machine-strict proof, both sides executed: the drafted check activates as gating only after (a) FAILING against the retained bad artifact and (b) PASSING against a designated healthy reference (the pre-regression green state of the same surface, or a healthy sibling per D6) — the pass side is a test, never an expectation, so a non-discriminative fails-everything check cannot gate. A fail-only check inserts as report_only until its first real pass upgrades it. After a bounded redraft budget with no valid proof, no gating check is inserted; the lesson lands flagged `check-undraftable`. Acceptance: no machine-drafted gating check exists with `proven=false`.
- **R7.** The bad artifact is pinned at regression time — commit SHA + diff + failure evidence bundle retained in cloud storage — so proof and future re-proof (KD7) have something to run against. Proof execution materializes the pinned state in a **disposable isolated worktree**, never the live project checkout (concurrent sessions share it and its HEAD must not move); provisioning cost folds into D12's merge-time budget. Evidence bundles and screenshots are **secret-scanned/redacted before cloud write** (failure diffs and console output are where tokens surface); retention policy and content-sensitivity rules: Open Decision D3.
- **R8.** The automatic rerun of the regressed ticket must run with the new check pinned (via R11) and cannot FINISH until it passes. Acceptance: rerun's pinned set contains the new check_id; finish blocked while it fails.

### Human channel (/af-learn)

- **R9.** `/af-learn` accepts free-text complaints (plus optional file/URL/ticket pointers) from any repo, resolves the target project space, drafts lesson + check, attempts proof against the live state, inserts leniently per KD5, binds narrowly, and offers regression of matching tickets in the same motion. Single and bulk modes.
- **R10.** Lenient insertion is loud: unproven human checks are visibly flagged and enter the re-prove cycle (KD7); the first live catch of their failure class upgrades them to `proven` automatically.

### Binding, resolve, and scale

- **R11.** A new **ticket-identity lane** joins the resolve query: checks bound to a ticket id are mandatorily pinned at that ticket's (re-)claim, unskippable — exempt from diff-scoping (`scope_checks_to_changes`) and from all exemption machinery. This lane is the "forcibly apply" guarantee.
- **R12.** Default binding at ingestion is the narrowest scope covering the observed failure: the regressed ticket id(s) + the observed surface. Zero-match ingestion (no live ticket matches) binds surface-only and is flagged (no dangling ticket-id-only gates).
- **R13.** Ticket afterlife: when a bound ticket finishes or is deleted, the identity binding converts to its surface binding (checks never dangle on dead ids; orphan GC covered by re-prove cycle).
- **R14.** Widening is automatic and evidence-gated: recurrence of the same class in a new scope (surface/tag/project) triggers widening INTO that scope only after a fresh proof there (machine-strict rules apply to auto-widening). Universal promotion additionally requires recurrence in ≥2 distinct projects. Rejected inversion guard: a check whose proof is satisfiable by generic breakage (e.g. "tests fail") must not widen on unrelated failures — widening proof must fail for the class-specific reason (Open Decision D6 owns the mechanism).
- **R15.** Latency guarantee wins conflicts: per-ticket pinned-check budget and cost tiers (static/cheap vs browser/LLM/expensive) are introduced; expensive checks demote to report_only in scopes where the budget is exceeded, cheapest-first ordering applies. Acceptance: a ticket unrelated to any failure history pins the same count of checks as today (universals + its plan validations), regardless of corpus size.

### Rebuild context

- **R16.** On re-claim of a regressed ticket, the worker's contract automatically includes: the regression reason(s), the lesson(s), and the pinned new check(s) — provenance-marked as untrusted data (KD8). regression_detail becomes accumulative (a list keyed by finding) so concurrent findings never clobber (verified today: single dict, clobber risk). **Injection also fires at first claim, not only re-claim:** every fresh ticket's contract includes the top-ranked matching lessons from the shared org space, under the same D7 cap and provenance marking — this is context injection, explicitly not check authoring, and stays inside KD2.
- **R17.** Resolution stamps the specific finding whose check passed — a rerun passing check A must not stamp finding B resolved. **Resolution additionally requires the original finding's observed symptom re-evaluated against the rebuilt state** (the merger re-checks what regression_detail recorded, not solely the pinned check's exit code). Check-passed-but-symptom-present is recorded as a **check-defeat** — a first-class failure class feeding R3 — which demotes the defeated check to report_only and routes it through machine-strict redraft against the fresh bad artifact.

### Lifecycle (KD7 mechanics)

- **R18.** Quiet gating checks re-prove on a cadence against their retained artifact; re-prove failure (artifact gone/environment drift) demotes to report_only, never silent deletion. Lessons are never deleted by lifecycle — only enforcement state changes.
- **R19.** False-positive signal: N consecutive regressions of the same ticket by the same check with no relevant change auto-suspends the check (stops gating), flags the operator, and records the suspension as a lesson-annotation. Manual kill switch: one command disables any check immediately, recorded with reason.
- **R20.** Flap protection: ingestion always consults archived/suspended checks of the same class before drafting anew (see R3); resurrection carries prior proof history.
- **R20a — Check state machine (explicit):** a check is in exactly one enforcement state: **gating** (blocks FINISH), **report_only** (runs and records, non-blocking — entered via machine fail-only proof per R6, proof demotion per R18, budget overflow per R15, or check-defeat per R17; lenient human inserts land gating per DF4), **suspended** (stopped by the false-positive signal or the kill switch per R19; resurrectable), or **archived** (terminal enforcement state, entered only by explicit manual action or prolonged unrecoverable re-prove failure — never on silence, per KD7; lesson always retained; resurrectable via R20). Entry/exit conditions live with the owning requirement named here.
- **R20b — Taxonomy staged rollout:** taxonomy-dependent automation (R14 widening, R20 resurrection) activates only after an initial observe-only period in which class assignments are recorded and surfaced in af-retro but drive no automatic action — classification reliability is observed before the automation trusts it.

- **R1a — Plan-time authoring entry point:** the ingestion API exposes an explicitly-named plan-time authoring path (lenient human/intake channel per KD5, exempt from lesson/proof requirements) for the completeness guards and doc-sync checks that af-intake-plan and af-build's mid-build delegation write today — these have no failure to prove against and must remain authorable after retirement.
- **R1b — Authentication:** all ingestion-API operations (ingest, widen, suspend, kill-switch, regress) require an org-authenticated Praxis identity; solo-operator single-role is the accepted authz model, revisited if the org gains a second contributor.
- **R27 — Dedup false-negative sweep:** a periodic, off-critical-path near-duplicate sweep over lessons proposes class merges (under D11 governance), retroactively credits recurrence on merge, and surfaces class-assignment decisions in af-retro for spot-audit — owning the false-negative direction E7 does not (a recurrence misread as a new class silently zeroes widening, transfer, and resurrection).

### Retirement workstream (KD3)

- **R21.** Delete `agent_factory/skills/af-intake-build-validation/` and `agent_factory/skills/af-intake-plan-validation/`; purge dead code paths that exist only to serve them; migrate their reusable internals (section-locked snapshot writes, check schema validation, `resolve_preview`/coverage tooling, regress-matching-work calls) into the ingestion API. All existing references (af-build SKILL.md, af-intake-plan delegation, docs) updated **and repointed to R1a's plan-time entry point** (af-intake-plan SKILL.md and af-build SKILL.md are verified live callers). Acceptance: repo-wide grep finds no live invocation path to either skill; existing check-resolution tests still pass; af-intake-plan's completeness-guard delegation works through R1a.
- **R22.** `seeded_checks.toml` is grandfathered as the git-shipped universal library (it is code, not knowledge); its candidate/promote_universal lanes remain. New universals arrive only via R14 promotion, which writes to the cloud store (Open Decision D8 owns the dual-source seam).

### Observability

- **R23.** `af-retro <project>`: one command prints a run's full failure/ingestion/lifecycle record (regressions, lessons ingested, checks activated/suspended/widened, proof outcomes, per-run and cumulative check-undraftable rate, gating-vs-demoted ratio over time) from Praxis — no sentinel-file archaeology. This is the operator's detail view in a no-review-queue world.
- **R24 — Flags are push, not pull:** suspension, parking, undraftable, and check-defeat events emit to a designated operator channel — terminal notification at loop end plus a persistent unacknowledged-flags list that af-build surfaces at session start — and `af-retro --flags` aggregates all pending-attention states across every project in the org space, newest first. This is a notification channel, not a review queue (KD6 untouched); without it, a silent suspension de-closes S1 with nobody knowing.

## Edge States & Failure Classes

- **E1 — Rerun still fails the new check** after a bounded regress cycle cap → ticket parks `blocked` with the full history; loop continues with other tickets; operator flagged. (Cap value: D2.)
- **E2 — Regression targets a ticket under a live worker lease** → regression must either await lease expiry or revoke it; the completing worker's FINISH must fail against a regressed-under-it ticket (no lost regressions). (Semantics: D5.)
- **E3 — Concurrent findings on one ticket** → accumulate (R16); injection carries all open findings; resolution per-finding (R17).
- **E4 — Drafted check passes on the bad artifact** (vacuous) → machine: redraft up to budget then lesson-only (R6); human: insert flagged unproven (R10).
- **E5 — Bad artifact already destroyed** at proof time → machine: lesson-only + `check-undraftable`; prevention: R7 pins at regression time, before drafting.
- **E6 — Nondeterministic/flaky checks** → proof requires the configured repeat count (D4); graded/LLM-judged checks are eligible but their proof must be repeated; flaky proof = no proof.
- **E7 — Dedup false positive** (new failure wrongly merged into an old lesson) → recurrence evidence still attaches; if the old check then fails its class-specific widening proof in the new scope, the mismatch splits the lesson (flag + new class). (Mechanism: D6.)
- **E8 — Widening race: new scope's rerun fixes the artifact before the widening proof runs** → widening waits for the next recurrence; no proof, no gate (machine-strict).
- **E9 — /af-learn from a repo with no resolvable project space** → refuses with guidance; never writes cross-org; never regresses tickets without naming the project and getting confirmation in-session.
- **E10 — Bulk /af-learn inserts a check that immediately blocks many tickets** → R19's false-positive suspension applies; kill switch is the manual brake.
- **E11 — Praxis unreachable mid-merge** → the loop already cannot operate without Praxis (it is the source of all dynamic state); regression+ingestion halt loudly together. No offline queue (rejected: silent de-closure of the loop).
- **E12 — Malformed machine-drafted check definition** → schema validation at insertion (inherited from retired skills' validators, R21); invalid drafts are rejected at the API boundary, never written; loader-level fatality is thereby unreachable from the machine channel.
- **E13 — Lesson-injection bloat** (contracts inflate as lessons accumulate) → injection cap + relevance ranking per rebuild (D7).
- **E14 — Cloud store rollback** ("undo yesterday's bad ingestion wave") → requires a named rollback unit for validation content (D9).
- **E15 — Dedup false negative** (a recurrence misclassified as a new class) → owned by R27's near-duplicate sweep: merge proposed, recurrence retroactively credited, assignment visible in af-retro.

## Implied Features (accepted from ideation)

- Failure-class taxonomy as first-class data (nodes with derived_from edges to checks/tickets) — the transfer mechanism between projects.
- Proof-artifact store (pinned SHAs, diffs, evidence bundles, screenshots) in cloud storage.
- Kill-switch + suspension CLI (R19).
- `af-retro` run report (R23).
- File/Obsidian projection generator of the shared space — derived, regenerable, explicitly out of the critical path (deferred; see Scope).

## Scope Boundaries

**In:** everything under Requirements; the two-skill retirement workstream; the new resolve lane; proof engine; lifecycle; `/af-learn`; `af-retro`.

**Out (deferred):** the Obsidian/markdown projection layer; deeper check certification (mutation corpus, red-team adversaries); the sensor-capability instrument-class floor; heartbeat watchdog for silent driver deaths; any docket/review UI (rejected permanently per KD6, not deferred).

**Accepted trade-off (by design, per KD2):** prevention-before-first-failure is impossible — the system only learns from failures it has paid for. Cross-project sharing amortizes drafting and lesson-authoring, but each project still pays a failure class once before its gate widens there (once per class × project until universal promotion).

## Success Criteria

- **S1 (non-repeat guarantee):** a failure Matt calls out via `/af-learn` with a proven check cannot recur silently: matching tickets regress, their reruns pin the check mandatorily (R11), and FINISH is impossible while it fails.
- **S2 (flat unrelated cost):** pinned-check count and validation wall-time on tickets with no relevant failure history remain at today's baseline as the corpus grows (R15's budget enforces).
- **S3 (transfer):** a failure class first paid for in project A gates in project B after B's first recurrence, with no human authoring in between.
- **S4 (closed machine loop):** merger regression → lesson + proven check → rerun-passes requires zero human actions.
- **S5 (retirement):** both speculative-intake skills deleted with no orphaned callers; ingestion API is the sole validation writer.

## Open Decisions (for af-intake-plan to force)

- **D1 — Redraft budget** (machine drafting attempts before lesson-only). Checked: no convention; suggest 2-3.
- **D2 — Regress-cycle cap per (ticket, check)** before parking blocked (E1). Checked: graded_loop caps exist per-validation (3 iters) but reset on re-claim; no per-ticket-cycle cap exists.
- **D3 — Proof-artifact retention and content policy** (duration, size budget, storage location in cloud; archived checks' artifacts included; **re-runnability** — environment capture sufficient to re-execute the proof, not just store the diff, since artifact rot otherwise converges the corpus to report_only in slow motion; **content sensitivity** — secret-scan/redaction rules per R7, and whether cross-project readability of failure evidence via the shared org space is an accepted exposure). Checked: today's screenshots/measurements land in run dirs with no policy.
- **D4 — Proof repeat count / determinism rule** for flaky and LLM-judged checks (E6).
- **D5 — Lease semantics on regression** (await vs revoke; FINISH-fails-if-regressed-under-you mechanics) (E2). Checked: `praxis_regress_requirements` has no lease awareness today.
- **D6 — Class-specific proof mechanism** preventing generic checks from widening on unrelated breakage (R14/E7): options — proof must reference class-tagged assertions; or widening proof must fail on the new scope's artifact AND pass on a healthy sibling.
- **D7 — Injection cap and ranking** for lessons in rebuild contracts (E13).
- **D8 — Dual-source seam for universals**: how R14's cloud-promoted universals and seeded_checks.toml's git universals coexist without a second writer violating R1 (options: cloud is authoritative and toml is imported at load; or toml remains for hand-shipped code checks with distinct id-space).
- **D9 — Rollback unit for cloud validation content** (per-fact history vs snapshot point-in-time writes) (E14).
- **D10 — Re-prove cadence** for quiet checks (KD7) and who pays for it (off-critical-path scheduling).
- **D11 — Taxonomy governance**: how classes merge/split/rename over years and how keyed records migrate. Checked: no schema-migration convention exists for meta-keyed facts.
- **D12 — Merge-time ingestion budget**: bound on drafting+proof wall-time inside the merge path; overflow behavior (defer proof to background and hold rerun until it lands, vs block merge).
- **D13 — Praxis capacity ownership**: expected query/write volume of the new lanes and org-space lookups at fleet scale; when the shared service needs provisioning attention.

## Defaults Taken (flagged for override)

- **DF1:** seeded_checks.toml grandfathered in git (R22) — rationale: it is executable code shipped with the factory; consistent with KD1's code-vs-knowledge split.
- **DF2:** Latency guarantee outranks widening ambition (R15) — rationale: Matt's stated top concern; universal promotion is rare by construction (≥2 projects).
- **DF3:** Praxis-outage behavior = halt loudly, no offline queue (E11) — rationale: the loop is already fully Praxis-dependent; a queue adds a second source of truth for no availability gain.
- **DF4:** Unproven human checks gate immediately (lenient per KD5) rather than report_only-until-proven — rationale: Matt's explicit "they all get authored and inserted even if I don't oversee"; the re-prove cycle and false-positive suspension are the safety net.
- **DF5:** Zero-match ingestion binds surface-only (R12) — rationale: keeps the guarantee lane clean of dangling ids.

## Rigor Mode

Rigorous + Collaborate. Passes run: ce-ideate (5-frame fleet + fresh-context basis verification, 2026-08-06); ce-brainstorm collaborative dialogue (5 blocking scope decisions); grounding scout + 7-claim fresh-context verifier (6 confirmed, 1 corrected); adversarial falsification pass (26 challenges, all recorded above as requirements, edges, or open decisions); five-lens gap sweep (failure-modes, security, data-lifecycle, rollback, who-pays — all fired; findings integrated); ce-doc-review cold-eyes panel: RAN — 6 personas (coherence, feasibility, product-lens, security-lens, scope-guardian, adversarial), 23 raw findings → 1 auto-applied correction + 13 walk-through findings (all Applied by the human: R1a/R1b/R2/R6/R7/R16/R17/R20a/R20b/R23/R24/R27/E15/KD8-anchor-4/D3 extensions) + 5 FYI observations (cost-tiering may be ahead of need; taxonomy could start flat; single trust domain assumption; human-channel coverage bounded by invocation habit; KD2 evidence generalizes from one domain). Cross-model pass skipped (no different-family CLI installed). Decision mode: Collaborate — 8 forks settled by the human in-session (KD4-KD9, lifecycle, security, proof-leniency) plus 13 review findings dispositioned individually; remainder recorded as Open Decisions, never resolved away.
