# Build-reproduction eval — can the factory build a ticket to ~the known-good commit?

The build-loop analog of `evals/plan_repro` (which proves the PLAN loop has no holes). This proves the
**BUILD loop**: given a repo at a known START commit and a ticket already in Praxis, running
`af-build` on that ticket should produce changes that land **close to a known-good GOLDEN
completion commit**.

It is the regression net for the iteration loop itself — claim → resolve-checks-by-query → build →
verify → mark `build_state="finished"` — exercised end-to-end on a real ticket (R73, the forgot-password
flow) against a real app (team-app).

## Inputs (you provide the two commits)
- `--repo`         team-app checkout (default `../../../team-app`).
- `--start-commit` the checkpoint to build FROM (pre-feature; has the eval/test scaffolding + the ticket's
  acceptance test harness). The ticket graph (prd-team-app requirements + the `auth-password-reset-e2e`
  check) is already loaded in Praxis.
- `--golden-commit` the known-good "completion" commit — the reference implementation of the feature.
- `--ticket`       the requirement to build (default R73 / `e9db7e1dada846ce843474b7785df515`).

## What it does
1. **Setup (isolated):** `git worktree add` the START commit into a temp dir (never touches your team-app
   working tree). Reset the ticket in Praxis to a pristine `build_state="incomplete"`, no lease, no pins.
2. **Run the loop:** drive the headless `claude` CLI (subscription, no API key) in that worktree with
   `/agent-factory:af-build please implement <ticket>`, tools enabled, so the real factory
   loop builds the feature autonomously (the same loop you'd run interactively).
3. **Score vs the golden** (see `score.py`):
   - **diff closeness (LLM judge):** extract the GOLDEN diff (`start..golden`) and the AGENT diff
     (`start..worktree-HEAD`) for the feature paths, and judge whether the agent's change achieves the
     same behavior — scored per acceptance ASPECT drawn from the `auth-password-reset-e2e` check:
     (1) player "forgot password" request accepted, (2) reset link obtainable via a dev mechanism,
     (3) a real `/reset-password?token=` screen, (4) new password set + token single-use, (5) login with
     the new password / old rejected.
   - **file coverage:** overlap between the files the agent touched and the files the golden touched.
   - **Praxis state:** did the loop drive the ticket to `build_state="finished"` (hard enum), and did its
     bound check get recorded as passed on the ticket node?
   - **behavioral (optional, `--run-tests`):** if the team-app stack is up, run the golden's acceptance
     test (`e2e/auth/password-reset`) against the agent's build; a green run is the strongest signal.
4. **Report + PASS/FAIL:** "gets close" = aspect score ≥ threshold (default 0.8) AND the ticket reached
   `finished` in Praxis. Behavioral, when run, is authoritative (green test ⇒ pass).

## Why diff-closeness (not exact match)
There are many correct implementations of forgot-password. The golden commit is ONE of them; the eval asks
"did the agent achieve the same observable behavior / touch the same surfaces," judged semantically — not
"did it produce the same bytes." Same philosophy as plan_repro's fuzzy coverage judge.

## Files
- `run_eval.py` — the harness (worktree setup → run loop → score → report). Parameterized by the two commits.
- `score.py`    — diff extraction + the per-aspect LLM judge + file-coverage + Praxis-state checks.
- (reuses `evals/plan_repro/claude_cli.py` for the subscription-backed judge, and `hooks/_praxis.py` /
  `hooks/_ticket_state.py` for the Praxis-state read + the ticket reset.)

## Run (once you have the commits)
    python -m evals.build_repro.run_eval --start-commit <SHA> --golden-commit <SHA>
    # add --run-tests if the team-app stack (Postgres + backend + player) is up for the behavioral signal
