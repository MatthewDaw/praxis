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

Signals emitted by hooks running *inside* the build session — last-activity
timestamps (R22), the "blocked on a question" event (R23), and any other
harness-fired event. These are cooperative: a compromised, hung, or merely buggy
session can fail to fire them, or (in principle) fire them falsely. They are
**advisory** — useful for freshness and UX (e.g. sorting the job list, showing a
"waiting on you" banner) — but never the sole basis for a terminal or control
decision.

## Out-of-domain (external, trustworthy)

Signals observed independently of the build session's cooperation:

- the external session poll (`claude agents --json`, R21) — session existence and
  state as reported by the Claude Code background daemon, not the session itself;
- process/session liveness as seen from outside the session;
- Praxis ticket state (`build_state`, claim leases) — read directly from the
  Praxis graph, which the session can influence only through the same claim/patch
  API every other actor uses.

A job's terminal state (R24) and any restart-reconciliation or resume/reap
decision (R30, R43) are decided from out-of-domain signals only. In-domain
signals may corroborate (e.g. explain *why* a job is in `awaiting-human`) but
never gate a state transition by themselves.
