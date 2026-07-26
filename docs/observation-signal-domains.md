---
title: Observation signal domains
category: convention
module: box-service
component: job-model
tags: [observability, hooks, job-model, control]
applies_when: a terminal-state or control decision for a remote job is about to be taken from an observation signal
---

# Observation signal domains

Every signal the box service uses to observe or decide the state of a remote job
declares which of two domains it belongs to. A terminal-state or control decision
(marking a job `completed`/`failed`/`needs-attention`, reaping a session, taking a
control lease, deciding group integration) is **never** taken on a single
**in-domain** signal alone — it must be corroborated by an out-of-domain signal.

## In-domain (hook-fired, forgeable by the session)

Signals emitted by hooks running *inside* the build session — activity/heartbeat
events, a "blocked on a question" event, a `Stop`/`SubagentStop`/`PreCompact` hook
payload, a self-reported "I'm done" message, or any other harness-fired artifact
the session itself produces. These are cooperative: a compromised, hung, or
merely buggy session can fail to fire them, or fire them falsely. They are
**advisory only** — useful for freshness and UX (sorting the job list, showing a
"waiting on you" banner, explaining *why* a job is in `awaiting-human`) — but
never the sole basis for a terminal or control decision.

## Out-of-domain (external, trustworthy)

Signals observed independently of the build session's cooperation:

- the external session poll (`claude agents --json`) — session existence/state
  as reported by the agent runtime's own process supervisor, not the session
  itself;
- direct process/session liveness checks (PID / exit code) as seen from outside
  the session;
- Praxis ticket state (`build_state`, `all_validations_passed`, claim leases) —
  read directly from the Praxis graph, which the session can influence only
  through the same claim/patch API every other actor uses.

These are the only signals restart reconciliation
(`knowledge/serve/box_service_reconcile.py`), failure classification
(`knowledge/serve/box_service_failures.py`), job authorization
(`knowledge/serve/box_service_authz.py`), and group-integration decisions
(`knowledge/serve/box_service_groups.py`) key their terminal/control decisions
on. A job's terminal state (a state outside `OPEN_JOB_STATES`, see
`knowledge/serve/box_service_models.py::Job.is_open`) and any restart-
reconciliation, resume/reap, authorization, or group-integration decision are
decided from out-of-domain signals only. In-domain signals may corroborate but
never gate a transition by themselves.
