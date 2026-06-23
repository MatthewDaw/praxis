# Feature Specification: Deterministic Ingestion Cassette

**Feature Branch**: `002-deterministic-ingestion-cassette`

**Created**: 2026-06-23

**Status**: Draft

**Input**: User description: "docs/proposals/2026-06-22-deterministic-ingestion-cassette.md"

## Overview

The application eval suite (`matt/applications/*`) distills its seeded source material through a
real LLM ingestion splitter (`ingest_model`) whose output text **varies run-to-run even at
temperature 0**. Because every downstream layer (the embedding fixture, the constructed knowledge
graph the agent reads) is keyed on that text, the suite is both **expensive** (every embed is a
live API call, repeated per write) and **irreproducible** (the graph the agent sees differs each
run). A measured A/B probe confirmed facts a check asserts were present one run and gone the next,
*purely from re-distillation* — so a pass/fail swing cannot be attributed to the policy under test.

This feature makes the **ingestion → embedding → graph-construction** layer deterministic and
cheap by recording the ingestion LLM's output once into a committed, model-keyed replay cassette
and replaying it offline — the same committed / keyed / loud-miss pattern the embedding cache
already uses. It is the prerequisite that turns the application suite into a usable measurement
instrument (unblocking the `model-robust-recall-policies` spec's deferred FR-030/SC-013), and it
unlocks deterministic component cases built from *real* distilled insights.

It is **eval-infrastructure determinism, not a recall-policy change**, and it explicitly does
**not** make application cases fully deterministic end-to-end — the live agent and judge remain
nondeterministic.

## Clarifications

### Session 2026-06-23

- Q: Should feature 002 also absorb the second FR-030/SC-013 prerequisite (active-fact
  retrievability / `ingest_state: active`)? → A: No — 002 stays **ingestion-cassette-only**. The
  active-fact-retrievability axis is the **explicit next follow-up** after this feature, scoped as
  its own unit; SC-007 keeps its "subject to the separately-tracked prerequisite" caveat.
- Q: Which application cases flip from `embedder: live` to `cached` in this feature? → A: **All**
  `matt/applications/*` cases that use `ingest_model`, in one pass. The committed distilled-text +
  embedding fixture footprint is accepted, mitigated by the compact vector codec.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Record-once / replay-offline ingestion (Priority: P1)

A maintainer runs an application eval case offline. The ingestion splitter's output for each
seeded input is served from a committed cassette instead of a live model call, so the same
distilled insights — and therefore the same knowledge graph — are produced on every run, with no
live ingestion call.

**Why this priority**: This is the core capability and the MVP. Without it, every other benefit
(cached embeddings, attributable measurement, deterministic component cases) is impossible. It
delivers reproducibility on its own.

**Independent Test**: Run an `ingest_model` case twice offline against the committed cassette and
assert the set of distilled facts in the graph is byte-identical across runs, with zero live
ingestion calls. Change a seeded input or the model id and confirm a **loud miss** (not a silent
stale result).

**Acceptance Scenarios**:

1. **Given** a committed ingestion cassette covering a case's seeded inputs, **When** the case's
   ingestion runs offline (no key), **Then** each input's distilled output is replayed from the
   cassette and no live model call is made.
2. **Given** the same cassette, **When** the case runs twice, **Then** the resulting set of
   distilled facts is identical both times.
3. **Given** replay-only mode (no key) and a seeded input or `ingest_model` that changed since the
   cassette was recorded, **When** ingestion runs, **Then** it fails loudly with a refresh
   instruction rather than passing on a stale or missing fixture.
4. **Given** neither a committed cassette nor a key, **When** a case requires cassetted ingestion,
   **Then** the case is skipped (not mis-run), consistent with the embedding/verdict fixtures.

---

### User Story 2 - Cached embeddings for the application suite (Priority: P2)

With ingestion output now stable, a maintainer flips application cases from the uncached `live`
embedder to the committed-vector (`cached`) path, so the graph-construction layer replays entirely
from committed fixtures and makes zero live embedding calls.

**Why this priority**: This is the cost win and the second half of "the graph-construction layer
is deterministic and offline." It depends on US1 (stable text is the precondition for a stable
embedding key), so it follows it.

**Independent Test**: After recording the ingestion cassette and refreshing the embedding cache,
run a previously-`live` application case offline on `cached` and confirm it constructs the graph
with zero live embedding or ingestion calls and a stable result.

**Acceptance Scenarios**:

1. **Given** a recorded ingestion cassette, **When** the embedding cache is refreshed, **Then** it
   records vectors for the now-stable distilled strings.
2. **Given** both fixtures committed, **When** a flipped application case runs offline, **Then**
   the graph-construction layer issues zero live ingestion or embedding calls.
3. **Given** a refresh is needed, **When** the maintainer regenerates fixtures, **Then** the
   documented order is ingestion-cassette first, embedding-cache second, and following it produces
   a self-consistent pair.

---

### User Story 3 - Deterministic component cases from real distilled insights (Priority: P3)

A maintainer builds an offline dedup/conflict component case whose seeded facts are *real
LLM-distilled* atomic insights (captured in the cassette), rather than hand-written verbatim
strings, and it replays deterministically offline.

**Why this priority**: A quality multiplier — it lets component tests exercise the system on
realistic distilled text — but it is additive on top of the deterministic ingestion layer, so it
is lowest priority.

**Independent Test**: Author a component case seeded via the cassetted distillation of a real
input and confirm it replays offline with identical facts every run.

**Acceptance Scenarios**:

1. **Given** a committed ingestion cassette, **When** a component case seeds from cassetted real
   distillation, **Then** it produces the same insights offline on every run.

---

### Edge Cases

- **Stale fixture** (a seeded input edited, or `ingest_model` bumped) → loud miss with a refresh
  instruction; never a silent stale pass.
- **No verdict/fixture source and no key** → the case is skipped, not failed or mis-run.
- **Fixture growth** — committing distilled outputs and their embeddings for the whole application
  suite is sizable and accepted (all `ingest_model` cases flip); the compact vector codec is the
  mitigation, not subsetting which cases flip.
- **Partial determinism confusion** — because the agent and judge stay live, a flipped case is
  *not* fully reproducible end-to-end; the guarantee is scoped to graph construction and must be
  communicated as such.
- **Refresh-order mistake** — refreshing embeddings before re-recording ingestion would cache
  vectors for soon-to-be-stale text; the documented order prevents this.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The eval harness MUST serve the ingestion splitter's output for a given
  `(ingest_model, raw input)` from a committed replay cassette when one is available, so the
  distilled text is identical across runs.
- **FR-002**: On a cassette hit, the harness MUST NOT make a live ingestion model call.
- **FR-003**: On a miss with recording enabled (a key present), the harness MUST compute the real
  output, record it under its key, and persist it; on a miss with recording disabled, it MUST fail
  loudly with a refresh instruction (no silent stale or empty result).
- **FR-004**: The cassette key MUST include the ingestion model id, so changing the model is a
  clean miss rather than a silent reuse of another model's output.
- **FR-005**: With neither a committed cassette nor a key available, the affected case MUST be
  skipped (graceful degradation), matching the embedding-cache and verdict-cassette behavior.
- **FR-006**: Introducing the cassette MUST NOT change how eval cases are authored — the
  distillation step receives the same input/output contract; only its source (live vs replayed)
  changes.
- **FR-007**: The system MUST provide a regeneration path that, with a key, re-records the cassette
  by re-running the ingestion of every case that uses `ingest_model`, capturing exactly those
  inputs, for commit.
- **FR-008**: Once ingestion replays deterministically, **all** `matt/applications/*` cases that
  use `ingest_model` MUST be flipped to the committed-vector embedding path (`cached`) instead of
  the uncached live embedder.
- **FR-009**: The fixture-refresh procedure MUST be documented as an ordered two-step process —
  record the ingestion cassette first, then refresh the embedding cache against the now-stable
  strings — and following it MUST yield a self-consistent fixture pair.
- **FR-010**: An offline application-suite run on committed fixtures MUST make zero live ingestion
  or embedding calls for the graph-construction layer.
- **FR-011**: The feature MUST NOT claim or imply full application-case determinism; its scope is
  the ingestion → embedding → graph-construction layer, with the agent and judge remaining
  nondeterministic, and this boundary MUST be documented.
- **FR-012**: The feature MUST be limited to the eval harness; no production ingestion/serve path
  is changed.

### Key Entities *(include if feature involves data)*

- **Ingestion cassette**: a committed mapping from `(ingestion model id, raw input text)` to the
  distilled output text. Replayed offline; recorded on a miss only with a key; a stale/missing key
  is a loud error. The fourth keyed-replay surface alongside the embedding cache and the
  merge/conflict verdict cassettes.
- **Embedding cache (existing)**: committed `(model, text) → vector` fixture; becomes usable for
  the application suite once the ingested text is stable (consumer of the cassette's output).
- **Application eval case (existing)**: a case that distills seeded source via `ingest_model` and
  constructs a knowledge graph; the primary beneficiary, eligible to flip from `live` to `cached`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Re-running an `ingest_model` case's ingestion twice offline yields an identical set
  of distilled facts both times (today it drifts run-to-run).
- **SC-002**: An offline application-suite run on committed fixtures makes **zero** live ingestion
  or embedding calls for the graph-construction layer.
- **SC-003**: A changed seeded input or model id surfaces a loud failure with a refresh
  instruction in 100% of cases — no silent stale pass ever occurs.
- **SC-004**: Every `matt/applications/*` case that uses `ingest_model` (currently `embedder:
  live`) runs on `cached` committed vectors deterministically after this feature — none remain on
  the uncached live embedder.
- **SC-005**: Per-run live embedding calls for the application suite's graph construction drop to
  zero on replay (from the current 2–3 live embeds per write).
- **SC-006**: At least one deterministic component case seeded from real cassetted distillation
  exists and replays offline with identical facts every run.
- **SC-007**: The ingestion-nondeterminism blocker on the `model-robust-recall-policies` spec's
  FR-030/SC-013 is removed — application-suite outcomes for a write-policy change become
  attributable to that change (subject to the separately-tracked active-fact-retrieval
  prerequisite).

## Assumptions

- The established embedding-cache contract (committed, model-keyed, loud-miss, record-with-key,
  skip-when-unavailable) is the pattern to mirror; this feature reuses it rather than inventing a
  new mechanism.
- A live model key is available locally for recording fixtures; CI and routine runs replay
  offline.
- The "users" are the eval-harness maintainers and the team using the application suite to measure
  write-policy changes.
- This feature builds on the `model-robust-recall-policies` work (the embed-once write path and the
  `VerdictCassette` keyed-replay pattern). This branch is **stacked on the 001 branch**
  (`001-us3-tier-b-implicit-contradiction`), so that code is present and implementation can proceed
  now; its PR **merges after** the 001 stack lands on `main` — building on the 001 tip avoids
  eval-harness merge conflicts.
- A unified keyed-replay abstraction across the four cassette surfaces (embeddings, ingestion,
  merge verdicts, conflict verdicts) is **deferred** — the ingestion cassette ships as a near-copy
  of the embedding cache; extraction happens later, once three concrete instances prove the shared
  shape, not speculatively.
- All `matt/applications/*` cases that use `ingest_model` flip to `cached` in this feature (one
  pass, not incremental). The committed distilled-text + embedding fixture footprint is accepted as
  a tradeoff for full coverage, mitigated by the compact vector codec; if the footprint proves
  prohibitive in practice it can be revisited, but bounding it is not a goal of this feature.
- The **second** prerequisite for FR-030/SC-013 — marking distilled facts retrievable so the
  active-gated reader surfaces them (e.g. an `ingest_state: active` axis, after main's active-fact
  gating) — is **out of scope** here and is the **explicit next follow-up** after this feature
  (its own unit of work). Shipping 002 alone therefore does not, by itself, fully unblock
  FR-030/SC-013 (see SC-007).
- Full application-case determinism is explicitly **not** a goal; the live agent and judge remain
  nondeterministic, so "the application suite runs in CI" is **not** a claim this feature makes.
