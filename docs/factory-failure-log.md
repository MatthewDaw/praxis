# Factory failure log

*A running record of defects found in the factory itself — the loop, the gates, the state contract —
as distinct from defects in the projects it builds. Kept so each one is fixed at the source rather than
worked around per-project, and so the recurring shapes become visible.*

**How to use this.** Add an entry when a factory defect is found, whether or not it is fixed. Record the
*evidence*, not the impression: the log line, the grep, the measured cost. An entry with no reproduction
is a hunch, and hunches have twice sent us at the wrong cause here (§F2, §F6).

---

## The recurring shapes

Six defects in, three patterns account for nearly all of them. Check these first when something is wrong.

**S1 — A guarantee stated in prose with no enforcement point.** The docstring or prompt says a thing is
true; nothing asserts it. Every instance was caught only when it failed in production.
*Instances: F1, F3, F4.*

**S2 — A loud bug with a silent twin.** The same defect appears twice, once where it crashes and once
wrapped in `|| true` / `2>/dev/null`. The loud copy halts a run; the quiet copy **forges** one. Always
grep for siblings of any bug you fix.
*Instance: F1.*

**S3 — A fix that cannot reach its own residue.** The fix is correct going forward but the damaged
records are, by the fix's own predicate, unreachable — so they persist as landmines.
*Instances: F4, and see F4's sweep.*

---

## F1 — `NameError` in conflict resolution, and its silent twin

**Found:** 2026-08-05, sports_analysis round #3 (the first round that ever hit a merge conflict).
**Fixed:** `a659b0d`.

Two inline-python blocks named their project variable `proj` and called
`_praxis.regress_requirements(p, ...)`.

- **Loud copy** (conflict-resolution enforcement sweep): crashed with `NameError` immediately after
  force-landing a branch. Under `set -euo pipefail` this killed the whole loop, and the exit trap's
  purge/reap made the log tail read like a normal round end. It also **ate the regression it was
  recording**, leaving R2 marked `finished` while its conflicting hunks had just been overwritten by the
  integration side.
- **Silent copy** (post-merge verifier's regression writer, one screen below): identical bug, wrapped in
  `2>/dev/null || echo 0`. It would never crash — it would silently record **zero regressions forever**,
  letting every integration-failed ticket keep reading `finished`.

**Cost:** one halted run, one corrupted ticket, and an unknown number of rounds where the quiet copy may
already have dropped regressions.

**Lesson (S2):** the loud bug is the lucky one. When you fix a defect, grep the file for the same shape.

---

## F2 — Verdict fields allowed to contradict each other

**Found:** 2026-08-06, taolu round #1. **Fixed:** `b67d5ea`.

Post-merge verification returns `verdict`, `gates_green`, `regressed`. The loop **printed them without
checking they agree**. Observed live:

```
verdict=pass gates_green=False regressed=0
```

That asserts simultaneously that the merged tree's repo-wide gates were RED and that every ticket
survived and nothing needs rebuilding. The round counted as a pass.

The verify prompt already forbade this in words — *"if you genuinely cannot attribute it, regress the
whole batch rather than passing a red tree"* — but a prompt is not an enforcement point (S1). This stage
exists **because** self-reported verdicts leak self-judgement back into a loop whose whole design is that
nothing grades its own work, so the one field combination meaning "I checked and it was bad, and also
everything is fine" needed a gate.

**Fix shape, deliberately narrow:** an incoherent verdict is downgraded to **UNVERIFIED**, not converted
to a failure. `gates_green=false` is also what a verifier reports when a repo has no lint/typecheck
tooling configured, so auto-regressing on it would trade a false pass for a false failure and rebuild
healthy tickets forever. Tickets keep the state they earned; the round's green claim does not stand.

**Note the near-miss:** the first diagnosis assumed a red tree was being waved through. It was probably
the missing-tooling case. The fix is right either way, but the *reason* was nearly recorded wrong.

---

## F3 — `resolve_finding()` had a unit test and no production caller

**Found:** 2026-08-05 (sports R2), re-confirmed 2026-08-06 (taolu T1/T8/T10/T15). **Fixed:** `4cdc010`.

`open_finding()`'s docstring has always said a finding is answered *"when a later verification round
confirms the ticket survived (which stamps `resolved`)"*. Nothing stamped it. `resolve_finding()` existed,
was unit-tested, and had zero production callers (S1).

**The livelock it produced.** A ticket whose finding was fixed by a *sibling's* merge finishes its
rebuild with zero commits — correctly, there is nothing left to change — and the zero-commit guard
regresses it for exactly that. Repeat forever.

**Measured cost:** T8 and T1 rode it for **17 consecutive rounds**; T10, T20, sports R2 and farming R26
for 6–8 each. sports R2 had to be broken by hand mid-build.

**The guard itself is correct** and should not be weakened: it exists because a ticket once closed twice
with its file untouched while every pinned check stayed green. The bug was the missing other half of the
contract, not the guard.

---

## F4 — Stale findings the fix cannot reach *(open)*

**Found:** 2026-08-06, taolu. **Swept by hand; no code fix yet.**

F3's fix stamps `resolved` on any ticket **present in a round** that still reads `finished`. A ticket that
is already `finished` never appears in a round — so findings written *before* the fix are unreachable by
it (S3). Four taolu tickets (T1, T8, T10, T15) sat `finished` with open findings, each one a landmine
that would re-trigger the zero-commit guard on any future regression.

Cleared by hand with a documented `resolved_by`. **The general fix is missing:** a one-shot sweep at loop
start, or in `--resolve-orphans`, that clears open findings on finished tickets whose work is on `HEAD`.

**Check for this in any project that ran before `4cdc010`:**

```python
[m.get("requirement_id") for f in facts
 if (m := f.get("meta") or {}).get("build_state") == "finished"
 and ts.open_finding(m) is not None]
```

---

## F5 — Blocked tickets with no recorded reason *(open)*

**Found:** 2026-08-06, taolu. **No fix.**

Three tickets (T16, T19, T22) sit in `build_state="blocked"` with `audit_disposition = None`. A blocked
ticket is never claimed, so these will never build — and nothing on the ticket says why, who blocked it,
or what would unblock it. From outside, a blocked ticket and a forgotten one are indistinguishable.

**Wanted:** `blocked` should require a reason at write time, the way a regression requires a failure
report. A state that stops work forever and explains nothing is worse than a failure.

---

## F6 — Straggler exit 7 over already-merged residue

**Found:** 2026-08-05, sports_analysis drain; recurred on taolu. **Working as designed; reporting is the
problem.**

A run exits `7` (STRAGGLERS) when worktree *directories* remain, even when every corresponding branch is
already an ancestor of `HEAD`. The run reports failure over work that fully landed.

The invariant is right and must not be loosened — **it never deletes anything to reach green**, which is
exactly the property you want. But `exit 7` plus "the run left work behind" reads as data loss when the
truth is stale directories. Verified both times with:

```bash
git merge-base --is-ancestor <branch> HEAD   # merged -> residue only
git branch -d <branch>                       # refuses anything unmerged; safe verb
```

**Wanted:** distinguish *unmerged branch* (real orphan, exit 7) from *stale directory whose branch is
merged* (residue, clean up and say so).

---

## F7 — A billing halt is invisible outside the log

**Found:** 2026-08-06, taolu (02:41 and 07:58). **No fix.**

`BILLING FAILURE (out of credits/quota) — halting the whole loop` is the correct behaviour: exit 3, no
retry, wait for a human. But nothing surfaces it. The second halt cost **~8 hours** before anyone looked
at the log, and the only symptom from outside is a loop that stopped making progress.

**Wanted:** any halt requiring human action should leave a durable, queryable marker — a Praxis episode
at minimum — so "why is nothing happening" is answerable without reading a log on a box.

---

## Cross-cutting notes

**Editing a running loop's script is unsafe.** `bash` reads scripts incrementally, so editing
`af-ticket-loop.sh` while instances are executing it can make them run garbage. Fixes land at the next
relaunch. Push freely; pull onto the box deliberately.

**`AF_WATCH=1` picks up work authored after launch — including work not yet blessed.** On 2026-08-06 a
watching loop grabbed three freshly-minted tickets **before** the intake was blessed and **before** four
build-validation checks existed, so all three pinned check sets without them. RESOLVE happens at claim
time; a check authored after dispatch is too late for that round. Either stop the loop while intaking, or
regress the affected tickets afterward.

**Two processes per loop is normal.** `pgrep -fc` returns 2 — the `bash -c` launcher wrapper plus the
script it spawned as its child. Not duplicate loops; check `ppid` before concluding otherwise.
