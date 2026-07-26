---
title: Observation signal domains
category: convention
module: box-service
component: job-model
tags: [observability, hooks, job-model, control]
applies_when: a terminal-state or control decision for a remote job is about to be taken from an observation signal
---

# Observation signal domains

Every signal the box service observes about a running job is classified into
exactly one of two domains. A terminal-state or control decision (marking a
job `completed`/`failed`/`needs-attention`, reaping a session, taking a
control lease) is **never** taken on an in-domain signal alone — it must be
corroborated by an out-of-domain signal.

## In-domain (hook-fired, forgeable by the session)

Signals emitted by a Claude Code hook running *inside* the session being
observed — a `Stop`/`SubagentStop`/`PreCompact` hook payload, a
self-reported "I'm done" message, or any other artifact the session itself
produces. These are **advisory only**: a compromised, stuck, or merely
mistaken session can emit (or fail to emit) any of them, so they can suggest
a state transition but can never be the sole basis for one.

## Out-of-domain (trustworthy)

Signals the box service (or Praxis) observes independently of the session's
cooperation:

- an external session poll (`claude agents --json`) — process existence and
  liveness as reported by the OS/CLI, not by the session;
- direct process liveness checks (PID / exit code);
- Praxis ticket state (`build_state`, `all_validations_passed`, lease
  heartbeats) — read from the graph, not asserted by the session.

These are the only signals restart reconciliation
(`knowledge/serve/box_service_reconcile.py`), failure classification
(`knowledge/serve/box_service_failures.py`), and job authorization
(`knowledge/serve/box_service_authz.py`) key their terminal/control decisions
on. A hook-fired signal may enrich a report or a human-facing message, but it
never gates a transition by itself.
