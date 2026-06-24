# Skill Sharing — Browse & Fold-In

**Date:** 2026-06-23
**Scope:** Standard
**Status:** Requirements (ready for `/ce-plan`)

## Problem

There is no clean way to move skills/ideas between knowledge graphs — neither
between a user's snapshots nor between users in the same org. Today a fact is
only visible if it is org-shared or owned by the reader, and there is no
user-facing path to copy a selected subset of one graph into another. The
result: good skills stay siloed in whoever's graph (or snapshot) first ingested
them.

Note: a "skill" is not a distinct entity — it is a cluster of atomic `facts`
(grouped by source doc / `cluster_id`). "Sharing a skill" means moving a
selected subset of facts across a cache/user boundary.

## Goal

Let a user open another source in their org — a teammate's live graph or any
saved snapshot — browse it grouped by skill/cluster, cherry-pick the ideas they
want, and **fold them into their own live graph**, deduped and conflict-checked
against what they already have.

## Users & Access

- **Actor:** any member of an org.
- **Trust boundary:** the org. Within an org, a member can browse and take
  anything from any other member's graphs and from any snapshot. No
  publish/opt-in step, no per-fact privacy gate inside the org.
- **Source = any of:** a teammate's live graph, the actor's own other snapshots,
  or any org snapshot (`snapshot:<name>`).
- The source is **read-only** from the actor's side; folding-in never mutates
  the source.

## Behavior

### Browse a source
- List the source's facts **grouped by cluster/skill** (`cluster_id` / `label`,
  falling back to source doc).
- Each group is expandable to its individual facts.

### Select
- **Default unit is the cluster:** checking a skill selects all its facts.
- **Fact-level override:** expand a cluster and deselect (or select) individual
  facts.

### Fold in
- Selected facts are **copied** into the actor's live graph as new facts they
  own.
- Copies run through the **dedup + conflict-flagging** portion of the existing
  write policy (Deduper → ConflictFlagger), against the actor's current graph.
  - Skip the LLM distillation step — foreign facts are already atomic.
- Each copied fact carries **provenance**: which user/source it came from.
- Conflicts surface using the existing contradiction path rather than silently
  overwriting.

## Success Criteria

- A user can, from another user's graph or any snapshot, fold a selected set of
  skills into their own graph and immediately retrieve them.
- Folding in a skill the user already has does not create duplicates (dedup
  fires).
- Folding in a skill that contradicts an existing fact surfaces a conflict
  rather than silently winning.
- Folded facts are attributable to their origin.
- The operation is synchronous (no LLM round-trip).

## Scope Boundaries

**In scope**
- Browse-any-org-source, cluster-grouped view.
- Cluster-default / fact-override selection.
- Copy-with-provenance fold-in through dedup + conflict checks.

**Deferred for later**
- **Reference/subscribe model** and an org-level canonical skill library
  (Approach C). If personal curation proves to be the wrong frame and the real
  need is a shared canonical set, revisit — it is a different product bet, not an
  extension of this one.
- Propagation of source edits to already-folded copies (copies intentionally
  drift).
- Cross-org sharing.
- Publish/opt-in or per-fact privacy controls (org is full-trust by decision).

## Open Questions

- **Cluster integrity on partial fold-in:** when a user deselects some facts in a
  cluster, does the folded subset keep the source `cluster_id`/`label`, or get
  re-clustered in the actor's graph? (Leaning: keep source label as provenance,
  let assign-write-step re-home it.)
- **Edges:** do `fact_edges` between selected facts come along with the fold-in,
  or only nodes? (Snapshot "add" mode today merges nodes by id only.)
- **Provenance shape:** new column(s) vs. `meta` JSON on the copied fact — a
  planning/implementation decision.

## Dependencies / Assumptions

- Relies on existing cluster grouping (`cluster_id`/`label`) being populated on
  facts to make the browse view useful.
- Reuses the write-policy dedup/conflict components and the contradiction
  surfacing path.
- Assumes browsing another member's *live* graph is acceptable org-wide (stated
  decision: same-org = full access).
