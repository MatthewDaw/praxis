# The Planner (planning-side coverage gate)

> Companion to [`00-overview.md`](00-overview.md). How the planning side becomes data-driven.

## Goal
Move the planner's "what to look for" out of hard-coded Python + skill prose and into a
Praxis `planning` snapshot, so the planning gate enforces **whatever checklist is in Praxis**
with the rigor it already has — and a checklist item can be added with no code change.

## What was hard-coded (now de-hardcoded)
- The old planning-audit gate carried a frozen `GAP_LENSES` tuple
  (`failure-modes`, `security`, `data-lifecycle`, `rollback`, `who-pays`). The *enforcement* was
  already generic ("every item closed with evidence"); only the **list** was concrete. That whole
  separate gate is now deleted — its lenses become declarative planning **checks** in Praxis, and the
  single `hooks/build_completeness_gate.py` enforces them like every other ticket/check (no
  per-phase `checklist_gate`; there is exactly one gate).

## What stays deterministic (do NOT move to Praxis)
The mechanical, parseable rules in `src/agent_factory/plan_gate.py` — `R-ACCEPT-BINARY`,
`R-NO-VAGUE`, `R-NO-DANGLING`, `R-HAS-SOURCE`. They are cheap, exact, eval-covered, and must
not be subject to retrieval. They become the seed **deterministic** check kind.

## The shape (hybrid, not thin-shell)
Skills keep their flow and the deterministic gates; only the **judgment checklist** lives in
Praxis. Concretely:

1. `af-intake` (the admission + planning-validation write-path) **resolves the applicable planning checklist by query** from the
   `planning` snapshot (`meta.scope="planning"` + applicability to the project) — fresh, never a
   pre-authored list on the requirement.
2. The skill records each applicable check **as a Praxis check/ticket** (pinned as the requirement's
   completion contract) that must be addressed (replacing the hard-coded lens loop).
3. Enforcement is generic and singular: `hooks/build_completeness_gate.py` reads Praxis live and
   blocks while any pinned planning check is open-without-evidence (lenses/tech/test are just more
   check entries). There is no separate planning gate.

## Remediation (in the agent, on a hole)
Already supported by `af-intake`: for each unmet item — research / take a default + log an
episode / **ask the human** / defer as an owned decision / expand the plan with the missing
requirement. The gate only blocks + reports; the skill chooses the response.

## What the planning checklist must contain (seeded from the eval)
The `derived: true` features in `evals/plan_repro/team-app/golden-features.yaml` are the
evidence. Each implies a checklist item, e.g.:
- `AUTH-password-reset` ⇒ "any app with auth needs a credential-recovery flow."
- `ONBOARD-consent-disclaimer`, `ONBOARD-minor-consent-gate` ⇒ "consent / minor-handling for apps touching minors or health-adjacent data."
- `STATE-loading-skeleton`, `STATE-empty-nothing-yet`, `PROMPT-editor-empty-state` ⇒ "every screen needs loading / empty / error states."
- `MSG-force-approval-override` ⇒ "moderation models need an admin override."
- `STREAK-hybrid-nightly` ⇒ "resolve live-vs-batch computation ambiguities explicitly."
- `NOTIF-push-subscription` ⇒ "device/token lifecycle + offline fallback for push."

The eval then **proves** the checklist is complete: a checklist-driven plan that reproduces
the golden with zero holes means the checklist has no holes.

## First cut
De-hardcode `GAP_LENSES` → a `planning`-snapshot checklist that `af-intake` resolves by query and
records as Praxis checks; the single `build_completeness_gate` enforces closure over them generically. Lower-risk
than the validation side and the cleanest demonstration of the pattern.
(The validation side is where the live bugs are — see the separate thread / `00-overview.md`.)
