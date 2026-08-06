---
title: meta.finished_at is server-written on every build_state transition — no client ever supplies it
date: 2026-08-03
category: conventions
module: knowledge/finished_at.py, knowledge/serve/facts_candidates.py (`update`), knowledge/knowledge_graph/.../postgres_vector_graph.py (`release_requirement`, `regress_requirements`), agent_factory/hooks/_ticket_state.py (`release`)
problem_type: convention
component: data_integrity
severity: medium
related_components: [agent_factory, requirements, productivity_series, migrations]
applies_when:
  - Adding or changing any path that can set `meta.build_state`
  - Tempted to stamp a completion/started/updated timestamp from a client or agent
  - Writing a query that filters tickets by `finished_at` (or adding an index on it)
  - Investigating a finished ticket missing from a finished-by-date report
  - Adding a meta key whose value is a timestamp
tags: [praxis, agent-factory, timestamps, finished-at, build-state, index, data-integrity, server-authoritative]
---

# `meta.finished_at` is server-written, never client-supplied

A completion timestamp is a fact about a write the **server** makes. A client stamping one is
guessing at a clock it does not own — it can be skewed, stale, or forged. So `finished_at` has
exactly one producer, `knowledge/finished_at.py`, and every server path that can move
`build_state` routes through it:

| path | what it does |
|------|--------------|
| `PATCH /candidates/{cid}` (`_praxis.patch_meta`) → `FactsCandidates.update` | stamps / clears from the server clock |
| `POST /requirements/{cid}/release` → `release_requirement` | stamps (finished) / removes (incomplete) in SQL |
| `POST /requirements/regress` → `regress_requirements` | removes it — the ticket is no longer finished |

**The one rule** (`finished_at.resolve`): a write that sets `build_state="finished"` stamps the
server clock; a write that sets it to anything else drops the key; a write that does not touch
`build_state` leaves it alone (so a heartbeat or run-marker patch cannot drag a completion
forward). A caller-supplied `finished_at` is discarded before the merge on every path,
including the `detail` map of `/requirements/regress`.

Clients do not participate. `_ticket_state.release()` sends `build_state` and nothing else.

## Why this is a convention and not a preference

The key previously had TWO producers that disagreed on shape: the server wrote a fixed-format
UTC ISO-8601 string; agent_factory's ticket loop wrote a bare `time.time()` float. One plan
carried both (5 epoch rows, 3 ISO). Nothing errored — `productivity_series` parsed both — but
`snapshots_finished_at_idx` (migration 0013) is a **TEXT** expression index over the ISO shape
(a `timestamptz` cast is STABLE, not IMMUTABLE, so Postgres refuses it in an expression index).
An epoch string sorts as text far outside any ISO range bound, so those rows silently fell out
of every `BETWEEN` query using the index. **A short answer, not a failure** — the worst kind.

Making the second producer agree on format would have left the same trap one careless edit
away. Removing the second producer closes it.

## Shape

`2026-07-25T03:50:06.740712+00:00` — zero-padded, UTC, microsecond precision, explicit offset.
Anything else is not range-indexable. `finished_at.is_indexable()` is the check;
`finished_at.SQL_NOW_ISO_UTC` is the SQL that mints it. Keep the SQL and the Python in lockstep.

`finished_at.parse()` still reads the legacy epoch shape — tolerance on READ, one producer on
WRITE — because a snapshot restored from an older dump can still carry one, and dropping real
completed work from a report is worse than parsing a shape we no longer write.

Migration 0014 fixed the rows that existed: epoch → ISO (a pure reformat; the instant is
unchanged, nothing is re-released), stale timestamps dropped from non-finished tickets, and
`finished_at = created_at` backfilled for tickets finished before stamping existed — so
`build_state = 'finished'` now implies a non-null, indexable `finished_at` with no exceptions.

## Known hole, deliberate

A `finished` transition through `patch_meta` is not lease-checked, so a non-holder can mark a
ticket done. That is load-bearing: `release()` deliberately HONORS a completion whose lease was
taken over mid-ticket, because refusing it caused an unbounded silent rebuild loop (a worker's
finished work discarded, the ticket re-handed out, the next finish racing the same way). The
completion is still dated by the server, so it is auditable for what it is.
