---
title: Structural contradiction detection (precision-first)
status: ready-for-planning
date: 2026-06-23
type: feat
scope: deep-feature
origin: docs/ideation/2026-06-23-contradiction-detection.md
---

# Structural contradiction detection (precision-first)

## Problem frame

The knowledge graph's contradiction detector over-flags. On each write it does one
cosine-similarity recall pass and asks an LLM a bare-boolean question
("Does NEW contradict EXISTING?") for every similar candidate, then emits a flag
per pair. This conflates **topical similarity** with **contradiction**.

Observed on the live `volta_video` graph (12 flagged pairs):
- **9 false positives** — topically-related-but-non-opposing facts (two editing-style
  notes; background-color note + compositing note; "Galvani discovered animal
  electricity" + "Volta disproved animal electricity"; sequential career dates; an
  atomic note compared against a whole wiki paragraph).
- **3 real**, but all the *same* conflict (voltaic-pile invention year 1799 / 1800 / 1801),
  redundantly emitted as 3 separate pairs.

A contradiction is not "two facts about the same topic" — it is **the same subject
asserting incompatible values for the same single-valued property**. A session spike
(`scratchpad/spike_sav_contradiction.py`, real `gpt-4o-mini` extraction over the 18
facts behind the 12 flags) validated the reframe: extracting (subject, attribute, value)
claims and flagging only same-subject + same-functional-attribute + incompatible-value
took false positives **9 → 0** and collapsed the real conflict into **one** cluster
capturing all five year-asserting facts.

## Actors

- **A1 — Ingestion pipeline / author**: writes facts (code-derived or explicit). Triggers detection at write time.
- **A2 — Human reviewer**: vets flagged contradictions in the contradictions tab and resolves them. Resolution semantics are unchanged by this work.

## Goal

Make contradiction detection **precise** (few/no false positives) and **efficient**
(few LLM calls), by grounding it in structured claims rather than text-pair similarity —
without changing the existing resolution workflow.

---

## Requirements

- **R1 — Claim-based representation.** Each fact is decomposed at write time into one or
  more atomic claims of the form (subject, attribute, value), plus a flag marking whether
  the attribute is **functional** (single-valued for that subject — e.g. an event's year,
  a person's birth year) or multi-valued (e.g. a person's discoveries, a list of metals).
- **R2 — Structural contradiction definition.** A contradiction exists only when two facts
  hold claims with the **same subject**, the **same attribute**, that attribute is
  **functional**, and the **values are incompatible**. Multi-valued attributes never
  produce a contradiction on value difference.
- **R3 — Precision-first uncertainty handling.** When extraction is low-confidence, an
  attribute's functional/multi-valued status is unclear, or value incompatibility is fuzzy,
  the system **does not flag** (suppress over false-alarm). Missing a real conflict is
  acceptable; a false positive is not.
- **R4 — Gray-zone value check (narrow).** An LLM is invoked only to decide whether two
  values for the *same functional slot* are genuinely incompatible vs synonymous
  ("first electric battery" vs "early electric battery" → same; 1799 vs 1800 → incompatible).
  It is not used to decide whether an arbitrary pair of facts relate. Per R3, an uncertain
  gray-zone verdict suppresses rather than flags.
- **R5 — Clustered surfacing.** All facts competing for the same conflicting slot are
  surfaced as **one** contradiction item in the contradictions tab, not as N pairwise
  flags. The 1799/1800/1801 case appears as a single item listing the competing values.
- **R6 — Resolution semantics unchanged.** Existing behavior is preserved: a new fact that
  contradicts an *established (active)* fact is marked `proposed` and in-contradiction;
  two mutually-contradicting facts ingested together are both `proposed` and appear in the
  contradictions tab for human vetting. No automatic winner-selection or graph mutation is
  added.
- **R7 — Backfill + re-evaluate.** Apply the new detector to facts already in graphs:
  extract claims for existing facts, detect latent conflicts, and re-evaluate currently
  emitted flags — structural false positives auto-clear, real conflicts re-surface as
  clusters. This runs as a one-off migration/backfill pass over existing graphs.
- **R8 — Replace the existing path.** The new structural detector replaces the current
  cosine-recall + bare-boolean `ConflictJudge`/`ConflictFlagger` contradiction path
  outright (not run gated alongside it).
- **R9 — Efficiency.** Detection for the common case is an index/lookup over claim slots
  with **zero LLM calls**; LLM use is limited to the R4 gray-zone and to claim extraction
  (once per fact at write time, cacheable). LLM calls must not scale O(candidates) per write.

---

## Key flows

- **F1 — Write-time detection.** A2/A1 writes a fact → claims extracted (R1) → for each
  functional claim, look up other facts in the same (subject, attribute) slot → if an
  incompatible value exists (R2, gray-zone-checked per R4), record a contradiction and
  apply R6 status semantics.
- **F2 — Review.** A2 opens the contradictions tab → sees one clustered item per conflicting
  slot (R5) with the competing values and their source facts → vets/resolves via the
  existing workflow (R6).
- **F3 — Backfill.** Operator runs the backfill pass (R7) → claims extracted for existing
  facts → latent conflicts surfaced as clusters, structural false positives cleared.

## Acceptance examples

- **AE1 (covers R2, R5):** Facts asserting voltaic-pile invention year 1799, 1800, and 1801
  produce exactly **one** contradiction cluster for `(voltaic pile, invention year)` listing
  all three values — not three pairwise flags.
- **AE2 (covers R2, R3):** "Galvani discovered animal electricity" and "Volta disproved
  animal electricity" produce **no** contradiction (different subjects/claims; not a
  same-slot value conflict).
- **AE3 (covers R2):** "Volta discovered methane" and "Volta discovered the electrochemical
  series" produce **no** contradiction (`discovery` is multi-valued).
- **AE4 (covers R2):** "Professor at Como in 1774" and "Professor at Pavia in 1779" produce
  **no** contradiction (different roles/slots; both true over time).
- **AE5 (covers R4):** "the first electric battery" and "an early electric battery" for the
  same subject/attribute are judged **synonymous** → no contradiction.
- **AE6 (covers R7):** After backfill over the current `volta_video` graph, the 9 existing
  false-positive flags are cleared and the voltaic-pile-year conflict remains as one cluster.

---

## Scope boundaries

### In scope
- Claim extraction + functional-attribute tagging at write time (R1).
- Structural same-slot detection with precision-first suppression (R2, R3).
- Narrow LLM gray-zone value-compatibility check (R4).
- Clustered surfacing in the contradictions tab (R5).
- Backfill + re-evaluation over existing graphs (R7).
- Replacing the existing contradiction path (R8).

### Deferred for later
- **Implicit / cross-vocabulary contradiction detection** — catching conflicts between
  facts that share no subject/attribute surface (the Tier-B `AspectTagger` premise). Inherently
  low-precision; conflicts with the precision-first posture. Revisit only if a precision-safe
  mechanism emerges.
- **Auto-resolution** — automatically selecting a winning value or mutating the graph
  (e.g. supersede-on-newest). Resolution stays human-driven (R6).
- **Embedding / recall-floor retuning** for the merge/dedup path.

### Outside this product's identity
- Detecting contradictions across different *entities'* claims as factual errors (e.g.
  recording that two historical figures disagreed is not a graph contradiction).

---

## Success criteria

- On the `volta_video` graph: false positives drop from 9 to ~0; the one real conflict is
  shown as a single cluster (matches the spike).
- No regression in catching genuine same-slot value conflicts (recall on real conflicts ≥ today).
- LLM calls per write no longer scale with candidate count; common-case detection makes zero
  LLM calls.
- Measured via the eval harness (the `matt/volta_video` case and any added contradiction cases).

## Dependencies / assumptions

- **Claim-extraction quality is the central risk.** The spike showed `gpt-4o-mini` sometimes
  mis-canonicalized subjects or collapsed distinct properties into one attribute. The design
  leans on (a) a capable, cached extraction step and (b) precision-first suppression as the
  backstop. Assumes extraction can be made consistent enough that R3 suppression keeps
  precision high without destroying recall.
- Assumes the existing `proposed` / in-contradiction status model and contradictions-tab
  resolution endpoints remain the resolution surface (R6).
- Assumes a claim/slot store can be added (HOW — for planning).

## Outstanding questions (for planning)

- Claim/slot **storage shape**: new normalized claims table keyed by (subject, attribute)
  vs storing claims on the existing fact records. (HOW)
- Whether R5 clustering is a **display-layer grouping** over existing pairwise contradiction
  records or a change to how contradictions are persisted. (HOW)
- **Subject canonicalization** strategy (entity normalization / aliasing) needed for slots to
  align — how far to take it. (HOW; flagged because it caused the spike's one missed conflict.)
- Extraction **model + caching** choice. (HOW)
