---
description: Drive this repo's "af-build-remote-jobs" Praxis project to done — the remote box-service job-dispatch feature. Thin alias for /af-build af-build-remote-jobs.
argument-hint: [optional extra instructions for this run, e.g. scope or a note for the build session]
---

The user invoked `/af-build-remote-jobs`. This is a **project-local alias**, not a
general agent-factory skill — it exists only in this repo's `.claude/commands/`
because `af-build-remote-jobs` is this repo's own Praxis project name (a ticket
set under org `praxis`), not a reusable concept the shared `agent_factory` plugin
should know about.

It is exactly equivalent to running:

    /af-build af-build-remote-jobs

Run the **`agent-factory:af-build`** skill now with the project argument fixed to
`af-build-remote-jobs`. Read that skill's own instructions in full and follow them
verbatim (FIND → CLAIM → RESOLVE → BUILD → VERIFY → FINISH, the incomplete-set
loop, the WORK-review panel at the end) — this file only supplies the project
name so the user does not have to type it.

If the user passed extra arguments below, fold them into the af-build invocation
as additional scope/context (e.g. "backend-only", a note about which tickets to
prioritize, or where to run — this repo currently drives builds from an EC2
devbox at 52.22.249.49 via tmux, not locally):

$ARGUMENTS
