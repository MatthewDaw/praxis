---
title: Observation signal domains for remote job control
category: convention
module: box-service
component: observability
tags: [job-model, observability, security, box-service]
applies_when: a signal is used to determine or influence a job's live/attention/terminal state
---

# Observation signal domains

Converted from requirement R70 (audit B1 security lens over R20/R22/R24/R25):
*"each observation signal is classified in-domain or out-of-domain, no signal
originates from a voluntary write by the session's model, and given a session
that suppresses or forges every in-domain signal, the attention-needed
determination is still reached from out-of-domain signals alone."*

Every signal the box service or af-build local runs derive a job's
running / awaiting-human / stalled / terminal state from is classified into
exactly one domain:

## Out-of-domain (trustworthy)

Signals the build session cannot influence by choosing what to write. These
alone must be sufficient to reach an attention-needed determination even if
every in-domain signal below is suppressed or forged.

- **External process/session liveness** — `claude agents --json` polled by
  the box service (R21); the session cannot make itself appear alive or dead
  to this poll by writing anything.
- **Praxis ticket state** — `build_state`, `claim_owner`,
  `claim_heartbeat_at`, `run_owner`/`run_at` read live from Praxis
  (`docs/factory-state-contract.md`). A session can stop heartbeating, but it
  cannot forge a *live* heartbeat for longer than the lease TTL, and it
  cannot flip another session's `build_state` to `finished` without actually
  covering + passing that ticket's validations.
- **Elapsed wall-clock time since the last confirmed activity signal** — a
  silence threshold measured externally, not self-reported.

## In-domain (advisory, forgeable)

Signals fired by hooks running *inside* the build session. They are useful
color (a last-activity timestamp, an explicit "blocked on a question" event)
but are a voluntary write by the same model whose cooperation cannot be
assumed, so no control or terminal-state decision may rest on one alone.

- **Harness-fired activity timestamp** (R22) — updated by an in-session hook
  on each turn; advisory input to "is this job making progress", never the
  sole signal a stall determination rests on.
- **Harness-fired "blocked on a question" event** (R23) — a first-class
  af-build behavior emitted when the agent produces text it needs an answer
  to, distinct from a permission prompt (the box service's allowlist
  permission mode means no permission prompt can occur at all). Advisory: a
  session that never fires it (crashed, or a suppressed/forged hook) still
  surfaces as attention-needed once the out-of-domain silence threshold or
  the external liveness poll say so.
- **Any other hook-emitted event** used only to enrich the stored activity
  tail (R25) is in-domain by construction — it is text the model chose to
  emit.

## The invariant this enforces

No code may take a **terminal-state** decision (`completed` vs `failed`,
`running` vs `awaiting-human`) from an in-domain signal in isolation. An
in-domain signal may *shorten* the time to surfacing an already-reachable
out-of-domain determination (e.g. an explicit "blocked on a question" event
short-circuits waiting for the silence threshold), but the same
determination must still be reachable — eventually — from out-of-domain
signals alone, per R70's acceptance condition.
