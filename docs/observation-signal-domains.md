---
title: Observation signal domains
category: convention
module: box-service
component: observability
tags: [observability, hooks, job-model, control]
applies_when: a control or terminal-state decision about a remote job is being made from an observation signal
---

# Observation signal domains

Every signal the box service uses to observe or decide the state of a remote job
declares which of two domains it belongs to. A control or terminal-state decision
never rests on a single **in-domain** signal alone.

## In-domain (hook-fired, forgeable by the session)

Signals emitted by hooks running *inside* the build session — activity/heartbeat
events, a "blocked on a question" event, and any other harness-fired signal. These
are cooperative: a compromised, hung, or merely buggy session can fail to fire
them, or fire them falsely. They are **advisory** — useful for freshness and UX
(sorting the job list, showing a "waiting on you" banner) — but never the sole
basis for a terminal or control decision.

## Out-of-domain (external, trustworthy)

Signals observed independently of the build session's cooperation:

- the external session poll — session existence/state as reported by the agent
  runtime's own process supervisor, not the session itself;
- process/session liveness as seen from outside the session;
- Praxis ticket state (`build_state`, claim leases) — read directly from the
  Praxis graph, which the session can influence only through the same claim/patch
  API every other actor uses.

A job's terminal state (a state outside `OPEN_JOB_STATES`, see
`knowledge/serve/box_service_models.py::Job.is_open`) and any restart-reconciliation,
resume/reap, or group-integration decision are decided from out-of-domain signals
only. In-domain signals may corroborate (e.g. explain *why* a job is in
`awaiting-human`) but never gate a state transition by themselves.
