---
title: Ticket writes are identity-keyed and bypass dedup/reconciliation — on_conflict does not guard them
date: 2026-07-30
category: conventions
module: knowledge/serve (app.py `_requirement_upsert`, `_check_upsert`, `_is_requirement_ticket`), knowledge/knowledge_graph/write_policy
problem_type: convention
component: data_integrity
severity: high
related_components: [write_policy, agent_factory, requirements, checks, contradiction_engine]
applies_when:
  - Touching the `/insights` write path for `category="requirement"` or `category="check"` facts
  - Tempted to "re-enable" dedup, merge, or the contradiction step on the ticket/check write path
  - Adding any predicate to the identity lookup inside `_requirement_upsert` / `_check_upsert`
  - Choosing `on_conflict` for any write that carries `meta.requirement_id` / `meta.check_id`
  - Reviewing a change to `default_write_policy()` or to the `Augmenter` step
  - Investigating two active facts that share one `meta.requirement_id`
  - Repairing a corrupted or duplicated ticket (delete-vs-reject, and in which order)
tags: [praxis, write-policy, dedup, augmenter, on-conflict, requirements, checks, data-corruption, agent-factory, delete-vs-reject, recovery]
---

# Ticket writes are identity-keyed and bypass dedup/reconciliation

A requirement-TICKET write (`category="requirement"` + `meta.build_state`, usually with
`meta.requirement_id`) is routed by `_is_requirement_ticket` to `_requirement_upsert` in
`knowledge/serve/app.py`, which runs against a **redact-only** graph (`policy=[Redactor()]`):
no Deduper, no Augmenter, no ClaimConflictDetector, no ConflictOverwriter. Its sibling
`_check_upsert` does the identical thing keyed on `meta.check_id`.

This is deliberate. Do not "helpfully" put dedup or reconciliation back on this path.

There are **two** ways to break it, and they produce the same residue — two active facts sharing
one `meta.requirement_id`. The first is letting a ticket reach the reconciled path at all. The
second, subtler one, is the identity lookup failing to find its own incumbent. Both are covered
below; both were live defects.

## Why a ticket is not a knowledge assertion

A ticket is a **build unit with an identity**, not a claim about the world to be reconciled
against the corpus. Two tickets with different `requirement_id`s are **different work** even
when their prose reads alike — "add rate limiting to the login route" and "add rate limiting
to the signup route" are near-neighbours in embedding space and are two separate jobs. Semantic
dedup has no way to know that; identity does.

The reconciliation the corpus needs already happened **upstream**: extraction reconciles at
intake time, and the contradiction net is the **AUDIT** over what landed, not the write path
itself. Making the write path also reconcile is not defence in depth — it is a second,
lossy, LLM-judged actor mutating facts nobody asked it to touch.

Hence the two rules:

* same `requirement_id` in this `(space, snapshot)` → **UPDATE in place** (a true restatement
  of that same ticket) — never a duplicate;
* new or distinct `requirement_id` → **ALWAYS a fresh distinct fact** — a new ticket can never
  mutate a different (or already-`finished`) ticket.

## Defect 2 — the identity lookup must key on the identity ALONE

Re-filing a ticket with an **already-existing** `meta.requirement_id` created a **second active
fact** instead of updating in place — the exact opposite of the contract above. Two causes, both
now fixed in `knowledge/serve/app.py`:

1. The write reached the server without `category="requirement"`, so `_is_requirement_ticket`
   rejected it and it never reached the identity-keyed path at all. *Fixed:* a ticket is now
   recognised by `meta.requirement_id` + `meta.build_state` even when `category` is missing or
   wrong, and `_requirement_upsert` stamps the category on.
2. **The identity lookup itself filtered on `category="requirement"`.** A ticket that landed via
   cause 1 has a NULL category, so the lookup could not see it, and the corrected re-write minted
   a fresh row instead of healing the first. *Fixed:* the lookup is now
   `graph.facts_by(state=None, meta_filter={"requirement_id": rid})` — matched on the key, with
   category NULL-or-`requirement` accepted, then healed on write.

Cause 2 is the one to internalise, because it is the trap a well-meaning maintainer walks into:

> **The identity lookup must key on the identity ALONE.** Every extra predicate you add to it
> (`category`, `state`, `source`, snapshot section, "only active ones") is a way for it to miss
> an incumbent — and a missed incumbent is not a no-op, it is an INSERT. Adding a filter turns
> the upsert into a duplicate-generator precisely in the recovery case it exists for, because a
> corrupted fact is exactly the fact whose secondary attributes are wrong.

The lookup also deliberately spans **all states** (`state=None`), for the same reason: a rejected
or proposed twin still owns that `requirement_id` and must be visible to the write that would
otherwise duplicate it.

## The identity key is the guard — `on_conflict` is not

`on_conflict` governs the **contradiction step**. It does nothing whatsoever to guard an
**additive merge**, and the safe-sounding value is the dangerous one:

| `on_conflict` | policy | contains `Augmenter`? |
|---|---|---|
| `"surface"` | `default_write_policy()` | **YES** — Mem0-style additive merge |
| `"auto_resolve"` | `[Redactor(), Deduper(), ConflictOverwriter(...)]` | no |

`surface` reads like the safe, human-in-the-loop option ("keep both facts, let a human
adjudicate"). It is the one that **enables additive merging**
(`knowledge/knowledge_graph/write_policy/write_step_variants/augmenter.py`, wired in
`postgres_vector_graph.default_write_policy`). That asymmetry is the whole bug. A caller
who passes `on_conflict="surface"` believing it protects their ticket is exactly the caller
who gets corrupted — which is why `_requirement_upsert` reports an unhonored `onConflict`
back in `note` instead of silently swallowing it.

## The concrete cost of getting this wrong

Live-reproduced in production. A ticket write that arrived **without** `category="requirement"`
failed the old `_is_requirement_ticket` test, fell through to the reconciled path, and:

```
request : a brand-new ticket, on_conflict="surface"
response: {"summary":"merged insight","action":"merged","id":"<an id the caller never wrote>",
           "onConflict":"auto_resolve","contradictionsSurfaced":0}
effect  : the new ticket was NEVER created; the other ticket's content was destroyed;
          three unrelated already-hardened tickets were flipped to state="rejected"
          (invisible to active queries, so the build silently never sees them);
          and the counter reported 0 while three facts were being rejected.
```

Residue to look for in the graph: **two active facts sharing one `meta.requirement_id`** in
the same snapshot. That is the corruption signature, and it is otherwise silent — the build
loop just stops seeing work it was given.

The fix was to stop trusting the `category` label alone (Defect 2, cause 1). Note that the
NULL-category facts this produced are also what made the identity lookup miss them afterwards —
the two defects compound: the first mislabels a ticket, the second then refuses to recognise it
during the repair.

## Repairing one: DELETE first, and delete BEFORE you rewrite

**Deletion is the default removal verb. `praxis_delete_fact` works in any state, needs no reject
step first, and cascades the fact's edges and claims away with it.** Reach for
`praxis_reject_fact` only in the narrow case where the row must be preserved for audit, or where
you specifically want the stale-dependent review propagation.

The reason is mechanical, not stylistic:

> **A rejected fact still holds its `meta.requirement_id`.** Rejecting instead of deleting is
> how a snapshot ends up with a stranded twin — a ghost that owns the identity, is invisible to
> active queries, and blocks or confuses the next identity-keyed write. This is the live state
> of `prd-sotos` right now.

Deletion genuinely frees the id; rejection does not.

**Ordering rule — remove the bad fact BEFORE writing the corrected one.** The instinct is the
reverse ("write the good one first so nothing is lost"), and that instinct permanently strands a
duplicate: you now have two facts on one `requirement_id`, and the repair write that was supposed
to heal became the twin. That is exactly how the `CHAT14` duplicate was created. Order is:

1. identify the bad fact (by `cid`, or address it by `requirement_id` + `space` + `snapshot`);
2. `praxis_delete_fact` it;
3. only then write the corrected ticket.

Two operational notes on `praxis_delete_fact`: addressing by `requirement_id` requires **both**
`space` and `snapshot` (that pair is the search scope), spans all states, and does not pin
`category` — for the same reason as Defect 2 above. If more than one fact carries that
`requirement_id` it **refuses and names every match with its state** rather than guessing, because
that duplicate *is* the corruption signature and choosing which twin dies is a human decision.
And a delete against a **blessed** `prd-<project>` snapshot is refused with a 400 by the
bless-state guard — re-arm the planning marker first (af-intake-plan Step 0d /
`POST /planning-marker`), same rule as `praxis_edit_fact`.

## The sibling precedent

`_check_upsert` on `meta.check_id` exists for the identical reason and had the identical bug:
semantic dedup silently merged two distinct checks whose prose read alike and **dropped the new
check's `run` command** — the command whose non-zero exit is the entire point of a check. A gate
that merged away is a gate that passes. Same disease, same cure: identity key, redact-only graph.

## The invariant a future change must preserve

> A plan-ticket (or check) write must **never mutate, merge into, or reject a fact the caller
> did not name.**

And its twin, from Defect 2:

> A ticket write for an existing `requirement_id` must **always** find and heal that one fact —
> the identity lookup keys on the identity alone.

Any change to `default_write_policy()`, to `Augmenter`, to the routing in `_is_requirement_ticket`,
or to the `facts_by` filter inside `_requirement_upsert` / `_check_upsert` is a change to these
invariants and must be argued against them. If you find yourself adding dedup "just for the obvious
duplicates", or narrowing the identity lookup "so it only matches real requirements", you are
reintroducing one of the two outages above.

## Related

- `knowledge/serve/app.py` — `_is_requirement_ticket`, `_requirement_upsert`, `_check_upsert`
- `knowledge/knowledge_graph/knowledge_graph_variants/postgres_vector_graph.py` — `default_write_policy()`
- `knowledge/knowledge_graph/write_policy/write_step_variants/augmenter.py` — the additive-merge step
- `knowledge/mcp/server.py` — `praxis_delete_fact` (identity addressing, duplicate refusal, bless guard)
- `agent_factory/docs/af-memory-policy.md` §2 (`on_conflict` guidance) and §1 (the ticket/check state model)
