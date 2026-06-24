# Ideation — Contradiction-detection algorithm (precision + efficiency)

_Date: 2026-06-23 · Mode: repo-grounded · Subject: `knowledge/knowledge_graph/write_policy/write_step_variants/` contradiction path_

## Problem (grounded)

On each write, **one cosine-recall pass** returns candidates above a high-recall floor;
`ConflictFlagger` loops every candidate and calls `ConflictJudge.contradicts(new, existing)` —
a **single LLM call per pair** with a **bare-boolean prompt** ("Does NEW contradict EXISTING?"),
no rubric, no reasoning, no confidence. Flags are emitted **pairwise**, no clustering.
`AspectTagger` (gated) adds a second recall key from a **hardcoded coding-style vocabulary**.

Observed on the `volta_video` graph — 12 flagged pairs:
- **3 real**, all the *same* conflict (voltaic-pile year 1799 vs 1800 vs 1801), redundantly emitted as 3 pairs.
- **9 false positives** — topically-related-but-non-opposing facts (two editing-style notes; bg-color +
  compositing; "Galvani discovered animal electricity" + "Volta disproved animal electricity"; sequential
  career dates 1774 Como / 1779 Pavia; atomic note vs whole wiki paragraph).

**Root cause:** the system measures *topical similarity* (cosine) and asks an unructured LLM a vague
question — it conflates "about the same thing" with "asserts the opposite." Contradiction is actually
**same subject + same attribute, incompatible value.**

## Topic axes

1. Recall / candidate generation · 2. Judge decision stage · 3. Cheap pre-filter / efficiency ·
4. Fact representation & granularity · 5. Post-hoc clustering / dedup of flags

---

## Survivors (ranked)

### S1 — Contradiction = functional-dependency violation over (subject, attribute, value) `[axis 4, keystone]`
Extract each fact into atomic (subject, attribute, value) triples at write time; a contradiction is a
same-(subject, attribute) key with an incompatible value. The 3 real conflicts are pure FD violations on
`(voltaic_pile, invention_year)`; **all 9 FPs fail the key test** (different subject or attribute) and are
dropped with **zero LLM**.
- **Basis:** convergent across all 4 frames; external — relational FD theory (Codd), SHACL functional
  properties (Oxford Semantic 2024), triple-level fact-checking 60.4% vs 58.1% sentence-level (arxiv 2312.11785).
- **Why it matters:** single highest-leverage reframe — fixes precision *and* cost in one move.

### S2 — Cascade: cheap structural/NLI gate first, LLM only for the gray zone `[axis 3]`
Stage the decision: (a) structural FD check decides the obvious cases; (b) a small local NLI cross-encoder
(`nli-deberta-v3-small`, ~<10ms/pair, no API cost) scores entail/neutral/contradict on remaining same-subject
pairs; (c) only the borderline band (e.g. contradiction score 0.3–0.8) escalates to one LLM call.
- **Basis:** external — FrugalGPT/MixLLM cascades (97% quality at 24% cost), RAG contradiction study thresholds.
  **Caveat from research:** NLI models over-trigger on topical proximity too — only reliable *behind* a
  subject-match gate, not as a standalone drop-in.
- **Why it matters:** turns O(candidates) LLM calls/write into O(borderline) — rare.

### S3 — "Blocking" recall on (entity, attribute), not cosine-only `[axis 1]`
Replace/augment the single cosine pass with entity-resolution-style **blocking**: recall existing facts that
share a normalized subject + attribute slot. Cosine becomes a tiebreaker within a block, not the gate. This
removes the framing bias that feeds the judge topical neighbors.
- **Basis:** external — record-linkage blocking (Fellegi-Sunter); direct — 9 FPs enter the funnel purely via
  topical cosine proximity.
- **Why it matters:** improves both precision (fewer decoys) and cost (smaller candidate set) before any judge runs.

### S4 — Rubric + reasoning + confidence + typed verdict judge `[axis 2]`
When the LLM *does* run, replace the bare boolean with a structured verdict: `{type, shared_subject,
shared_attribute, value_a, value_b, confidence, reason_code}` where type ∈ {temporal, numeric, negation,
mutual-exclusion, complementary, attribution-difference, none}. Force it to name the conflicting attribute and
values; instruct that complementary elaborations and "X claimed P / Y refuted P" attribution differences are
**not** contradictions; threshold on confidence.
- **Basis:** direct — current prompt has no rubric/reasoning/confidence; external — RAG study: Claude-3 + CoT
  reaches precision 0.951; argument-mining stance+attribution separates the Galvani/Volta FP.
- **Why it matters:** cheapest fix if we keep the LLM in the loop; kills the subtle FPs the structural gate can't.

### S5 — Cluster flags by claim signature `[axis 5]`
Group emitted flags into connected components / by (subject, attribute) signature and surface one contradiction
**set** per slot. The 1799/1800/1801 trio becomes one "year-of-invention has 3 competing values" item, resolved once.
- **Basis:** direct — flags are pairwise, the real conflict was tripled; external — GROUP BY / connected components.
- **Why it matters:** big de-noise + UX win that pays off **independently** of detection changes — cheapest standalone win.

### S6 (supporting) — Atomic decomposition to fix granularity mismatch `[axis 4]`
Decompose multi-claim facts (wiki paragraphs) into atomic claims before comparison; compare atom-to-atom with
probability aggregation, not whole-text or veto. Enables S1 to work on paragraphs and stops the
atomic-note-vs-essay FP.
- **Basis:** external — Atomic-SNLI (+8–10 F1; veto = low precision, soft-aggregate = balanced).
- **Why it matters:** prerequisite for S1/S2 to behave on the long proposed wiki facts in this very graph.

---

## Recommended synthesis — "cascade over atomic SAV claims"

S1+S2+S3+S5+S6 compose into one coherent design:
1. **Write time:** decompose fact → atomic (subject, attribute, value) claims; index by (subject, attribute).
2. **Detection = blocking + FD check (zero LLM):** same-(subject, attribute) claims with differing values, on a
   functional attribute → deterministic conflict. This *is* recall and detection fused.
3. **Escalate only ambiguous value comparisons** (free-text values, fuzzy attributes) to one rubric-carrying LLM
   call, optionally behind an NLI gate.
4. **Group flags by claim signature** into clusters for resolution.

Net: precision rises (FPs fail the key test), and LLM calls drop from O(candidates)/write to a rare gray-zone tail.
S4 is the fallback if full SAV extraction is too ambitious near-term — it salvages precision while keeping the
current architecture.

## Rejected / demoted

- **Pure NLI drop-in (no subject gate):** research shows NLI over-triggers on topical proximity; FPs persist. → folded into S2 behind a gate.
- **Lexical negation/antonym fast-path:** brittle, ~zero coverage on the actual (date) conflicts. → minor, not load-bearing.
- **Deferred nightly batch sweep instead of write-time:** real option but changes write semantics; clustering (S5) gets most of the benefit without that. → defer.
- **Batch one-vs-many judge call:** good efficiency, but subsumed by the cascade (most pairs never reach the LLM). → fold into S4 as the call shape.
- **Domain-general learned aspect vocab:** matters, but AspectTagger is gated/experimental; S1's (subject, attribute) key supersedes the aspect channel's purpose. → demote.

## Next step

Route the recommended synthesis (or a chosen subset — S5 alone is the cheapest standalone win) into
`ce-brainstorm` to define scope precisely, then `ce-plan`.
