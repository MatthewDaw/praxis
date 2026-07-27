---
title: Observation signal domains
category: convention
module: box-service
component: observability
tags: [observability, hooks, job-model, control]
applies_when: a control or terminal-state decision about a job (remote or the local-venue derived projection) is being made from an observation signal
---

# Observation signal domains

Every signal used to observe or decide the state of a job -- remote (a box-service job row) or
local (a derived projection, R45) -- declares which of two domains it belongs to. A terminal-state
or control decision (marking a job `completed`/`failed`/`needs-attention`, reaping a session,
taking a control lease, deciding group integration) is **never** taken on a single **in-domain**
signal alone -- it must be corroborated by an out-of-domain signal.

## In-domain (hook-fired, forgeable by the session)

Signals emitted by hooks running *inside* the build session — activity/heartbeat events, a
"blocked on a question" event, a `Stop`/`SubagentStop`/`PreCompact` hook payload, a self-reported
"I'm done" message, or any other harness-fired artifact the session itself produces. These are
cooperative: a compromised, hung, or merely buggy session can fail to fire them, or fire them
falsely. They are **advisory only** — useful for freshness and UX (sorting the job list, showing a
"waiting on you" banner, explaining *why* a job is in `awaiting-human`) — but never the sole basis
for a terminal or control decision.

## Out-of-domain (external, trustworthy)

Signals observed independently of the build session's cooperation:

- the external session poll (`claude agents --json`) — session existence/state as reported by the
  agent runtime's own process supervisor, not the session itself;
- direct process/session liveness checks (PID / exit code) as seen from outside the session;
- Praxis ticket state (`build_state`, `all_validations_passed`, claim leases, the whole-set
  `run_owner` / `run_at` marker) — read directly from the Praxis graph, which the session can
  influence only through the same claim/patch API every other actor uses.

These are the only signals restart reconciliation (`knowledge/serve/box_service_reconcile.py`),
failure classification (`knowledge/serve/box_service_failures.py`), job authorization
(`knowledge/serve/job_authz.py`), group-integration decisions (`knowledge/serve/box_service_groups.py`),
and local-derived-job (R45/R46) decisions key their terminal/control decisions on. A job's terminal
state (a state outside `OPEN_JOB_STATES`, see `knowledge/serve/box_service_models.py::Job.is_open`)
and any restart-reconciliation, resume/reap, authorization, or group-integration decision are
decided from out-of-domain signals only. `knowledge/serve/local_derived_job.py`'s TTL-staleness
check over `run_owner`/`run_at`/`build_state` is one such out-of-domain decision: it never depends
on any hook-fired signal from inside the killed session. In-domain signals may corroborate (e.g.
explain *why* a job is in `awaiting-human`) but never gate a transition by themselves.
