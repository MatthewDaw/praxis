---
title: Observation signal domains
category: convention
module: box-service
component: observability
tags: [observability, hooks, job-model, control]
applies_when: a control or terminal-state decision about a job (remote or the local-venue derived projection) is being made from an observation signal
---

# Observation signal domains

Every signal used to observe or decide the state of a job -- remote (a
box-service job row) or local (a derived projection, R45) -- declares which
of two domains it belongs to. A control or terminal-state decision never
rests on a single **in-domain** signal alone.

## In-domain (hook-fired, forgeable by the session)

Signals emitted by hooks running *inside* the build session -- activity /
heartbeat events, a "blocked on a question" event, and any other
harness-fired signal. These are cooperative: a compromised, hung, or merely
buggy session can fail to fire them, or fire them falsely. They are
**advisory** -- useful for freshness and UX (sorting the job list, showing a
"waiting on you" banner) -- but never the sole basis for a terminal or
control decision.

## Out-of-domain (external, trustworthy)

Signals observed independently of the build session's cooperation:

- the external session poll -- session existence/state as reported by the
  agent runtime's own process supervisor, not the session itself;
- process/session liveness as seen from outside the session;
- Praxis ticket state (`build_state`, claim leases, the whole-set `run_owner`
  / `run_at` marker) -- read directly from the Praxis graph, which the
  session can influence only through the same claim/patch API every other
  actor uses.

A job's terminal state and any restart-reconciliation, resume/reap, or
local-derived-job (R45/R46) decision is decided from out-of-domain signals
only. `knowledge/serve/local_derived_job.py`'s TTL-staleness check over
`run_owner`/`run_at`/`build_state` is one such out-of-domain decision: it
never depends on any hook-fired signal from inside the killed session. In-
domain signals may corroborate (e.g. explain *why* a job is in
`awaiting-human`) but never gate a state transition by themselves.
