---
description: Trigger af-build on the EC2 devbox for a project — patches the box's FACTORY_PROJECT, launches scripts/af-ticket-loop.sh detached in tmux, and reports how to observe it. Runs remotely; returns immediately.
argument-hint: <project> [extra instructions, e.g. "max 3 tickets" or "backend-only"]
---

The user invoked `/af-build-remote-job`. This **triggers a build on the EC2 devbox** and returns —
it does not build locally and does not wait for the run to finish.

Do not confuse this with `/af-build-remote-jobs` (plural), which is a project alias that runs
`af-build` **locally** against the `af-build-remote-jobs` ticket set (the project that is building
the box-service dispatch feature). This command is the operator path that actually works today:
SSH + tmux, the way `docs/brainstorms/2026-07-24-af-build-remote-jobs-requirements.md` describes
runs being started by hand. The designed path — an MCP tool queuing a job for a box service to
claim — is **not wired**: `dispatch.dispatch_job` has no production caller, there is no
`POST /jobs` route, and only the read-side MCP tools (`praxis_list_jobs`, `praxis_get_job`,
`praxis_job_activity`) exist.

## Box facts (verified 2026-07-29)

| | |
|---|---|
| host | `ec2-user@52.22.249.49` |
| key | `~/.ssh/praxis-devbox.pem` |
| repo | `/workspace/praxis` |
| build worktree | `/workspace/af-praxis` |
| tmux session | `af-praxis` (the loop derives it as `af-$(basename <worktree>)`) |
| driver | `/workspace/praxis/scripts/af-ticket-loop.sh <project> <worktree> <pg> <redis> [max]` |
| ports | Postgres `5437`, Redis `6382` |
| loop log | `/workspace/af-ticket-loop.log` |
| creds | read by the loop from `/workspace/af-praxis/.claude/settings.local.json` → `env` |

Prefer `af-ticket-loop.sh` over `/workspace/af-queue.sh`. The queue script keeps every ticket in
ONE growing CLI session and has hit 100% context mid-build twice; the loop script runs one fresh
session per ticket with stall detection and is the maintained driver.

## Steps

**1. Resolve the project.** Take it from `$ARGUMENTS`. If no project is given, ask — never guess,
since the wrong project name silently builds nothing.

**2. Preflight, locally, before touching the box.** Confirm the project has claimable work, so a
multi-hour remote run is not started against a finished or misnamed set:

```bash
cd <repo-root> && PRAXIS_ORG=praxis .venv/bin/python3 - <<'PY'
import sys; sys.path[:0]=["agent_factory/src","agent_factory/hooks"]
import _praxis
p="<project>"
f=_praxis.facts_by(category="requirement", space=p, snapshot=f"prd-{p}") or []
claimable=[x for x in f if ((x.get("meta") or {}).get("build_state")) in ("incomplete","in_progress")]
print(f"{p}: {len(f)} requirements, {len(claimable)} claimable")
PY
```

Zero claimable → STOP and report; there is nothing for the box to do.
Also confirm the project has build-validation checks, or every ticket will pin an empty check set
and FINISH will self-certify:

```bash
PRAXIS_ORG=praxis .venv/bin/python3 -c "
import sys; sys.path[:0]=['agent_factory/src','agent_factory/hooks']
import _praxis; print(len(_praxis.facts_by(category='check', space='<project>', snapshot='building-validation') or []), 'check(s)')"
```

Zero checks → warn the user explicitly before proceeding.

**3. Patch the box's `FACTORY_PROJECT`.** REQUIRED, and the easiest thing to get wrong.
`af-ticket-loop.sh` reads only `PRAXIS_ORG`/`PRAXIS_API_KEY`/`PRAXIS_API_BASE_URL` from that
settings file — it does **not** set `FACTORY_PROJECT`. The completeness-gate hook resolves
`prd-<FACTORY_PROJECT>` from the environment, so a value left over from a previous run makes the
gate resolve the wrong project and **silently go inert**, which is exactly the failure
`knowledge/serve/dispatch_launch.py` warns about. Also give the project its own MCP cache so
concurrent projects never share an org selection.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "python3 - /workspace/af-praxis/.claude/settings.local.json '<project>' <<'PY'
import json,sys
cfg,proj=sys.argv[1],sys.argv[2]
d=json.load(open(cfg))
d['env']['FACTORY_PROJECT']=proj
d['env']['PRAXIS_MCP_CACHE']=f'/home/ec2-user/.praxis/af-{proj}.json'
json.dump(d,open(cfg,'w'),indent=2)
print('FACTORY_PROJECT ->', proj)
PY"
```

**4. Refuse to stomp a live run.** If a tmux session `af-praxis` already exists, a build is in
flight. Report what is running and ask before killing it — do not silently take the worktree.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 'tmux ls 2>/dev/null || echo "(no sessions)"'
```

**5. Launch the loop detached** so it survives the SSH connection closing. `setsid` + `nohup`
because the loop itself manages tmux sessions per ticket; it must not be a child of this ssh.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "cd /workspace && setsid nohup /workspace/praxis/scripts/af-ticket-loop.sh \
     '<project>' /workspace/af-praxis 5437 6382 <max_tickets_or_omit> \
     > /workspace/af-<project>-loop.log 2>&1 < /dev/null & echo started pid=\$!"
```

Pass `[max_tickets]` when the user asked to bound the run (e.g. "max 3 tickets"); omit otherwise
(the script defaults to 999).

**6. Confirm it actually came up** — a launch that dies in preflight is silent otherwise. Wait
~20s, then check the log and expect a tmux session to appear:

```bash
sleep 20 && ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'tail -15 /workspace/af-<project>-loop.log; echo "--- tmux ---"; tmux ls 2>/dev/null'
```

The loop refuses to start on a half-configured model backend, so a `preflight: FAILED` line in the
log is a real stop — report it verbatim rather than assuming the run is going.

**7. Report** the tmux session name, the log path, and these operator commands:

- observe live: `ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 -t 'tmux attach -t af-praxis'`
- tail the driver: `... 'tail -f /workspace/af-<project>-loop.log'`
- progress: `... '/workspace/af-progress-watch.sh'`
- stop it: `... 'pkill -f af-ticket-loop.sh; tmux kill-session -t af-praxis'`

## Never

- Never wait for the build to finish; this command triggers and returns.
- Never launch without patching `FACTORY_PROJECT` (step 3) — the gate goes inert, not loud.
- Never kill an existing `af-praxis` session without asking.
- Never use `/workspace/af-queue.sh` for a single project; it is the sequential multi-project
  driver and keeps one CLI session for the whole queue.
- Never push from the box on the user's behalf; the loop's prompt already tells the session not to.

$ARGUMENTS
