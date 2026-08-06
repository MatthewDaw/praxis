# Build-methodology audit — 2026-08-06

Scope: every af-ticket-loop run on the devbox across four projects (taolu-coach, sports_analysis,
farming_analysis, appeal_engine), Aug 5–6, plus a structural audit of
`agent_factory/scripts/af-ticket-loop.sh` (2,397 lines). Evidence: full log mining of all five log
files, Praxis ticket-state history, and line-level code reading.

## Headline numbers

- **78% of all taolu-coach ticket-dispatch slots were re-dispatches** (58 of 74); 20 of 24 rounds
  contained zero never-before-dispatched tickets. T8+T1 were co-dispatched **17 consecutive
  rounds**; T10/T20 six; sports R2 seven; farming R26 seven (still looping at audit time).
- farming_analysis spent **~28 of 42 logged hours producing nothing** (two silent driver deaths
  totalling 14h, a 13.3h dependency stall, ~2.5h frozen rounds). sports_analysis lost ~5.5h of 17
  active hours to >15-min silent gaps, ~4h of it in post-merge verification.
- Two driver generations ran **concurrently on taolu-coach for 82 minutes** (W vs A), double-billing
  every worker and racing each other's tmux sessions and protocol files.
- Verification produced no verdict in 2/19 taolu rounds and shipped UNVERIFIED merges in all three
  other projects; two `verdict=pass` rounds carried `gates_green=False`.

## Findings, ranked by measured cost

### P0-1 — Findings could never be resolved without a commit (FIXED: praxis@4cdc010)

`_ticket_state.resolve_finding()` — documented as the closer of the finding lifecycle and covered
by a unit test — had **no production caller**. A post-merge verification finding fixed by a
*sibling's* merge therefore stayed open forever: the rebuilt worker correctly changed nothing,
`finding_guard` regressed it *for* changing nothing, and the pair ping-ponged until a billing halt.
This one dead wire is the single largest quota sink in the data (the 17-round T8+T1 chain, and
equivalents in two other projects). Fixed: the verdict path now stamps `regression_detail.resolved`
on every round ticket that survives verification, exactly as `open_finding()`'s docstring promised.

### P0-2 — Sibling-collision rework: the batch planner ignores shared surfaces

Four of five taolu verification regressions were not defective work — they were collisions the
round composition created:
- T1 and T15, same round, each independently implemented the **same** shared licence-provenance
  check; the merge kept one, dropped the other, regressed both.
- T10's acceptance test hard-coded boundary timings invalidated by sibling T2's data expansion.
- T20's test hard-coded "elementary routine ships no figures yet" while sibling T6 shipped the
  figures.
- sports round 7: three tickets' `__init__.py` exports silently dropped by merge; farming round 1D:
  same pattern on a shared `ServeContext`.

Recommendations (not yet implemented):
1. Round composition should avoid co-dispatching tickets whose resolved check sets or predicted
   file surfaces overlap (the data exists: `resolve_preview --by-check`, plus `renders` bindings).
2. Worker prompt rule: an acceptance test may not encode assumptions about a *sibling ticket's*
   current scope ("X doesn't exist yet") — probe current state instead.
3. Shared infrastructure (a check consumed by several tickets) needs a designated owner ticket;
   workers finding it missing should depend on it, not reimplement it.

### P0-3 — Conflict-resolver crash: `NameError: name 'p' is not defined`

The force-land path crashed the entire driver in two projects (sports 22:26, farming 07:23), each
time leaving the round unverified and the loop silently dead — 27 minutes and **9 hours** of
invisible downtime respectively. It is a plain bug at the force-land `stdin:69` site and the
highest-severity unfixed defect. Fix the NameError; wrap the force-land pass so an exception
degrades to "orphans stay queued" rather than killing the driver.

### P1-4 — No single-instance guard; SIGTERM does not kill the driver

There is no lock file or PID file anywhere; the only identity is the tmux session name, and every
round *starts* by `tmux kill-session` on that name — so two drivers on one project fight by
mutually murdering sessions (observed live: taolu W vs A, sports C vs D, appeal's interleaved
pair). Worse, the driver survives `pkill` (SIGTERM) — its exit trap runs long cleanup and can even
spawn new agent sessions on the way out (`af_assert_no_stragglers` → `resolve_conflicts`); a
SIGKILL was required in live testing. The old "wedged" driver surviving its pkill is what created
the 82-minute concurrent-generation overlap. Recommendation: `flock` on
`/workspace/.af-lock-<project>` taken at startup and held for the process lifetime; `pkill` runbook
replaced by lock-aware stop; bounded exit-trap (never spawn agents during shutdown).

### P1-5 — Verification is the weakest protocol in the loop

- The verify session launch still has the pre-fix shape: 80s ready-poll, then **"sending anyway"**
  into a possibly-absent REPL, no dismiss-Enter, no landed-confirmation (the round-dispatch path
  got all four protections; resolver got all but landed-confirm). A swallowed verify prompt burns
  the full 2700s timeout and lands as UNVERIFIED.
- No secondary evidence on a missing verdict: a verify agent that finished judging but died before
  writing `$VERDICT` is indistinguishable from one that never started; the round's green claim is
  dropped (observed as A1's "session gone" 14s after the twin driver's identical verdict landed).
- Verify agents babysit 40–60-minute test suites inside their own session lifetime (~4h of sports
  wall-clock); when the session dies first, the merge ships UNVERIFIED.
- `verdict=pass` with `gates_green=False` counts as a pass (taolu B1/B2; farming logged the
  INCOHERENT variant), and a known-red gate (`test_access_control.py`) was re-reported
  "pre-existing" for **16 hours** across five rounds without ever becoming work.

Recommendations: port the four submission protections to the verify (and landed-confirm to the
resolver) launch; on missing verdict, check git/test artifacts before declaring UNVERIFIED; run
long gates as driver-side background processes the verdict references instead of agent-foreground
waits; make `gates_green=False` block the pass verdict; auto-open a repair ticket when the same
red gate survives N consecutive rounds.

### P1-6 — One-strike billing halt, with substring false-positive risk

Detection greps pane text for `insufficient balance|402|quota exceeded|billing|payment required|…`;
first match = `exit 3`, whole loop down. No transient-vs-hard distinction, no re-probe (the startup
auth probe already knows how to ask cheaply), no backoff — observed: generation A halted at 02:41
while its concurrent twin kept building for another 89 minutes on the same account. The resolver
wait has **no** billing check at all (a 402'd resolver just stalls). And any project whose pane
happens to print "billing" or "402" halts the run. Recommendation: on match, re-probe the
credential; retry with backoff for a bounded window (e.g. 3 probes over 10 min) before halting;
anchor the patterns to API-error shapes, not bare substrings; add the same (probed) check to the
resolver wait.

### P2-7 — Environment and disk assumptions

- Worker sessions are login shells: **any global `~/.bashrc` pin flows into every worker** and
  silently overrides per-project pins. Observed: `UV_PYTHON=3.14` made every fresh `uv sync` drop
  opensim (cp313-only wheels) until the pin was removed and the interpreter constrained
  per-project. The driver unsets only the five backend/billing variables; it should log-or-unset
  known-dangerous toolchain pins (`UV_PYTHON*`, `PIP_INDEX_URL`, `NODE_OPTIONS`, …) or run workers
  with a curated env.
- `AF_MIN_FREE_GB` checks only the worktree filesystem. The **root volume hit 99%** and corrupted
  wheel extraction while `/workspace` had 115G free. Preflight should also check `/` and
  `$TMPDIR`.
- Driver exports (cache dirs, `$PY`) may not reach tmux panes at all if the tmux server predates
  them — the hardlink-cache optimization can silently not apply. Pass env explicitly via
  `tmux new-session -e` or the launch command line.

### P2-8 — WIP-commit hygiene and provenance fragility

- `commit_wip` runs bare `git add -A`: it staged **36 live agent worktrees as embedded git repos**
  across 15 WIP commits on taolu alone. Add a pathspec exclusion (`:!.claude/worktrees`) and a
  `.gitignore` entry.
- The commit-subject ticket-id matcher requires the id **trailing and exact**; farming lost real
  work to STRANDING because a worker put the id mid-line. Tolerant matching (id anywhere in
  subject, or a trailer line) plus a prompt-side reminder would have saved that branch.
- Stale `af-watch-stop-*` sentinel silently turned an appeal_engine relaunch into a no-op; launch
  should remove (with a log line) any pre-existing stop file.

### P3-9 — Observability

- 40–60-minute batch waits emit zero log lines; every stall diagnosis in this audit had to be done
  from pane captures. A one-line heartbeat every ~5 minutes (workers alive, last activity age)
  would make silence unambiguous.
- Two farming driver deaths were **totally silent** (5h and 9h gaps). With the P1-4 lock in place,
  a trivially cheap `systemd`-style liveness check (cron: lock present but PID dead → log + notify)
  closes the gap without becoming the forbidden external supervisor (it observes, never restarts).
- `worktree_recently_written` accepts *any* file mtime under the worktree as liveness — an orphaned
  process appending a log keeps a dead session "alive" past every stall window. Scope it to
  non-log paths or require pane change AND file writes.

## Fixed during this audit cycle

1. praxis@7629fb8 — round-dispatch prompt-swallow (ready-poll 3×, fail-loud, dismiss-Enter,
   landed-confirm + resend). Post-fix: 10+ round submissions, zero swallowed prompts.
2. praxis@4cdc010 — finding-resolution dead wire (P0-1). Post-fix, first relaunch: T10 and T20
   immediately exited their churn loop and finished.
3. Box-side (not in repo): system python3.13 + per-project interpreter constraint;
   `UV_PYTHON` global pin removed; uv cache relocated off the root volume; opensim pinned as a
   direct dependency (sports_analysis@d2cbe9d on the box clone).

## Suggested order for the remaining work

1. P0-3 resolver NameError (small, prevents driver death + hours of silence).
2. P1-5 verify-path submission protections (port existing code; direct quota savings).
3. P1-4 flock single-instance guard + bounded exit trap.
4. P1-6 billing re-probe with bounded backoff.
5. P0-2 batch planner shared-surface separation + worker prompt rules (largest remaining rework
   reduction, needs design).
6. P2/P3 hygiene items opportunistically.
