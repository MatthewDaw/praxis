# Feature Specification: Grounding-aware rubric judge

**Feature Branch**: `004-grounding-aware-rubric-judge`

**Created**: 2026-06-23

**Status**: Draft

**Input**: User description: "docs\proposals\2026-06-23-grounding-aware-rubric-judge.md"

## Clarifications

### Session 2026-06-23

- Q: How does the judge decide which grading treatment each criterion gets — does it need a classification mechanism? → A: No separate mechanism. Each criterion's own text already states its policy; the judge grades per the criterion text with the reference available. The fix is supplying the reference, not adding a blanket "any claim not in the reference is a failure" rule (which would wrongly penalize conflict-handling criteria). The reference must be both supplied AND clearly labeled to the judge as *what it is* — the seeded background/source material the scenario was built from — so the judge knows its role. Crucially, the label MUST stay neutral: it must NOT assert that the reference is authoritative truth the answer must comply with, or the safety/override case backslides (a judge told "this is the truth" could penalize an answer for correctly overriding a seeded rule). Each criterion's text decides the relationship to the reference (align with it vs. correctly override it).
- Q: Does this feature remove the brittle keyword checks? → A: No. Keep the deterministic checks and *widen* them (accept synonyms/paraphrases of the same concept) so they stop failing correct answers on wording. Retiring a check is only considered later, as a follow-up, IF widening still proves too brittle AND the reference-aware grounding criteria prove strong enough to replace it.
- Q (analyze remediation): Should the grounding success criteria be reproducible, and do we need an answer cassette? → A: Yes — add a **judge-verdict cassette** (extend the existing merge/conflict mechanism) so the rubric judge replays deterministically offline. No separate answer cassette: deterministic validation feeds **authored fixed control answers** (grounded + fabricated/rule-obeying) as fixtures. Provisional score thresholds adopted: ungrounded ≤ 0.3, grounded ≥ 0.7, separation ≥ 0.4. Terminology normalized to "seeded reference" (not "ground truth").

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Grounding criteria catch fabrication (Priority: P1)

The rubric judge grades "grounded" and "honest" criteria by checking the answer's claims against the case's **real seeded background** (the seeded reference), not against the answer alone. A fluent, confident answer that invents employers, projects, tools, or metrics not present in the background now scores **low** on those criteria; an answer whose claims trace to the background scores high.

**Why this priority**: This is the core defect. Today these criteria carry the most rubric weight (e.g. 3.5 of 6.0 for the application cases) yet are graded with no reference, so the judge scores plausibility, not support — a documented hallucination scored ~0.85 on "grounded." Fixing this is what makes the whole eval suite trustworthy; every other story builds on it.

**Independent Test**: Take a case with a known fabrication (an answer citing tools absent from the seeded background) and a matched grounded answer. Confirm the fabricated answer scores low on grounded/honest and the grounded answer scores high — a clear separation that did not exist before.

**Acceptance Scenarios**:

1. **Given** a case with a seeded reference and an answer that fabricates a claim absent from that reference, **When** the judge grades it, **Then** the grounded/honest criteria score low (the fabrication is penalized).
2. **Given** a case with seeded reference and an answer whose claims are all supported by it, **When** the judge grades it, **Then** the grounded/honest criteria score high.
3. **Given** a true claim that comes from the real background but was not among the facts the reader retrieved for this question, **When** the judge grades it, **Then** that claim is treated as grounded (not false-flagged as fabricated).
4. **Given** a case that has no seeded reference, **When** the judge grades it, **Then** scoring is identical to today's behavior (no reference is used, no regression).

---

### User Story 2 - Safety "ignore the stored rule" case becomes gradeable (Priority: P2)

For the safety case where a direct user instruction must override a stored rule, the judge is given the stored rule as part of the reference so it can actually verify the agent correctly ignored it. Previously the judge never saw the rule, so this safety assertion — which rests entirely on the rubric (no deterministic checks) — could not be evaluated at all.

**Why this priority**: It is arguably the most important assertion to fix because it is a safety behavior with zero deterministic backstop, but it depends on the same reference-passing mechanism as Story 1, so it follows P1.

**Independent Test**: Run the safety override case with an answer that correctly follows the user's direct instruction (ignoring the stored rule) and one that wrongly obeys the stored rule. Confirm the judge now produces a meaningful, differentiated score for the "ignores stored rule" criterion.

**Acceptance Scenarios**:

1. **Given** the safety override case, **When** the judge grades an answer that correctly lets the direct request take precedence over the stored rule, **Then** the "ignores stored rule" criterion scores high.
2. **Given** the same case, **When** the judge grades an answer that wrongly obeys the stored rule, **Then** that criterion scores low.
3. **Given** a conflict-handling criterion (flag a contradiction, ignore a low-confidence rumor, follow active over retired knowledge), **When** the judge grades with the conflicting facts visible in the reference, **Then** it follows the criterion's stated policy and an answer that correctly *ignores* a deprecated/unverified/overridden fact is not penalized for omission.

---

### User Story 3 - Widen brittle deterministic checks (keep them) (Priority: P3)

The brittle literal-keyword checks (e.g. requiring the exact token "rag") are **widened** to accept synonyms/paraphrases that express the same concept, so a correct answer is no longer failed for wording. The deterministic checks are **kept** as a reproducible backstop alongside the reference-aware judge — they are not removed. Retiring a check is only revisited later, as a conditional follow-up, if widening still proves too brittle and the grounding criteria prove strong enough to replace it.

**Why this priority**: This is the payoff and cleanup. Keeping deterministic checks preserves a reproducible safety net (more stable run-to-run than the LLM judge) while ending the phrasing/model lottery. Lowest urgency; benefits from Stories 1–2 being in place.

**Independent Test**: Take an answer that expresses a required concept with a synonym and confirm the widened check now passes it; take a genuinely wrong/missing-concept answer and confirm the widened check still fails it. Confirm no deterministic checks were removed.

**Acceptance Scenarios**:

1. **Given** an answer that expresses a correct concept with a synonym (not the literal keyword), **When** the widened deterministic check grades it, **Then** the answer passes (no longer failed for phrasing).
2. **Given** an answer that genuinely lacks or contradicts the required concept, **When** the widened deterministic check grades it, **Then** the answer still fails (discrimination is retained).
3. **Given** this feature is complete, **When** the suite is inspected, **Then** the deterministic checks remain in place (none removed); retirement is deferred to a conditional follow-up.

---

### Edge Cases

- **No seeded reference**: the reference is omitted entirely; behavior is unchanged from today (explicitly a no-regression requirement).
- **Large reference**: a case's full raw background can be large, increasing judge input. The full raw seed is still used; only obvious boilerplate may be trimmed — the system must never fall back to the reader-retrieved subset as the reference (that reintroduces false-flagging of true claims).
- **Retrieved subset vs. real background**: the reference is the full seeded background, never the query-focused subset the reader surfaced, so true claims outside the retrieved subset are not mistaken for fabrications.
- **Distilled vs. raw**: the reference uses the raw seeded source, not distilled/rephrased facts, because distillation can drop or alter wording.
- **Commission vs. omission**: the grounding frame penalizes claiming something unsupported (commission), not failing to mention a fact (omission) — so correctly ignoring a poison/retired/overridden fact is not penalized.
- **Judge nondeterminism**: the judge is still a live model, so scores vary run-to-run; reference-aware grading is more stable than token-matching but not deterministic.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: When a criterion's own text calls for factual grounding (e.g. grounded, honest, factual-grounding), the judge MUST be able to grade it against the case's seeded reference — scoring whether each claim is *supported* by it, not merely whether the answer is plausible. This is achieved by supplying the reference; the criterion text already states what to verify.
- **FR-002**: The reference handed to the judge MUST be the full seeded reference (the raw seeded source documents, both passively-ingested and directly-seeded), NOT the reader-retrieved subset and NOT the distilled graph facts. It MUST be labeled to the judge neutrally — as the seeded background/source material the scenario was built from — so the judge knows what it is. The label MUST NOT frame the reference as authoritative truth the answer must conform to (that would mis-penalize the safety/override case, where the answer is expected to correctly override a seeded rule); whether the answer should align with or override the reference is governed by each criterion's own text.
- **FR-003**: A claim in the answer that is not supported by the reference MUST be treated as a grounding/honesty failure (a fluent-but-fabricated claim scores low).
- **FR-004**: A claim that is supported by the seeded reference but absent from what the reader retrieved for the question MUST NOT be flagged as fabricated.
- **FR-005**: When a case has no seeded reference, the reference MUST be omitted and grading MUST match the prior behavior (no regression).
- **FR-006**: For the safety case where a direct user instruction overrides a stored rule, the stored rule MUST be included in the reference so the judge can verify the agent correctly ignored it.
- **FR-007**: The judge MUST grade each criterion according to that criterion's own text, with the reference available as context. It MUST NOT impose a blanket rule that any claim absent from the reference is automatically a failure, because that would mis-penalize conflict-handling criteria (flag a contradiction, ignore a low-confidence rumor, follow active over retired knowledge, let a direct request take precedence) whose text already specifies the expected handling. No separate per-criterion classification is required — the criterion text is the source of the grading policy.
- **FR-008**: The grounding frame MUST penalize unsupported claims (commission) only; correctly omitting or ignoring a deprecated, unverified, or overridden fact MUST NOT be penalized.
- **FR-009**: The grounding-aware behavior MUST apply consistently across the judges in use (the primary judge and the parity judge), so results do not depend on which judge ran.
- **FR-010**: Existing deterministic checks that are brittle to phrasing MUST be *widened* (e.g. accept synonyms/paraphrases that express the same concept) so a correct answer is not failed for wording. The deterministic checks MUST remain in place as a reproducible backstop — they are not removed by this feature.
- **FR-010a**: A widened deterministic check MUST still fail an answer that genuinely lacks or contradicts the required concept (widening reduces phrasing-sensitivity without losing discrimination).
- **FR-010b**: Retiring or de-weighting a deterministic check is OUT of scope for this feature and is a conditional follow-up — considered only IF widening still proves too brittle AND the reference-aware grounding criteria prove strong enough to replace the check.
- **FR-011**: The change MUST require no edits to the rubric criteria themselves and MUST NOT change agent/runner behavior — it changes only what context the judge grades against.
- **FR-012**: The rubric judge's per-criterion scores MUST be recordable to, and replayable from, a committed judge-verdict cassette (extending the existing merge/conflict/aspect verdict-cassette mechanism), so grounding validation runs deterministically and offline without a live judge.
- **FR-012a**: Grounding/honesty (and the safety-override) success criteria MUST be verifiable deterministically using authored fixed control answers (a grounded answer and a deliberately-fabricated/rule-obeying answer) as committed fixtures graded via the cassette — no live agent/runner output is required for that gate.

### Key Entities *(include if feature involves data)*

- **Rubric case**: An eval case carrying weighted rubric criteria and (for most affected cases) seeded reference. 34 rubric cases total; 16 depend on the seeded reference.
- **Rubric criterion**: A named, weighted scoring dimension whose own text states what the judge should check. Some criteria call for factual grounding (e.g. grounded, honest), some for conflict handling (e.g. flag a contradiction, ignore a stored rule), and some are output-intrinsic (style/structure/correctness). The judge follows the criterion text in all cases; the reference is supplied so grounding/conflict criteria can actually be verified, while output-intrinsic criteria simply don't need it.
- **Seeded reference (the scenario's background source)**: The full raw seeded source material for a case. For grounding criteria it defines what counts as supported (claims outside it are fabrications); for override/conflict criteria it is context the answer may correctly contradict. Distinct from the reader-retrieved subset and from distilled graph facts. It is supplied to the judge with a neutral label (what it is), never as authoritative truth the answer must obey.
- **Judge verdict**: A per-criterion score (0–1) produced by the judge for a graded answer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A deliberately ungrounded control answer scores **≤ 0.3** on grounded/honest while a matched grounded answer scores **≥ 0.7** (separation **≥ 0.4**), where today the same fabrication scored high (~0.85). (Thresholds are provisional, set here so the gate is testable; tune during validation.)
- **SC-002**: The documented empty-context hallucination (citing tools that are not in the real background) scores **≤ 0.3** on the grounded criterion.
- **SC-003**: The safety "ignore the stored rule" criterion scores **≥ 0.7** for an answer that correctly takes the direct request's precedence and **≤ 0.3** for one that wrongly obeys the stored rule — where before it was unverifiable. (Provisional thresholds, as in SC-001.)
- **SC-004**: 100% of cases without seeded reference produce the same scores as before the change (no regression).
- **SC-005**: True claims drawn from the real background but outside the reader-retrieved subset are not flagged as fabricated in any affected case.
- **SC-006**: All 16 affected cases re-run and behave correctly; the 18 reference-free rubric cases are unchanged.
- **SC-007**: At least one previously phrasing-brittle deterministic check is widened so an answer expressing the concept with a synonym passes, while a genuinely ungrounded/incorrect answer still fails — with no deterministic checks removed in this feature.

## Assumptions

- **Independent of the grounding/reader work**: because the reference is the seed (always present on the case), this change does not depend on the active-fact retrievability / reader changes; only the separate, optional "context-faithfulness" variant (judging against the retrieved subset) would. That variant is out of scope here.
- **Judge model is configurable and chosen empirically**: a stronger judge model is defensible for long-reference claim verification, but the exact model is a tuning decision validated by whether it catches the seeded-hallucination control — not fixed by this spec. The existing judge-model configuration is reused.
- **Reference cost is accepted**: larger judge inputs (and modestly higher per-case cost) are the accepted price of correctness; trimming, if needed, removes only obvious boilerplate and never substitutes the retrieved subset.
- **Determinism via judge cassette**: the rubric judge's verdicts are recorded to a committed cassette and replayed offline, so grounding validation is reproducible without a live judge (Constitution Principle II). Live judge runs are needed only to (re)record the cassette and for broad empirical tuning across all affected cases.
- **Eval-infra only**: this affects the eval harness's judging, not any product/runtime behavior.

## Out of Scope

- Changing the rubric criteria themselves.
- Making the agent/runner deterministic or changing what the agent sees.
- The separate "context-faithfulness" eval that grades against the reader-retrieved subset.
- Adding new deterministic guards (e.g. a regex check for the safety case) — worth doing independently but not part of this feature.
- Retiring or removing existing deterministic checks — this feature *widens* them instead; retirement is a conditional follow-up (FR-010b).
