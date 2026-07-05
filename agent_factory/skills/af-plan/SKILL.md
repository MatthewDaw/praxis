---
name: af-plan
description: >
  The explore/research front-end of the agent factory. Use to turn a rough feature idea (or a
  thin PRD) into an exhaustive, deliberately-over-generated requirements doc — scope, behaviors,
  edge states, implied features, open decisions — by driving compound-engineering's ce-brainstorm
  and ce-ideate plus adversarial research. It does NOT write Praxis, run the audit, or save a
  snapshot: it produces the messy-but-thorough doc that af-intake then admits and hardens.
---

# Factory Plan (explore / research front-end)

af-plan has ONE job: produce a **thoroughly-exhausted requirements doc** from a rough idea. Its
output looks like what `ce-brainstorm` produces — a real requirements doc (scope, behavior, success
criteria, edge states, implied features, open decisions) — only pushed harder and broader. That doc
is the hand-off. **af-plan does NOT write to Praxis, does NOT run the audit, does NOT save a
snapshot.** All admission, validation, and hardening live in **af-intake**.

State and the build loop are out of scope here. Praxis is the single source of dynamic truth; there
are no JSON status files or locks (see `METHODOLOGY.md`, `docs/factory-state-contract.md`). The
FIND→CLAIM→RESOLVE→BUILD→VERIFY→FINISH build loop belongs to **af-build** — do not reproduce it here.

**Why over-generate (review leverage).** A bad line of *code* costs one bad line; a bad line of
*plan* can spawn hundreds of bad lines of code; a missed *requirement* can spawn thousands. The
cheapest place to catch a missing behavior is before it exists. So af-plan's bias is **throw
everything in** — every behavior, edge, implied feature, and open question you can surface. Filtering,
de-duping, conflict-resolution, and the done-gate happen later in af-intake; your failure mode is
*omission*, not excess.

## Step 0 — Clarify a rough idea with compound-engineering (REQUIRED)

**compound-engineering is a HARD factory dependency** (declared in `.claude-plugin/plugin.json` and
the marketplace manifest). It is the required front-end whenever the input is a *rough feature idea*
rather than a finished requirements doc:

- **`ce-brainstorm`** — turn the idea into a real requirements doc (scope, behavior, success criteria,
  edge states) through collaborative dialogue. af-plan exhausts requirements; it is not where you
  invent product shape from a one-liner. Run this FIRST when the input is vague or ambitious.
- **`ce-ideate`** — surface the adjacent and implied features the idea never stated, and critically
  evaluate them. Admit the accepted ones into the doc as candidates. This is the generative move that
  feeds af-intake's planning-checklist lenses (which later FORCE the implied decisions at af-intake).

Skip Step 0 only when a complete, hardened PRD already exists — and say so explicitly, never
silently. (Even then, an `ce-ideate` pass to surface implied features is usually worth it.)

## Step 1 — Choose rigor and decision mode

Ask the human **two** blocking questions, **one per turn** (never stack them — see Interaction rules).

**1a. Rigor — how hard to push:**
- **Quick** — one brainstorm + one ideate pass; capture the obvious scope and the loudest gaps.
- **Rigorous** — loop brainstorm/ideate and the research + adversarial passes below **until a fresh
  challenge pass surfaces nothing new** (gap-finding run loop-until-dry). Every gap-lens
  (failure-modes, security, data-lifecycle, rollback, who-pays-the-tradeoff) must explicitly
  fire-or-pass and be written into the doc.

**1b. Decision mode — how to settle every genuine fork/open decision** (only after the
research-to-ground pass in Step 2a has failed to answer it from sources):
- **Collaborate** (default) — surface each genuine product fork to the human as a blocking question;
  the human decides. High-touch: the human weighs in on every open decision as it comes up.
- **Autonomous (force decisions)** — never block on a fork: take the low-regret default on every one,
  record it in the doc as a **default-with-rationale flagged for override**, and keep moving. The
  human reviews the flagged defaults at the end (Step 3) instead of being asked mid-flight.

Note **both** modes in the doc, so a quick/autonomous pass never poses as an exhausted, human-decided
one. Decision mode governs *only how a genuine fork is settled* — it never licenses skipping the
research-to-ground pass, and even in Autonomous mode an adversarial challenge is still **recorded, not
resolved away** (Step 2b), and a high-regret/irreversible fork (data loss, auth, money) still surfaces
to the human rather than being defaulted.

## Step 2 — Research, pushback, and exhaustiveness

Work the requirement space until it stops yielding. These moves are about *generating and grounding*
candidates — none of them touch Praxis.

**a. Research to ground, not to ask.** Before recording an open question, try to answer it from
sources: the source text / PRD section, prior art, conventions, the codebase. Fan out parallel web
or repo research where it helps (`ce-web-researcher`, `ce-best-practices-researcher`, or the read-only
`Explore` agent for bulk reading). Read any file the human or the PRD names explicitly *fully*
yourself; only delegate exploratory reading. When a convention clearly applies (PRD silent, clear
low-regret default), record it in the doc **as a default with its rationale** and flag it for the
human to override — don't bury it as settled.

**b. Adversarial pass (a skeptic must challenge every candidate).** For each behavior, file ≥1
falsifiable challenge — missing actor, unbounded condition, hidden dependency, unhandled empty/error
case, irreversible action. Use leading yes/no questions to corner vagueness into a concrete gap
("so you have NOT specified what happens on empty input — correct?"). Spawn an adversarial reviewer
(`ce-adversarial-document-reviewer`) over the candidate set whose only job is to falsify and surface
missing actors, unbounded conditions, and dangling concepts. Record every challenge in the doc as an
open item; do not resolve it away — af-intake's audit is where challenges get forced to a decision.

**c. Edge states and implied features are first-class.** For every screen/flow/entity, enumerate the
states the idea didn't mention: loading / empty / error / partial-failure / permission-denied /
offline, plus the lifecycle (create / edit / delete / recover) and the can't-miss failure classes
(data loss, auth bypass, irreversible action, silent partial failure). Every domain concept a
requirement *references* must be *defined* somewhere in the doc or explicitly marked out of scope —
no dangling concepts. List these even when you're unsure; over-generation is the point.

**d. Capture acceptance intent, not a gate.** Where you can, sketch a binary acceptance condition
("when X, the system does Y, observable via Z") so af-intake has something concrete to admit. Where
you can't yet, record it as an **open decision** with the options you see — do NOT force a fake
condition and do NOT block. Turning these into admitted, gated facts is af-intake's job.

**Stop by information-gain, not exhaustion.** When the next question's expected information gain is
low, say so and stop. But beware the inverse trap: a short, clean doc usually means *nothing was
challenged yet*, not that the space is exhausted.

## Step 3 — Produce the requirements doc and hand off

Write the doc as plain markdown (the `ce-brainstorm` shape, pushed harder). Include:
- **Scope** — what's in, what's explicitly out, the personas/actors involved.
- **Behaviors** — each desired behavior with a sketched acceptance condition where possible.
- **Edge states & failure classes** — the enumerated empty/error/lifecycle/loss cases from Step 2c.
- **Implied features** — the adjacent features `ce-ideate` surfaced and the human accepted.
- **Open decisions** — every unresolved fork and adversarial challenge, with options and what you
  already checked ("PRD silent; no convention applies"), so af-intake/af-intake can force each.
- **Defaults taken** — conventions you applied, each flagged for override.
- **Rigor mode** — which passes ran.

Then hand the doc to **af-intake**. State the boundary explicitly in your closing report:

> **af-intake inserts this doc into Praxis and runs all planning validation** — it admits each
> settled requirement as a `source="prd-<project>"` fact, runs the cold-eyes **af-intake**, resolves
> contradictions, clears the done-gate, and calls `save_snapshot`. af-plan writes nothing to Praxis.

## Interaction rules
- One question per turn (blocking tool, single-select + free-text escape). Never stack questions.
- Draft-for-judgment over ask-from-blank: pre-fill what you resolved as "resolved from <source>" or
  "default (PRD silent) — confirm?" so the human edits rather than dictates.
- Cite the source that grounded each suggestion.
- Open-ended only when the answer is inherently narrative or a menu would bias it.

## Never
- Never write to Praxis (no `add_insight`, no `select_space`/`clear_graph`/`save_snapshot`), never
  run the audit, never run a plan/done gate — all of that is **af-intake**.
- Never reproduce the build loop (af-build) or build/claim/pass state.
- Never silently skip the `ce-brainstorm`/`ce-ideate` front-end for a rough idea — say so if you do.
- Never resolve an adversarial challenge or open fork away on your own — record it for af-intake.
- Never under-generate: when unsure whether a behavior/edge belongs, include it and flag it.
