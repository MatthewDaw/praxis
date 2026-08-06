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

## Box facts (verified 2026-07-29)

| | |
|---|---|
| host | `ec2-user@52.22.249.49` |
| key | `~/.ssh/praxis-devbox.pem` |
| factory repo | `/workspace/praxis` (the loop script lives here regardless of target project) |
| ⚠ second checkout | `/workspace/af-praxis` is a SECOND checkout of the same repo, usually on a different branch. **Patching it does nothing at runtime** — see below. |
| driver | `/workspace/praxis/agent_factory/scripts/af-ticket-loop.sh <project> <worktree> <pg> <redis|none> [max]` (ships with the plugin; all projects run this one file) |
| tmux session | derived by the loop as `af-$(basename <worktree>)` |
| loop log | `/workspace/af-ticket-loop.log` (plus the per-run log this command redirects to) |

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

**Two checkouts exist, and only one is on the runtime path.** `/workspace/praxis` and
`/workspace/af-praxis` are separate clones of this repo, typically on different branches. The
plugin marketplace in `~/.claude/settings.json` resolves `agent-factory-local` to ONE of them —
check it, never assume:

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "python3 -c \"import json,pathlib;print(json.loads((pathlib.Path.home()/'.claude/settings.json').read_text()).get('extraKnownMarketplaces'))\""
```

Whatever that path says is where SKILL.md, hooks and `evals/` are loaded from for every worker
session. A fix applied to the other clone compiles, tests green, commits cleanly — and changes
nothing about the running system. Observed 2026-08-06: a judge fix was verified in `af-praxis`
while the marketplace pointed at `praxis`, so the very next run reproduced the bug it supposedly
fixed. Patch the checkout on the marketplace path, or patch both and say so.

Note the loop SCRIPT is resolved separately, from the path you invoke in step 6 — so the driver and
the plugin can come from different clones at the same time. That is the trap: refreshing one in
step 5b tells you nothing about the other.

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

**8. Report** the tmux session, the log path, and these operator commands:

- observe: `ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 -t 'tmux attach -t af-<worktree-basename>'`
- tail: `... 'tail -f /workspace/af-<project>-loop.log'`
- progress: `... '/workspace/af-progress-watch.sh'`
- stop: `... 'pkill -f "af-ticket-loop.sh [<]project-initial>..."; tmux kill-session -t af-<worktree-basename>'`
  — bracket ONE character of the pattern, e.g. `pkill -f "af-ticket-loop.sh [a]ppeal_engine"`. Without
  the bracket the pattern matches the ssh command carrying it, so `pkill` kills itself first and the
  rest of the line never runs: the loop dies while its claude session keeps building unsupervised, and
  the failure is silent.

## Never

- Never wait for the build to finish; this triggers and returns.
- Never guess the worktree or the ports — resolve them per step 2.
- Never launch without patching `FACTORY_PROJECT` (step 4); the gate fails silent, not loud.
- Never kill an existing loop session without asking.
- Never use `/workspace/af-queue.sh` for a single project — it is the sequential multi-project driver.
- Never push from the box on the user's behalf; the loop's own prompt already forbids it.
