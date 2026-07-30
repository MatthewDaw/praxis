---
name: af-build-remote-job
description: >
  Trigger af-build on the EC2 devbox for ANY Praxis project and return immediately — the operator
  path for starting a long remote build. Resolves that project's worktree and Postgres/Redis ports
  ON the box (worktree names and project names routinely differ), preflights that the ticket set has
  claimable work and non-empty building-validation checks, PATCHES the worktree's FACTORY_PROJECT
  (af-ticket-loop.sh does not, and a stale value makes the completeness gate resolve the wrong
  project and go inert rather than loud), refuses to stomp a live loop session, launches
  scripts/af-ticket-loop.sh detached under setsid so it survives the ssh closing, verifies it did not
  die in its own backend preflight, and reports the tmux session plus observe/tail/stop commands.
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
| driver | `/workspace/praxis/scripts/af-ticket-loop.sh <project> <worktree> <pg> <redis|none> [max]` |
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
per ticket with stall detection and is the maintained driver.

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

**6. Launch detached** so the loop survives the SSH connection closing. Pass `none` for redis when
the project has none; pass `[max]` only if the user bounded the run.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "cd /workspace && setsid nohup /workspace/praxis/scripts/af-ticket-loop.sh \
     '<project>' '<worktree>' <pg> <redis|none> <max?> \
     > /workspace/af-<project>-loop.log 2>&1 < /dev/null & echo started pid=\$!"
```

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
- stop: `... 'pkill -f af-ticket-loop.sh; tmux kill-session -t af-<worktree-basename>'`

## Never

- Never wait for the build to finish; this triggers and returns.
- Never guess the worktree or the ports — resolve them per step 2.
- Never launch without patching `FACTORY_PROJECT` (step 4); the gate fails silent, not loud.
- Never kill an existing loop session without asking.
- Never use `/workspace/af-queue.sh` for a single project — it is the sequential multi-project driver.
- Never push from the box on the user's behalf; the loop's own prompt already forbids it.
