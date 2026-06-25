# Proposal: Tabular / Templated Ingestion Integrity

**Status**: Draft · **Raised**: 2026-06-24 · **Owner**: TBD
**Source**: agent-factory gap **H6** (`agent_factory/docs/praxis-gaps.md`) — the one
knowledge-side hole that blocks the factory's first workload (a tabular-heavy PRD).

---

## Problem

When Praxis ingests **tabular or templated input** — a requirements table, a row-per-field
spec, repeated `key: value` blocks — it silently **under-emits facts**: distinct rows are
dropped or collapsed, so the stored graph is quietly incomplete. A plan built on a
silently-incomplete graph is the agent factory's worst failure mode, and the first PRD is
tabular-heavy, so this bites immediately.

There are **two independent loss points** on the write path. A fix that addresses only one
does not solve the problem — and the two are **coupled**: linearization (loss point A) makes a
table *look* fixed in a quick test, but the dedup guard (loss point B) is the only actual
guarantee. They ship together or not at all (see §2).

### Loss point A — distillation under-emits

`PromptIngestor.synthesis`
([knowledge/injestion/injestor_variants/prompt_injestor.py:140](../../knowledge/injestion/injestor_variants/prompt_injestor.py))
is a single LLM call under one hardcoded `SPLIT_PROMPT` ("break into discrete ideas, one per
line"), then `text.splitlines()`. Given a 30-row table, the model summarizes or collapses
rows that share a sentence shape instead of emitting one line per row. The offline fallback
`segment_passthrough` (line 99) is **sentence**-based (`_SENTENCE_SPLIT_RE`, line 79) and has
no concept of rows at all — a markdown/CSV table is mangled rather than parsed.

### Loss point B — the deduper over-merges sibling rows

Every insight then flows through `graph.write` → the write-policy pipeline → `Deduper`
([knowledge/knowledge_graph/write_policy/write_step_variants/deduper.py:32](../../knowledge/knowledge_graph/write_policy/write_step_variants/deduper.py)).
Stage 2's `MergeJudge.same_lesson` collapses **semantically similar** text into an `update` on
an existing fact. Tabular rows differ only in their key ("field X required: yes" / "field Y
required: yes") — exactly the case the judge is most likely to wrongly rule "same lesson" and
silently merge two distinct requirements into one.

> **Critical consequence:** `/insights` skips the lossy splitter (loss point A) but still runs
> the full write pipeline — `ingest_insights` calls `graph.write` per insight
> ([knowledge/serve/pipeline_adapter.py:151](../../knowledge/serve/pipeline_adapter.py)). So a
> "linearize locally and send via `/insights`" shim fixes A but **not** B. B is Praxis-only.

---

## What I want to build

Three changes, ordered by leverage. All reuse the existing schema — **no new database**, and a
new table only in the optional item #3.

### 1. Table-aware distillation branch — *fixes loss point A*

Detect tabular/templated input and route it to a **`TableLinearizer`** instead of the prose
`SPLIT_PROMPT`.

- **Detection:** markdown tables (`| ... | ... |` with a `---` separator row), CSV-ish
  delimited rows, and repeated `key: value` blocks.
- **Linearization:** emit **one `Insight` per row**, folding the row/column identity *into the
  text* so each fact is lexically distinct and self-contained — e.g. `daily_prompt | required |
  yes` becomes `"For the daily_prompt field, required = true"`, not `"required: true"`. Distinct
  text is what stops the embedder/judge from collapsing siblings downstream (defense in depth
  with #2).
- **Deterministic, not prompt-engineered.** For *detected* tables, route to the deterministic
  `TableLinearizer`. The reference research is blunt that prompt interventions don't fix
  structural extraction problems — so the LLM row-count check is a **guardrail that triggers the
  deterministic fallback**, not a retry-the-LLM loop. Reserve prompt-augmentation only for
  ambiguous *templated prose* we chose not to linearize.
- **Two call sites need the branch:**
  - `PromptIngestor.synthesis` — when an LLM is present, route detected tabular input to the
    linearizer (don't rely on the prose prompt).
  - `segment_passthrough` — the offline path needs a table branch; today it destroys tables.
- **One liftable module.** The linearizer is built locally first (port) and promoted into Praxis
  later (§Sequencing step 3). Write it as a single self-contained module so it can move
  wholesale — if the local and Praxis copies drift, behavior shifts under us at migration.
- **Output contract:** unchanged. Still a `list[Insight]`
  ([knowledge/injestion/injestion_def.py:12](../../knowledge/injestion/injestion_def.py)) →
  more rows in the existing **`facts`** table.

### 2. Dedup slot-guard — *fixes loss point B (Praxis-only, highest leverage)*

Make the `Deduper` consult the **`claims`** rows that `ClaimExtractor` already produces
(`(subject, attribute, value, functional)`, indexed `claims_slot`).

**Key on the full functional slot `(subject, attribute)`, not subject alone.** Subject-only
protects a *field → required* table (subject varies per row) but silently fails the other common
PRD shape: **same subject, different attribute** — a role×permission table where every row is
`subject = coach` (`coach can edit themes`, `coach can edit prompts`, `coach can manage roster`).
Subject-only never fires and the judge folds them into one "coach permissions" fact. Our first
PRD has exactly this shape (the roles/permissions and ratings tables). Slot-keying covers both.

**It is a three-way decision, not a binary block.** Compare the incoming functional claim against
each candidate's claim on the same slot:

| Case | Condition | Action |
|---|---|---|
| Distinct facts | different `(subject, attribute)` slot | **block merge** — both stay active |
| Contradiction | same slot, **different value** | **route to the contradiction engine** — do *not* merge and do *not* let both go active |
| Genuine duplicate | same slot, same value | **allow merge** — this is what makes re-ingest idempotent |

> A guard that only "blocks merge on a differing slot" gets the contradiction case wrong: two
> conflicting requirements both go active and you've traded silent-merge for silent-conflict.
> All three cases must be spelled out in the implementation.

**Fail-safe when claims are absent.** The guard depends on `ClaimExtractor` having produced a
functional claim with a populated subject — an LLM step that *will* sometimes return empty/null.
On a tabular-flagged insight with a missing/empty functional claim: **do not merge — demote to
`proposed`** for review. Fail toward keeping rows distinct (the whole point), and let a human
resolve the low-confidence ones. (This also settles the block-vs-demote open question:
demote-to-`proposed` is the right default for the missing-claim / low-confidence path.)

- This is the part that **cannot** be shimmed away locally — without it, no amount of clever
  linearization is *guaranteed* safe against sibling-row collapse. Note that #1's identity-folded
  text (`"For the daily_prompt field, required = true"` vs `"...email field, required = true"`)
  still shares ~80% of tokens, so cosine stays high and the judge still gets consulted — the
  slot-guard remains the only guarantee. **#1 does not let us skip #2.**
- **Storage effect:** read-only against `claims`; changes an in-memory `WriteDecision`. Net
  effect is *fewer* wrong merges → more correct rows in `facts`.

> Scoping note: confirm the exact wiring against `MergeJudge` and the store's recall/write pass
> before implementing — the guard sits in `Deduper.apply` ahead of the stage-2 judge loop,
> reading `decision.claims` and the claims on each candidate hit, and the contradiction-case path
> must hand off to the existing conflict detector rather than just suppressing the merge.

### 3. Ingestion completeness report — *makes the vision's "audit the rejected pile" real*

Today nothing reconciles **rows-in vs. facts-out**; the candidate surface exists
(`serve/facts_candidates.py`, `/candidates?state=rejected`) but there's no count to audit
against.

- **Default (no schema change):** compute on the fly and return in the ingest response —
  `{rows_submitted, facts_active, merged_into_existing: [target_ids], rejected}`.
- **Optional (the only place a new table is justified):** persist an `ingest_runs` row
  (run id, source, counts, timestamp) **only if** ingest audits must survive across sessions.
  Start without it.

---

## Storage impact summary

| Change | Touches | New table? |
|---|---|---|
| 1. Table linearizer | writes more rows to `facts` | No |
| 2. Dedup slot-guard | reads existing `claims`; in-memory decision | No |
| 3a. Completeness report (inline) | ingest response only | No |
| 3b. Completeness report (durable) | new `ingest_runs` | Optional, deferred |

No new **database**. Existing tables: `facts`, `claims`, `fact_edges`, `mounted_snapshots`,
`cached_*` mirrors.

---

## Sequencing (respects the factory's thin-harness bet)

1. **Local shim first (factory port, no Praxis change):** table-linearizer → send via
   `/insights` → read back `/candidates` and reconcile counts (#1 + #3a). Unblocks the first
   PRD. Accepts that B can still over-merge near-identical rows.
2. **Praxis fix:** the dedup slot-guard (#2) — small, surgical, and the part that can't be
   shimmed. This is the change I'd actually land in Praxis. Ship it *with* #1, not after — #1
   alone only makes the table look fixed.
3. **Later (optional):** promote the linearizer into Praxis as a first-class structured-input
   distillation branch so all consumers benefit and the harness gets thinner again; add durable
   ingest-audit (#3b) only if needed.

---

## Acceptance criteria

**Reproduce on the real PRD first.** Before building, write the failing test from an actual
`inspiration/` table (the roles/permissions or metrics table — *not* a synthetic one) and
quantify the current under-emission. That validates the problem size and becomes the gate.

- [ ] **Real-PRD reproduction:** a failing test on an actual `inspiration/` table demonstrates
      and quantifies current under-emission before any fix lands.
- [ ] A markdown table of N distinct rows ingests to N distinct active facts (none silently
      merged) — both with an LLM and on the offline `segment_passthrough` path.
- [ ] **Both table shapes covered:** field→required (subject varies) *and* role×permission (same
      subject, attribute varies) each yield one fact per row.
- [ ] **Contradiction (negative) test:** two rows with the **same slot, conflicting values**
      produce a *contradiction*, not two silent active facts — proves the guard routes case 2 to
      the conflict engine rather than disabling conflict handling.
- [ ] **Idempotency test:** ingesting the same table twice yields N facts, not 2N — proves the
      guard preserves legitimate exact/same-value dup merge.
- [ ] **Missing-claim fail-safe:** a tabular-flagged row with an empty/null functional claim is
      *not* merged — it demotes to `proposed`.
- [ ] Ingest returns a completeness report whose `facts_active + merged + rejected` accounts for
      every submitted row.
- [ ] No regression on prose ingestion (the existing eval cases and cassettes still pass).
- [ ] No new database; schema change limited to the optional `ingest_runs` table if #3b is taken.

---

## Open questions

- Linearizer home: local port vs. Praxis distillation branch for the first cut (leaning local
  shim per the sequencing above).
- Detection precision: how aggressively to classify "templated" prose as tabular without
  false-positiving on ordinary lists.
- *(Resolved)* Block vs. demote: distinct slots → block merge (both active); missing/empty claim
  → demote to `proposed`. See §2.
