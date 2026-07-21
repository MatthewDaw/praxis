# Proposal: a pre-run baseline health check for af-build

Status: proposal (design only — not implemented)
Date: 2026-07-21
Area: `agent_factory/skills/af-build/SKILL.md` (the build loop), `agent_factory/hooks/build_completeness_gate.py`

## The problem (from a real run)

Standing up `/af-build` on a fresh box, a run ground for ~13 hours before anyone realized the ~200
failing tests it kept trying to "fix" were **pre-existing on `main`** — stale tests unrelated to any
ticket. The factory has no notion of a *baseline*: it treats every red signal it sees as work to do.
So a repo that is already red on `main` turns af-build into a machine that chases failures it did not
cause and cannot close, because closing them was never in scope.

Today af-build's run start (SKILL.md §0, "OPEN THE RUN") does exactly one thing: resolve the scope to
its incomplete ticket ids and `stamp_run` the whole-set marker. It never runs the target repo's own
test/build gate to establish what "green" even means *before* the first ticket. The per-ticket VERIFY
step (§6) runs validations, but with no baseline it cannot tell "my change broke this" from "this was
already broken" — and the completeness gate
(`build_completeness_gate.py`) only reasons about Praxis ticket state, not the repo's test health.

## The proposal

Add a **BASELINE step (§0.5)** to the af-build loop, between OPEN THE RUN and the first FIND:

1. **Run the target repo's test/build gate ONCE at run start**, before claiming any ticket. Discover
   the command the same way a ticket's validations do — the `applies_to: ["*"]` build/test/lint/
   typecheck checks in the project's `building-validation` snapshot (SKILL.md "The lanes that build
   the contract"). Those universal checks ARE the repo gate; running them once up front is the
   baseline probe. If the snapshot declares none, fall back to a detected default (`pytest`, `npm
   test`, `just test`, …) and record that it was inferred.

2. **Classify the result into a baseline fingerprint**: the set of failing tests/checks observed on
   the repo *as checked out*, before any factory change. Persist it as a run-scoped fact in Praxis
   (a `category="baseline"` fact keyed to the run marker, so it lives next to the run's other dynamic
   state — no new JSON files, consistent with "all dynamic state lives in Praxis").

3. **Attribute every later failure against the baseline.** During per-ticket VERIFY, a validation
   failure whose signature is already in the baseline fingerprint is *pre-existing-on-main*, not
   *introduced-by-this-run*. Only introduced failures gate a ticket. Pre-existing ones are surfaced,
   never chased.

4. **Surface a red baseline as an explicit up-front DECISION, not silent churn.** If the baseline
   probe is red, af-build STOPS before the first ticket and presents the operator a clear choice:
   - **Proceed anyway** — accept the red baseline; the run will only gate on *newly introduced*
     failures (pre-existing reds are excluded from every ticket's contract for this run).
   - **Fix the baseline first** — the reds become their own tickets (via af-intake-plan amend) and
     are built like any other work before the real scope starts.
   - **Abort** — the repo is not in a fit state to build on; nothing is claimed.

   This is the 13-hour-saver: the red baseline becomes a 30-second decision at second 0 instead of a
   discovery made after a day of thrashing.

## Why this shape

- **It reuses the existing check machinery.** The `["*"]` wildcard validation lane already models
  "the repo-wide gate every ticket must pass." The baseline is just running that lane once, earlier,
  and remembering the answer — no new concept for authors to learn.
- **It stays fail-loud, never fail-open.** A red baseline halts for a decision; it does not silently
  lower the bar. "Proceed anyway" is an explicit, logged operator choice (mirroring how
  `FACTORY_GATE_DISABLED=1` is loud, never silent).
- **It is attribution, not suppression.** Pre-existing failures are still reported at run end
  ("baseline was red: N pre-existing failures, untouched"), so they are visible and can be scheduled
  as real work — they are just not allowed to masquerade as this run's tickets.
- **It composes with the parallel Workflow.** The baseline is computed once by the run opener and
  handed to every per-ticket worker as read-only context (the same way the run marker is), so N
  parallel workers share one baseline instead of each rediscovering it.

## Sketch of the loop change (SKILL.md)

```
0.   OPEN THE RUN        — resolve scope ids, stamp_run.
0.5  BASELINE (new)      — run the ["*"] repo gate once; fingerprint the failures; persist as a
                           run-scoped baseline fact. If red -> STOP and force the Proceed/Fix/Abort
                           decision before any FIND.
1..8 FIND -> FINISH      — unchanged, EXCEPT VERIFY (§6) attributes each failure against the baseline
                           fingerprint; only introduced failures gate a ticket.
```

## Open questions (for eng review before implementing)

- **Fingerprint granularity.** Test node id is precise but brittle to renames; a coarser signature
  (file + assertion) is more stable but risks masking a genuinely new failure that happens to share a
  signature with a pre-existing one. Recommend node id first, with a documented escape hatch.
- **Flaky baselines.** A test that fails intermittently could land in or out of the baseline depending
  on the probe run. Consider running the baseline probe twice and taking the union (conservative:
  more failures classified pre-existing) vs. intersection (aggressive). Union is safer for not
  chasing flakes; intersection is safer for not masking real regressions. Recommend union, logged.
- **Cost.** Running the full repo gate up front adds one gate's worth of latency per run. For a repo
  whose gate is slow this is real; it is still cheap next to 13 hours. Could be gated behind a
  `--baseline/--no-baseline` flag defaulting on.
- **Where attribution lives.** Cleanest is in the worker's VERIFY step reading the baseline fact;
  alternatively the completeness gate could refuse to let a ticket finish while an *introduced*
  failure exists. Recommend keeping the gate Praxis-only (as today) and doing attribution in VERIFY.
