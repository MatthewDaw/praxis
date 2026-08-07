---
name: af-rapid-queue
description: >
  Rapid drive-by intake: the owner is debugging a live app, spots a fix or refactor, and fires it at
  whatever session is open — mid-task, without waiting. This skill CAPTURES that request and nothing
  else: it spools the text locally (so a crash cannot lose it), files it into Praxis as an ordinary
  incomplete requirement TICKET in `prd-<project>` (so af-build drains it and the build-completeness
  Stop gate refuses to let a run end while it is outstanding), then hands control straight back to
  whatever the session was doing. It NEVER investigates, reads the codebase, debates, or starts
  building the request — derailing the in-flight task is the exact failure it exists to prevent. Use
  for a one-line "also fix X" / "refactor Y" / "Z is broken" thrown in mid-flight; use af-intake-plan
  for real planning, and af-build to actually drain the queue.
---

## What this is

Three guarantees, and they are the whole spec:

1. **Never derailed.** The in-flight task survives the interruption. This skill's entire budget is
   one spool write, one Praxis write, one line of output. It does not look at code.
2. **Never lost.** The request is durable before anything that can fail is attempted — spool first,
   Praxis second. An un-filed entry is re-offered at every Stop boundary until it becomes a ticket.
3. **Truly finished.** A captured request becomes an *ordinary ticket*, so it inherits the existing
   completion machinery unchanged: `incomplete_requirements` lists it, af-build claims/builds/verifies
   it, and `hooks/build_completeness_gate.py` blocks a stop while it is unfinished. There is no
   second queue, no lighter "quick fix" path, and no way for a rapid ticket to be waved through.

Nothing about the drain is new. The only new machinery is the write-ahead spool in
`hooks/rapid_queue.py` and the advisory Stop hook `hooks/rapid_queue_relay.py`.

## THE CARDINAL RULE — capture, do not act

**You are a clerk here, not a builder.** When this skill is invoked, the session is almost always in
the middle of something else. The request being filed is *not* the task at hand.

Forbidden while filing, without exception:

- reading, grepping, or opening any source file to "understand" the request
- diagnosing, reproducing, or root-causing it
- fixing it, or starting to
- asking the owner clarifying questions (they are mid-debug and did not stop for a conversation)
- surveying the existing queue, or reordering it

If the request is too vague to file, file it **verbatim as the owner said it** and let af-build's
own underspecification routing surface the gap later. A vague ticket in the queue beats a clarifying
question that costs the owner their train of thought.

## The loop

### 1. SPOOL FIRST — before anything that can fail

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/rapid_queue.py" capture "<the request, verbatim>"
```

Prints `{"captured": {"qid": ...}, "spool": ..., "pending_count": N}`. This is a local append that
does not touch the network, so from this moment the request cannot be lost — only delayed. Keep the
`qid`; step 3 needs it.

Do this even when you are confident Praxis is up. The ordering *is* the durability guarantee.

### 2. FILE it as a Praxis ticket

One write, into the plan snapshot the build reads (same shape af-intake-plan's C0 amend uses — a
rapid ticket is not a special species of ticket):

```
praxis_add_insight(
  insight  = "<the requirement — ONE semicolon-joined sentence>",
  source   = "prd-<project>",
  category = "requirement",
  meta     = { "build_state": "incomplete",
               "acceptance":  "<binary observable condition — REQUIRED, see below>",
               "verify":      "automated",            # REQUIRED — "automated" | "manual"
               "tags":        ["rapid-queue", "<class-tag>"],
               "scope":       "mvp",
               "intake_lane": "rapid-queue",
               "surfaces":    ["<screen-id>", ...] },  # only if it renders a known surface
  space    = "<project>",          # the bare project name
  snapshot = "prd-<project>",      # the plan snapshot itself, NOT working memory
)
```

**`acceptance` and `verify` are not optional, and getting this wrong is the one way to silently lose
a request.** `_ticket_state.start_ticket` runs the structural resumability probe
(`agent_factory/src/agent_factory/resumability.py`) *before* it leases: a ticket with neither an
acceptance condition nor a resolved check, or with no `verify` mode, is **never claimed** — it is
stamped `meta.under_specified` and parked for intake. A one-line drive-by filed without these sits in
the queue forever looking queued. Both are derivable from the request itself with zero investigation:

- **acceptance** — restate the request as the observable condition that makes it satisfied. "The
  header collapses on mobile" → *"the header renders at one line with no overlap at 375px width"*.
- **verify** — `"automated"` by default (a command or test can observe it). Use `"manual"` ONLY when
  no command could ever observe it, and know the cost: a manual requirement needs an externally
  attested pass (`PRAXIS_ATTESTED_CALLER`), which an unattended run cannot produce, so it will wait
  for the owner. Prefer `"automated"`.

Then bind a surface if it clearly renders one, so surface-scoped checks resolve onto it:

```
praxis_bind_surface(<requirement_id>, <screen_id>, <project>, space="<project>", snapshot="prd-<project>")
```

**Never author a check list on the ticket.** Which validations apply is af-build's fresh RESOLVE query
(tag ∪ `"*"` ∪ surface) — the `"*"` universal gates (typecheck/lint/test/build) land on a rapid ticket
automatically, which is precisely why a drive-by cannot dodge the bar the rest of the plan clears.

### 3. RETIRE the spool entry

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/rapid_queue.py" filed <qid> <ticket-id>
```

Only a real ticket id retires an entry — that is what keeps "handled" honest. Skip this and the Stop
hook will keep offering the request back, which is the correct failure direction.

### 4. RESUME — immediately, in one line

Report exactly one line and return to the interrupted task in the same turn:

> Queued as `<ticket-id>` ("<short restatement>"). Resuming <what you were doing>.

No summary of the queue, no "would you like me to work on it now", no plan. If the owner wants it
worked, they will run af-build.

## When Praxis is unreachable

Say so in one line, leave the entry queued, and resume. **Do not** debug Praxis, retry in a loop, or
work the request as consolation.

This is the one place the factory's usual "Praxis unreachable → fail closed, never buffer to a file"
rule reads differently, and the distinction is deliberate: that rule protects *build and validation
state* from being decided off a guess, and the spool holds none — no claim, no check, no pass, no
completion. It holds un-filed raw request text and nothing else, for as long as it takes a single
Praxis write to land. Failing closed here would mean discarding the owner's words, which is the
failure this skill was built to eliminate.

The pending entry is re-offered at every subsequent Stop boundary by `hooks/rapid_queue_relay.py`
(advice only — it never blocks), so the next session with a live Praxis files it.

## Invoked with no request

Treat it as "close out the queue": run `rapid_queue.py pending`, file each entry per steps 2–3, and
report the count. Still no building.

## How a queued request actually gets finished

Nothing here builds anything. The queue drains when af-build runs — it re-queries
`incomplete_requirements` live, so a rapid ticket filed *during* a build run is picked up by that same
run's next FIND, and one filed afterwards is picked up by the next run. The Stop gate is what makes
"truly finish everything" enforced rather than hoped for: while a run is active it blocks until every
claimable incomplete ticket in scope is finished, and a rapid ticket is claimable like any other.

So the owner's workflow is: fire requests all afternoon → run af-build → everything fired gets built,
verified against external signals, and reviewed.

## af-rapid-queue vs af-intake-plan

| Situation | Use |
|---|---|
| One-line "also fix X", mid-flight, no thought required | **af-rapid-queue** |
| A missing requirement that belongs in the plan's coverage story | af-intake-plan C0 amend |
| A wave of changes, or edits to existing requirements | af-intake-plan FULL INTAKE (re-baseline) |
| A rule that must hold for every ticket ("all auth tickets pass the login e2e") | ingestion_api.plan_time_author_check |

Same destination, different ceremony: C0 asks you to prove the addition isn't an edit and doesn't move
the coverage story. That deliberation is right for planning and wrong for a drive-by — so this lane
skips it and accepts the cost (a rapid ticket may turn out to be a near-duplicate; af-build's
reconciliation and the review panel catch that, and a duplicate is a cheaper failure than a lost
request).

## Never

- **Never investigate, fix, or plan the captured request in this skill.** Capture and resume.
- **Never file into working memory** — always pass `space="<project>"` and `snapshot="prd-<project>"`,
  or the build cannot see it.
- **Never file without `acceptance` + `verify`** — the ticket will be parked `under_specified` and
  never claimed, which is a silent loss wearing a queued badge.
- **Never author `pinned_checks`, a check list, or a claim lease here.** Resolution and claiming
  belong to af-build.
- **Never retire a spool entry without a real ticket id**, and never hand-edit the spool file to make
  a nag go away.
- **Never let the spool become the queue.** It is a write-ahead log measured in seconds; Praxis is the
  queue. If entries are piling up there, Praxis is down and that is the thing to report.
- **Never batch-drop requests you judge unimportant.** Triage is the owner's, and af-build's, not the
  clerk's.
