---
name: af-build-remote-job
description: >
  Trigger af-build on the EC2 devbox for ANY Praxis project and return immediately — the operator
  path for starting a long remote build. Resolves that project's worktree and Postgres/Redis ports
  ON the box (worktree names and project names routinely differ), preflights that the ticket set has
  claimable work and non-empty building-validation checks, PATCHES the worktree's FACTORY_PROJECT
  (af-ticket-loop.sh does not, and a stale value makes the completeness gate resolve the wrong
  project and go inert rather than loud), refuses to stomp a live loop session, REFRESHES the
  plugin-shipped driver on the box so no project runs a stale copy, launches
  agent_factory/scripts/af-ticket-loop.sh detached under setsid so it survives the ssh closing,
  verifies it did not die in its own backend preflight, and reports the tmux session plus observe/tail/stop commands.
  Use when the human wants a build run on the devbox rather than locally. NOT for local builds — use
  af-build for those; and distinct from praxis's /af-build-remote-jobs alias, which builds the
  box-service dispatch feature locally. It never waits for the run to finish and never pushes.
---

This **triggers a build on the EC2 devbox** and returns —
it does not build locally and does not wait for the run to finish. It works from ANY repo; the
project argument decides what gets built, not the current directory.

Do not confuse this with `/af-build-remote-jobs` (plural), a praxis-only alias that runs `af-build`
**locally** against the ticket set building the box-service dispatch feature. The designed remote
path (an MCP tool queues a job, a box service claims it) is **not wired**: `dispatch.dispatch_job`
has no production caller, there is no `POST /jobs` route, and nothing calls `launch_job_session`.
Only the read-side MCP tools (`praxis_list_jobs`, `praxis_get_job`, `praxis_job_activity`) exist.
So this drives the SSH + tmux path that is how runs actually get started today.

## Box facts (verified 2026-08-13)

| | |
|---|---|
| host | `ec2-user@52.22.249.49` |
| key | `~/.ssh/praxis-devbox.pem` |
| factory repo | `/workspace/praxis` (the loop script lives here regardless of target project) |
| **plugin source** | `/workspace/praxis-plugin/agent_factory` — a SEPARATE shallow clone that the marketplace resolves to. **This is where SKILL.md and hooks load from.** |
| ⚠ second checkout | `/workspace/af-praxis` is a SECOND checkout of the same repo, usually on a different branch. **Patching it does nothing at runtime** — see below. |
| driver | `/workspace/praxis/agent_factory/scripts/af-ticket-loop.sh <project> <worktree> <pg> <redis|none> [max]` (ships with the plugin; all projects run this one file) |
| tmux session | derived by the loop as `af-$(basename <worktree>)` |
| loop log | `/workspace/af-ticket-loop.log` (plus the per-run log this command redirects to) |

**The driver and the plugin now come from DIFFERENT clones, deliberately.** Before 2026-08-13 both
resolved to `/workspace/praxis`, which meant refreshing the plugin required `git pull` on the very
checkout whose `af-ticket-loop.sh` a live loop was mid-execution of — bash reads scripts
incrementally, so that is a real hazard, and it made every plugin update wait for a build to drain.
Splitting them means:

- **`/workspace/praxis-plugin` is safe to pull at any time.** Nothing executes from it; it only gets
  read at plugin-install time. This is the checkout to update when skills or hooks change.
- **`/workspace/praxis` still owns the driver** and should only be pulled when no loop is running.
- The cost is that they can drift, and refreshing one tells you nothing about the other. Step 5b
  refreshes the driver; the plugin is refreshed separately (pull `praxis-plugin`, bump
  `plugin.json`'s version, reinstall). **Check both when a change does not seem to take effect.**

`/workspace/praxis-plugin/agent_factory/.env` holds the Praxis credentials and is gitignored, so a
fresh clone will NOT have it. Copy it in, or every hook in every worker session loses its backend
and the gates go inert rather than loud.

Worktrees are per-project and each carries its own `.claude/settings.local.json`. Observed layout —
resolve it, never assume it:

| worktree | Praxis project | pg | redis |
|---|---|---|---|
| `/workspace/af-praxis` | (whatever its `FACTORY_PROJECT` says) | 5437 | 6382 |
| `/workspace/appeal_engine-build` | `appeal_engine` | 5435 | none |
| `/workspace/bestie` | `gss-prices-hardening` | 5434 | none |
| `/workspace/sotos-build` | — | — | — |

Prefer `af-ticket-loop.sh` over `/workspace/af-queue.sh`. The queue script keeps every ticket in ONE
growing CLI session and has hit 100% context mid-build twice; the loop script runs one fresh session
per BATCH with stall detection and is the maintained driver.

**The loop is batch-parallel.** Each round it computes the dependency-ready frontier itself, caps it at
`AF_BATCH_MAX` (default 16), and hands af-build that explicit id list as the run scope — so af-build fans
the batch out across parallel per-ticket worktrees, the completeness gate releases the session when the
batch is done, and the next round starts in fresh context. Tickets that depend on each other are never in
the same batch.

`AF_BATCH_MAX` is the ONLY parallelism cap in this path, and nothing narrows it underneath. The round
fans out with `Agent` subagents, which carry no core-derived cap — the driver explicitly forbids the
`Workflow` tool for exactly that reason, so the Workflow concurrency limit never applies here. (An
earlier version of this line claimed concurrency was "additionally capped by the Workflow tool"; that
was wrong, and it made small rounds look like a hard ceiling when they were only a default.) **DISK is
the real ceiling**: each worker is a full checkout plus, where the project bootstraps per-worktree
deps, a full dependency tree — so size `AF_BATCH_MAX` from the volume, not the core count.
`AF_MIN_FREE_GB` (default 15) aborts a round that will not fit, but cannot reclaim disk spent
mid-round. After every round — and on every abnormal exit, via the cleanup trap — the loop purges the
round's worktrees unconditionally (the tree is scratch, the branch is the artifact) and then reaps the
branches whose work has landed, ending with a `N branches reaped, M unmerged branches remain: <names>`
line. An empty survivor list is the normal case; a named survivor is real orphaned work, and a ticket
that reads `finished` while its commits sit unmerged fails the round and is regressed for rebuild.
`AF_KEEP_BRANCHES=1` reports instead of reaping, for debugging a bad round.

**Every parallel round is verified after it merges.** Each ticket's validations ran inside its own
worktree, against a tree where its change was the only one present, so a batch of five produces five
green claims about five trees that no longer exist and none about the merged result. After a round
lands, a fresh session re-runs the whole-repo gates on the integrated tree and dispatches independent
adversarial lenses over the combined diff — integration conflict, per-ticket acceptance re-run against
the MERGED tree, test integrity — and regresses in Praxis any ticket whose work does not survive
integration, so the next round rebuilds it. It builds nothing and pushes nothing. Single-ticket rounds
skip it, since they merge exactly the tree their worker already validated. `AF_VERIFY_ROUND=0` disables
it; `AF_VERIFY_TIMEOUT_S` bounds it, default 2700. A round that produces no verdict is logged as
UNVERIFIED, never as a pass.

## Box auth — set up or repair the Claude identity (do this BEFORE launching)

The loop drives workers non-interactively under `--dangerously-skip-permissions`. If the box's
Claude identity is missing, half-configured, or freshly re-logged-in, every round dies the same
way: `FATAL: round #N pane never signalled ready after 240s — no agent in the session`, forever,
while the loop process itself looks healthy. One farming_analysis run burned five rounds on this
before anyone looked inside the pane. There are TWO identity models; pick ONE per project.

### Model A — shared default identity (`~/.claude`), interactive login

Use when the project can share the box's main account.

1. Interactive login (ONLY the human can complete the browser step):
   `ssh -i ~/.ssh/praxis-devbox.pem -t ec2-user@52.22.249.49 claude /login`
   Open the printed URL, pick the intended account, paste the code back. This rewrites
   `~/.claude/.credentials.json`.
2. **A fresh login RESETS per-session acknowledgement UI.** Clear it so headless workers don't
   stall on a dialog: in `~/.claude.json` set `hasCompletedOnboarding = true` and
   `theme = "dark"` (login NULLS the theme, re-triggering the wizard), and
   `bypassPermissionsModeAccepted = true`. The bypass acceptance is necessary but partially
   TTY-gated — the step-3 smoke test is what actually proves it clear.
3. Confirm which account is active (email only, no secrets):
   `python3 -c "import json;print(json.load(open('/home/ec2-user/.claude.json'))['oauthAccount']['emailAddress'])"`
4. Launch (step 6 below) WITHOUT any `CLAUDE_CONFIG_DIR` override.

### Model B — isolated per-project identity (separate account, quota-isolated)

Use when the project needs its OWN account so its usage never competes with the main account's
quota (farming_analysis runs this way: `/home/ec2-user/.claude-farming`). Requires a genuinely
DIFFERENT account — a scoped `CLAUDE_CONFIG_DIR` isolates the LOGIN, not the QUOTA; two tokens
minted from one account still drain one pool.

1. `mkdir -p /home/ec2-user/.claude-<project>` on the box.
2. Mint a long-lived token UNDER that dir (interactive; the human signs in with the SEPARATE
   account): `ssh -t ... 'CLAUDE_CONFIG_DIR=/home/ec2-user/.claude-<project> claude setup-token'`.
   **The token prints exactly ONCE to stdout and is NOT stored automatically** — run it inside a
   tmux session with `remain-on-exit on`, or tee stdout, or the token is lost and the OAuth code
   (single-use) is burned with it.
3. Store it in `/home/ec2-user/.claude-<project>/env`, chmod 600:
   `export CLAUDE_CONFIG_DIR=/home/ec2-user/.claude-<project>` +
   `export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat0...`
4. **Interactive sessions ALSO need `.credentials.json` in that config dir** — `setup-token`
   does not write it, and without it every worker session stops at a "Select login method"
   dialog that `-p` probes never show. Construct it from the env token (shape:
   `{"claudeAiOauth": {"accessToken": "<token>", "refreshToken": "", "expiresAt": <ms>,
   "scopes": ["user:inference"], "subscriptionType": "max"}}`, chmod 600).
5. Clear the dialogs in `/home/ec2-user/.claude-<project>/.claude.json`: the Model-A step-2
   flags PLUS a trust entry per project root —
   `projects["/workspace/<worktree>"] = {"hasTrustDialogAccepted": true}` (trust is per
   project root; worktrees under it inherit). Also set
   `permissions.defaultMode = "bypassPermissions"` in `<config-dir>/settings.json` so every
   session on this identity runs bypass even if a launcher forgets the flag.
6. Launch (step 6 below) with `source /home/ec2-user/.claude-<project>/env &&` prefixed.

### The ONLY valid validation

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'source /home/ec2-user/.claude-<project>/env 2>/dev/null; \
   timeout 25 claude -p "reply with the single word READY" --dangerously-skip-permissions'
```

Expect exactly `READY`. **A `-p` probe WITHOUT `--dangerously-skip-permissions` proves
nothing** — print mode skips every onboarding dialog (theme, login method, folder trust,
bypass acceptance), so it returns clean while interactive worker sessions still stall. The
farming run's five dead rounds all happened after a plain `-p` probe had "verified" auth.

### Gotchas learned the hard way

- Copying a laptop's keychain credentials to the box works but binds the box to whatever account
  the laptop uses AND shares one rolling quota window — near the cap, the loop dies with
  `BILLING FAILURE` within ~90s of every launch. `/login` (A) or a dedicated token (B) instead.
- After ANY re-login, re-run the READY smoke before launching — a reset dialog swallows the
  loop's first `tmux send-keys` prompt and the session sits idle at an empty REPL.
- The `FATAL: pane never signalled ready` signature = look INSIDE the pane
  (`tmux capture-pane -t af-<worktree> -p`) — it is almost always one of the four dialogs above,
  each fixable from config without another browser round-trip.

## Steps

**1. Resolve the project** from `$ARGUMENTS`. If absent, ask — a wrong name silently builds nothing.

**2. Resolve the worktree ON THE BOX. Do not guess it** — the worktree name and the Praxis project
name frequently differ (`bestie` builds `gss-prices-hardening`). If the user named one, use it.
Otherwise find the worktree whose configured project matches, and its ports:

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 "python3 - '<project>' <<'PY'
import json,glob,re,sys
want=sys.argv[1]
for cfg in sorted(glob.glob('/workspace/*/.claude/settings.local.json')):
    try: env=json.load(open(cfg))['env']
    except Exception: continue
    wt=cfg.split('/.claude/')[0]
    url=next((env[k] for k in ('PRAXIS_DB_URL','POSTGRES_URL','DATABASE_URL','SCRAPER_DATABASE_URL') if k in env),'')
    m=re.search(r'localhost:(\d+)',url or '')
    r=re.search(r'localhost:(\d+)',env.get('REDIS_URL',''))
    mark='  <== MATCH' if env.get('FACTORY_PROJECT')==want else ''
    print(f\"{wt:34} project={env.get('FACTORY_PROJECT','?'):26} pg={m.group(1) if m else '?':6} redis={r.group(1) if r else 'none':6}{mark}\")
PY"
```

Exactly one MATCH → use it. No match or several → show the table and ask which worktree to build in.
A missing pg port is a stop, not a default: the loop passes it to the session as the DB it must use.

**3. Preflight the ticket set** — do not start a multi-hour remote run against a finished or
misnamed set. Run locally from any clone of praxis (or on the box against `/workspace/praxis`):

```bash
PRAXIS_ORG=praxis <praxis>/.venv/bin/python3 - <<'PY'
import sys; sys.path[:0]=["<praxis>/agent_factory/src","<praxis>/agent_factory/hooks"]
import _praxis
p="<project>"
f=_praxis.facts_by(category="requirement", space=p, snapshot=f"prd-{p}") or []
c=[x for x in f if ((x.get("meta") or {}).get("build_state")) in ("incomplete","in_progress")]
ck=_praxis.facts_by(category="check", space=p, snapshot="building-validation") or []
print(f"{p}: {len(f)} requirements, {len(c)} claimable, {len(ck)} build-validation check(s)")
PY
```

Zero claimable → STOP, nothing to do. Zero checks → WARN loudly before proceeding: every ticket
would pin an empty check set and FINISH would self-certify.

**4. Patch `FACTORY_PROJECT` in the chosen worktree.** REQUIRED, and the easiest thing to get wrong.
`af-ticket-loop.sh` reads only `PRAXIS_ORG` / `PRAXIS_API_KEY` / `PRAXIS_API_BASE_URL` from that
file — it does **not** set `FACTORY_PROJECT`. The completeness-gate hook resolves
`prd-<FACTORY_PROJECT>` from the environment, so a value left from a previous run makes the gate
resolve the wrong project and **go inert rather than loud** (the failure
`knowledge/serve/dispatch_launch.py` warns about). Observed live: `/workspace/af-praxis` still read
`af-build-remote-jobs` long after that project finished.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "python3 - '<worktree>/.claude/settings.local.json' '<project>' <<'PY'
import json,sys
cfg,proj=sys.argv[1],sys.argv[2]
d=json.load(open(cfg)); was=d['env'].get('FACTORY_PROJECT')
d['env']['FACTORY_PROJECT']=proj
d['env']['PRAXIS_MCP_CACHE']=f'/home/ec2-user/.praxis/af-{proj}.json'
json.dump(d,open(cfg,'w'),indent=2)
print('FACTORY_PROJECT', was, '->', proj)
PY"
```

**5. Refuse to stomp a live run.** The loop's session name is `af-$(basename <worktree>)`. If it
exists, a build is in flight — report it and ask before killing.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 'tmux ls 2>/dev/null || echo "(no sessions)"'
```

**5b. Refresh the driver.** The loop ships INSIDE the plugin at
`agent_factory/scripts/af-ticket-loop.sh`, and every project runs that one file — never a copy. Pull
the factory repo before launching so a months-old driver cannot silently drive a new run. This is not
hypothetical: one project ran for weeks under its own supervisor against a stale copy of this script
while the canonical one had been rewritten twice, and nothing surfaced the divergence.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'cd /workspace/praxis && git fetch -q origin && git status --porcelain | head -3 && git pull --rebase -q && git log --oneline -1'
```

A dirty or diverged checkout is a REPORT, not something to force past: another loop may be executing
that file right now. If a stray per-project copy or launcher still exists, point it at the canonical
script with a symlink rather than re-copying it.

**THREE checkouts exist, and the plugin comes from only one of them.** `/workspace/praxis`,
`/workspace/praxis-plugin` and `/workspace/af-praxis` are separate clones of this repo. Since
2026-08-13 the marketplace resolves to `praxis-plugin` (see *Box facts*), but that is a setting, not
a law — check it, never assume:

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "python3 -c \"import json,pathlib;print(json.loads((pathlib.Path.home()/'.claude/settings.json').read_text()).get('extraKnownMarketplaces'))\""
```

Whatever that path says is where SKILL.md, hooks and `evals/` are loaded from for every worker
session. A fix applied to the other clone compiles, tests green, commits cleanly — and changes
nothing about the running system. Observed 2026-08-06: a judge fix was verified in `af-praxis`
while the marketplace pointed at `praxis`, so the very next run reproduced the bug it supposedly
fixed. Patch the checkout on the marketplace path, or patch both and say so.

The loop SCRIPT is resolved separately, from the path you invoke in step 6, so the driver and the
plugin come from different clones **by design now** — that is what lets the plugin be refreshed
while a build runs. It is still the thing that bites: refreshing the driver in step 5b tells you
nothing about the plugin, and vice versa. When a change "did not take effect", check which of the
two you actually updated.

A plugin refresh is its own sequence, and the version bump is not optional — the install cache is
keyed by version (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>`), so same-version
content changes are simply never picked up:

```bash
git -C /workspace/praxis-plugin pull --ff-only          # safe any time; nothing executes from here
# bump agent_factory/.claude-plugin/plugin.json version, then reinstall / reload plugins
```

**6. Launch detached** so the loop survives the SSH connection closing. Pass `none` for redis when
the project has none; pass `[max]` only if the user bounded the run. The driver locates its own hooks,
interpreter, and state dir from its path, so nothing here is project-specific.

```bash
ssh -n -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "cd /workspace && AF_WATCH=1 setsid nohup /workspace/praxis/agent_factory/scripts/af-ticket-loop.sh \
     '<project>' '<worktree>' <pg> <redis|none> <max?> \
     > /workspace/af-<project>-loop.log 2>&1 < /dev/null & disown; echo launched"
```

**`AF_WATCH=1` belongs on every remote run — leave it on.** Without it the loop exits the moment the
set drains, so any ticket authored afterwards is invisible until a human relaunches. That gap is what
gets filled with a hand-written supervisor script living outside this repo, and that is not
hypothetical: one was written, and it carried three separate restart bugs — it relaunched straight
through a billing failure; it could not distinguish a dependency stall from a clean drain (both exit
`0`) and relaunched **340 times in 8 hours**; and its process-match pattern matched its own command
line, so it would have spawned a duplicate of itself. Every one of those was an outside re-derivation
of state this loop already knows exactly. **Do not write a supervisor. If a run needs to keep going,
that behaviour belongs in the loop.**

Stop a watching run with `touch <parent-of-worktree>/af-watch-stop-<worktree-basename>` (or whatever
`AF_WATCH_STOP` names) — it exits cleanly at the next poll. A DELIBERATE halt (preflight failure,
billing, or any unrecoverable condition) still exits immediately and is never waited through: watch
mode only ever waits on a genuine drain or a dependency stall, both of which a human resolves without
restarting anything.

Optional knobs, prefixed before the command: `AF_MODEL_BACKEND=sonnet` (default deepseek),
`AF_BATCH_MAX=<n>` (round width, default 16), `AF_MIN_FREE_GB=<n>` (disk floor, default 15),
`AF_VERIFY_ROUND=0` (skip post-merge verification), `AF_WATCH_POLL_S=<n>` (watch cadence, default 300).

**7. Confirm it came up** — the loop refuses to start on a half-configured model backend, and that
failure is otherwise silent:

```bash
sleep 20 && ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'tail -15 /workspace/af-<project>-loop.log; echo "--- tmux ---"; tmux ls 2>/dev/null'
```

A `preflight: FAILED` line is a real stop — report it verbatim rather than assuming the run started.
Repeated `FATAL: round #N pane never signalled ready after 240s` lines are the AUTH/onboarding
signature — go back to "Box auth" above and run the READY smoke; do not let the loop spin.

**8. Report** the tmux session, the log path, and these operator commands:

- observe: `ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 -t 'tmux attach -t af-<worktree-basename>'`
- tail: `... 'tail -f /workspace/af-<project>-loop.log'`
- progress: `... '/workspace/af-progress-watch.sh'`
- stop: `... 'pkill -f "af-ticket-loop.sh [<]project-initial>..."; tmux kill-session -t af-<worktree-basename>'`
  — bracket ONE character of the pattern, e.g. `pkill -f "af-ticket-loop.sh [a]ppeal_engine"`. Without
  the bracket the pattern matches the ssh command carrying it, so `pkill` kills itself first and the
  rest of the line never runs: the loop dies while its claude session keeps building unsupervised, and
  the failure is silent.

## Progress logging — a heartbeat is not progress

**A heartbeat proves the process is alive. It cannot tell you how far along it is, whether it will
finish this hour, or whether what it is producing is getting worse.** Those are the questions
actually asked while a job runs, and answering them by waiting for the job to end is the same as
not answering them.

Measured on the first campaign to run an expensive step: it ran **28 minutes emitting nothing**.
Its per-unit scores were 0.6183 / 0.6273 / 0.4123 / 0.0491 — diverging from the third unit onward
— and it was *simultaneously* being truncated by a wall-clock budget. Both facts existed inside
the process the whole time. Both were only discoverable after it exited, by which point a
meaningless number had been adjudicated and recorded as a verdict. Every check while it ran
returned "394% CPU", which was true right up to the end and told nobody anything.

Any step that can run longer than a few minutes MUST emit one line per unit of work:

```python
import sys; sys.path.insert(0, "<praxis>/agent_factory/scripts")
from progress import Progress

p = Progress("M06 stgcn", total=20)      # total is what makes an ETA possible
for unit in units:
    ...
    p.step(score=metric)                  # score enables the degradation warning
p.done()
```

```
[progress] M06 stgcn 7/20 35% elapsed 9m48s eta 18m12s last=0.6183 mean=0.6221
[progress][WARN] M06 stgcn: last=0.0491 is 3.2 sigma below the mean of the previous 7 (0.5881)
```

Three properties are load-bearing:

- **`total` gives an ETA**, which is what turns "it is still running" into a decision. The unit
  count is almost always known up front — folds × seeds, files to migrate, tickets in a set.
- **`score` gives a degradation warning** *while there is still time to act*. It fires at 3 sigma
  over at least 4 prior samples, so it stays rare enough to be read.
- **Lines are flushed and prefixed `[progress]`**, so a supervisor can `grep` them out of an
  interleaved log. A buffered progress line is not a progress line.

Non-Python steps follow the same convention by hand: one flushed line per unit, prefixed
`[progress]`, carrying `n/total`, `elapsed`, and a metric where one exists.

This matters more here than anywhere else: the job is **detached**, so its log is the only
channel that exists. `tmux capture-pane` shows you a REPL, and the claim heartbeat shows you a
lease — neither shows you whether the work is advancing. A remote job that emits no progress can
only be judged by waiting for it, which defeats the point of detaching it.

## Never

- Never wait for the build to finish; this triggers and returns.
- Never guess the worktree or the ports — resolve them per step 2.
- Never launch without patching `FACTORY_PROJECT` (step 4); the gate fails silent, not loud.
- Never kill an existing loop session without asking.
- Never use `/workspace/af-queue.sh` for a single project — it is the sequential multi-project driver.
- Never push from the box on the user's behalf; the loop's own prompt already forbids it.
