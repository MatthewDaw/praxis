---
name: af-learn
description: >
  The human channel of the failure-learning loop: file a free-text complaint (or a bulk batch of
  them) from ANY repo/session into a Praxis project's shared knowledge — a lesson always lands,
  plus an active check when one can be drafted, proof-attempted, and bound narrowly to the named
  project, with matching tickets offered for regression in the same motion. Lenient throughout: an
  unproven check still gates immediately (its unproven status is loudly flagged, not silently
  hidden) and auto-upgrades to proven on its first real catch. Use when the owner says "remember
  this", "never let this happen again", "add a check for X", or reports a bug/regression they want
  enforced going forward — in any repo, not just this one. Refuses (writes nothing) when the
  target project cannot be named.
---

## What this is

The lenient, human-directed sibling of the merger's machine-strict auto-ingestion (FL2/FL5–FL7).
Both funnel through the SAME ingestion API (`agent_factory.ingestion_api.ingest`); this skill
wraps it on the human channel via `agent_factory.af_learn.learn` / `af_learn.learn_bulk`
(`channel="human"`), which is lenient per KD5/DF4: a check the owner asks for is authored and
inserted — gating immediately — without oversight, even when its fail-then-pass proof came back
`unproven`. The lesson always lands regardless (R2): knowledge is never lost to a drafting
failure.

**Do not reinvent any of this.** Drafting/proof/binding/regression logic lives in
`agent_factory/src/agent_factory/ingestion_api.py`; this skill's own code surface is only project
resolution (`agent_factory/src/agent_factory/af_learn.py`). If something about proof status,
enforcement state, or binding scope looks wrong, the bug is in `ingestion_api`, not here.

## The one hard rule — name the project, or refuse

`/af-learn` is callable from ANY repo, so there is no ambient "current project" the way af-build
has one inside a single project's own worktree. Before doing anything else:

1. If the human named the project explicitly in the request ("...in the `checkout-flow` project"),
   use that name verbatim.
2. Else, check whether `FACTORY_PROJECT` is set in the environment (the same seam
   `hooks._super_run_identity` already uses) — if so, confirm it with the human in one line before
   proceeding ("filing this against `<project>` — correct?") rather than silently assuming it's
   right for a complaint that arrived from an unrelated repo.
3. Else, **ASK** which project this belongs to. Do not guess, and do not fall back to whatever
   project this session happens to be sitting in — a repo can host code for a project with a
   completely different Praxis space name.

If the human cannot or will not name a resolvable project, **refuse and stop** — call neither
`learn` nor `learn_bulk`. Nothing is written (E9): no lesson, no check, no ticket regression, no
guess at a cross-org target.

This is enforced in code too, as a backstop: `af_learn.resolve_target_project` raises
`UnresolvableProjectSpace` before any Praxis call when it is given no explicit project and no
`FACTORY_PROJECT` env var — a defense against a caller that skips the confirmation step above, not
a substitute for asking.

## Single-complaint mode

Once the project is confirmed, draft the lesson + (optionally) a check from the complaint, then
call:

```python
from agent_factory import af_learn

result = af_learn.learn(
    "<the lesson text — what happened and why it must not recur>",
    project="<bare project name>",
    source="<where this came from: a URL, a file path, 'Matt via af-learn'>",
    drafted_run="<a concrete, narrow executable check — omit if none is provable>",
    # OR drafted_rubric={...} for a graded/subjective check instead of a binary run command
    ticket_ids=["<ids of any live tickets this complaint should regress>"],
    surfaces=["<surface ids this observably affects, if any>"],
    drafting_transcript="<free-form reasoning trail — secret-scanned before storage>",
)
```

Returns `{"lesson_id", "check_id", "wave_id", "proof_status", "class"}`. `check_id` is `None` when
no `drafted_run`/`drafted_rubric` was supplied — that's fine, the lesson alone still landed (R2).
`proof_status` is `"proven"` or `"unproven"` — an unproven check still gates (DF4); say so plainly
in your reply so the human isn't surprised later ("inserted as an active check; proof against a
live artifact was inconclusive, so it's flagged unproven until it catches something for real").

**Drafting guidance** (this is the one place a human judgment call is needed — the API does not
draft for you):
- `drafted_run` must be a genuinely narrow, executable command that would have caught the
  complained-about failure — not a broad "run the whole suite" gate. `ingestion_api._validate_run_body`
  is exempt from the machine-channel allowlist for human-authored bodies, but a vague or unfalsifiable
  command still helps no one.
- Prefer `drafted_rubric` (a graded check) only when the complaint is genuinely subjective/judgment-based
  and no binary command could observe it.
- If you cannot draft anything provable, call `learn` with no `drafted_run`/`drafted_rubric` at all —
  the lesson still lands, and a future recurrence can attach a check then.

**Offering ticket regression.** If the complaint clearly maps to one or more LIVE tickets in the
named project (the human references one, or the complaint plainly describes a defect in ongoing
work), pass their ids as `ticket_ids` — this regresses them in the same motion and mandatorily
pins the new check at their next claim (R11). If you are not sure which tickets are affected,
leave `ticket_ids` empty rather than guessing — the check still lands, bound to the observed
surface (R12's zero-match fallback), and a human can regress specific tickets later via
`ingestion_api.regress`.

## Bulk mode

For a batch of complaints (the human dumps a list, or points at a doc/thread with several distinct
issues), build one entry dict per complaint — same shape as `learn`'s keyword arguments, with the
lesson text under `"complaint_text"` — and call:

```python
from agent_factory import af_learn

results = af_learn.learn_bulk(
    [
        {"complaint_text": "...", "drafted_run": "...", "surfaces": [...]},
        {"complaint_text": "...", "ticket_ids": [...]},
        ...
    ],
    project="<bare project name>",
)
```

**Every entry is inserted without per-check oversight (R9/E10)** — there is no confirmation
prompt between entries, by design (KD6: the whole lifecycle runs without a human review queue).
The project is resolved ONCE, before the first entry is written, so an unresolvable project
refuses the WHOLE batch up front rather than landing half of it. Report the full result list back
to the human afterward (lesson/check ids, proof statuses) so nothing lands invisibly.

**If a bulk insert turns out to have been a mistake** (a check that immediately blocks tickets it
shouldn't have): use the safety nets, never a review queue —
`ingestion_api.suspend(check_id, project, reason)` for the automatic false-positive path, or
`ingestion_api.kill_switch(check_id, project, reason)` for an immediate manual disable. Both are
recorded with a reason and raise a pending-attention flag (R24) so the disablement itself is never
silent.

## What you must NOT do here

- Do not draft or insert checks directly against `ingestion_api` from this skill — always go
  through `af_learn.learn` / `af_learn.learn_bulk` so project resolution (and its refusal path) is
  never bypassed.
- Do not fall back to a "current repo's project" guess when the human hasn't named one — ask, or
  refuse.
- Do not gate a bulk batch on per-entry human sign-off — that reintroduces the review queue KD6
  explicitly rejected. Use suspend/kill-switch after the fact if something needs walking back.
- Do not fabricate a `drafted_run` just to have SOMETHING gating — an unfalsifiable or
  overly-broad check is worse than a lesson landing alone.
