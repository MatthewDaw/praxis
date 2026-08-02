---
title: A supplied meta.auditTrail is merged, never replaced — and provenance has a cheap read path
date: 2026-08-01
category: conventions
module: knowledge/serve (facts_candidates.py `create`, app.py `/facts/by`), knowledge/mcp/server.py, frontend-react (AuditTimeline)
problem_type: convention
component: data_integrity
severity: high
related_components: [requirements, agent_factory, snapshots, dashboard]
applies_when:
  - Writing a fact through `praxis_insert_fact` / `POST /candidates` with a `meta` object
  - Moving, splitting, or hand-repairing a ticket between snapshots
  - Adding a facade-owned key to the meta a write path composes
  - Reading a fact's history, or auditing provenance across a whole plan snapshot
  - Changing how the dashboard renders a fact's audit trail
tags: [praxis, provenance, audit-trail, insert-fact, snapshots, data-loss, projection, agent-factory]
---

# A supplied `meta.auditTrail` is merged, never replaced

`praxis_insert_fact` (`POST /candidates` → `FactsCandidates.create`) composes the stored meta as
`{**user_meta, "title": title, "auditTrail": <merged>}`. Two keys are not stored verbatim:

- **`title`** mirrors the required `title` argument. This is by design — the argument is
  authoritative and `meta.title` is its projection.
- **`auditTrail`**, if the caller supplies one, is **merged**: the caller's entries are kept in
  order and the backend's own `"created"` entry is appended after them.

Every other key round-trips byte-identical.

It used to *replace* the caller's trail with a single `"created"` entry. That was silent, lossy,
and landed on exactly the seam that most needs provenance: insert is what a snapshot-to-snapshot
move, split, or manual repair goes through, and the trail is the history such a move has to carry
across. A moved ticket arrived with no error, no warning, and one plausible-looking `"created"`
entry that read as if it had been authored fresh. It was caught only by diffing field-by-field
against a pre-move dump.

Merge beats both alternatives: silent overwrite loses the history, and a hard error would force
every mover into the insert-then-`praxis_edit_fact` dance (which worked, but cost an extra
spurious `"edited"` entry per fact). Appending keeps both properties — the history arrives, and
the move is still recorded.

A supplied trail that is not a list of objects is not provenance; it is dropped rather than
persisted as a shape every reader then has to defend against.

**If you add another facade-owned key to a write path, it must merge or fail loudly.** Silent
rewriting of one reserved key inside a documented free-form object is the failure mode this
whole entry exists to prevent.

## The one cap, and why it is not silent

Trails are bounded **once, at write time** (`_compact_audit_trail`: head 3 + recent 20). An
elision always leaves a visible `action="compacted"` marker entry naming how many were dropped.
That marker is load-bearing: it is the only record that history was deliberately discarded, and
without it a compacted trail is indistinguishable from a genuinely short one. Never render a
trail in a way that hides `note`.

There is **no cap on any read path** — `praxis_get_fact`, `praxis_facts_by`, `GET /candidates`
all return the complete stored trail.

## Reading provenance

Two ways, and the cheap one exists for a reason:

- `praxis_get_fact(cid, space=..., snapshot=...)` — one fact, whole, including its trail.
- `praxis_facts_by(..., fields="provenance")` — identity + `auditTrail` + `auditTrailCount` per
  fact, **without the bodies**. An exhaustive `facts_by` over a real plan snapshot returns every
  requirement's full text (~1.2 MB across 170 tickets), which overruns an agent's context and
  makes "just read the trail" impractical exactly when a repair needs it. An unknown `fields`
  value is a 400, never a silent fall back to the full payload.

The dashboard renders the same trail in the candidate detail panel (`AuditTimeline`), collapsed
to the first few entries with the **total count always stated**. State the count on any new
provenance view too: the original loss stayed invisible because there was no ordinary way to
*look* at a trail, and a view that can silently show a subset recreates that gap.

Snapshot-resident facts reach the dashboard by being loaded into working memory
(`POST /snapshots/load`), a row-level copy that carries `meta` intact — so that path is part of
the UI's provenance chain and is covered by
`knowledge/serve/tests/test_insert_fact_meta_audit_trail.py`.

## Related

- `POST /insights` (`praxis_add_insight`) never had this defect: it persists the writer's `meta`
  as given, trail included.
- Ticket/check writes are identity-keyed — see
  [ticket-writes-are-identity-keyed-never-deduped.md](ticket-writes-are-identity-keyed-never-deduped.md).
