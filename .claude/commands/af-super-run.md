---
description: Drive this repo's "af-super-run" Praxis project to done — factory hardening tickets. Thin alias for /af-build af-super-run.
argument-hint: [optional extra instructions for this run, e.g. scope or a note for the build session]
---

The user invoked `/af-super-run`. This is a **project-local alias**, not a general
agent-factory skill — it exists only in this repo's `.claude/commands/` because
`af-super-run` is this repo's own Praxis project name (a ticket set under org
`praxis`), not a reusable concept the shared `agent_factory` plugin should know
about.

It is exactly equivalent to running:

    /af-build af-super-run

Run the **`agent-factory:af-build`** skill now with the project argument fixed to
`af-super-run`. Read that skill's own instructions in full and follow them
verbatim (FIND → CLAIM → RESOLVE → BUILD → VERIFY → FINISH, the incomplete-set
loop, the WORK-review panel at the end) — this file only supplies the project
name so the user does not have to type it.

If the user passed extra arguments below, fold them into the af-build invocation
as additional scope/context:

$ARGUMENTS
