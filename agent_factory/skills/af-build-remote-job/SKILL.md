---
name: af-build-remote-job
description: >
  Start an unattended Praxis build on the remote laptop's Linux/WSL workspace and
  return after confirming startup. Use the canonical managed runtime setup: native
  Sonnet fan-out on the existing laptop login, project-owned resource limits and
  supervision, rolling dispatch, affected-code validation, durable recovery and
  twenty-minute timing observations. Resolve the worktree, org and ports; preserve
  active jobs and immutable factory snapshots. Never push or wait for completion.
---

Use the canonical installer, not a custom supervisor, model proxy, node wrapper or
one-off launch script. The implementation and configuration contract are in
`../../docs/managed-runtime.md`; read that document from the same plugin source.
All defaults below are per job, never machine-wide model or WSL settings.

## Resolve the host and project

The owner's remote build host is the Windows laptop reached with `ssh winlaptop`.
SSH lands in Windows; execute Linux commands inside its WSL Ubuntu distribution.
EC2 is retired. Do not start or stop another host or another project's services.

For a short command:

```sh
ssh -o BatchMode=yes winlaptop 'wsl -d Ubuntu --exec bash -lc "ls /workspace"'
```

For a longer operation, write a script locally and pipe it to WSL Bash. Keep command
text and data separate; do not interpolate JSON, credentials, backticks or command
substitutions into a Windows command line. Strip Windows carriage returns from
responses when needed. Long jobs run in the owned user service, not the SSH shell.

Resolve rather than guess:

1. The target's actual Git worktree and integration branch. Worktree and Praxis
   project names routinely differ. Preserve dirty work and live worker branches.
2. Its project/org and private Praxis identity from existing local configuration.
   A different project's/default factory key is not interchangeable. Confirm the
   intended plan and applicable checks exist before dispatch.
3. Its real application/database ports and existing Python environment. Inspect
   local project configuration and running services; do not change other jobs.
4. A current canonical factory release containing `scripts/af-runtime-setup.py`.
   Prefer `~/.praxis/factory-current/agent_factory` when present.
   `/home/mattd/af-factory/agent_factory` is an older source location, but it may
   contain unpublished commits. Never reset it or assume it is current. Refresh
   via a new committed release/snapshot, without overwriting a live runtime.

Inspect current service, tmux and worktree-lock ownership before installing. If the
same project already runs, report its real status and leave it running. A live
service or worktree mutex is not permission to start a duplicate or change its
settings. Use a separate worktree for unrelated work.

## Reuse the existing laptop authentication

Default to `backend=sonnet`, `model=sonnet`, unless the owner selects another model.
Use the existing authenticated Claude config directory, normally
`~/.praxis/auth/claude` on WSL, or the already working private login directory.
Do not ask the owner to authenticate again while that identity works.

If credentials have to be imported from the local laptop, use
`scripts/af-claude-auth.py` and a private file/transport. It selects the current
user's `Claude Code-credentials` Keychain account, validates subscription OAuth,
and never prints the token. An MCP-only Keychain record is not a model login.
Import only the existing authorized identity. Never put credentials in Git, a
prompt, the command line, logs or a static project profile.

The installer generation-probes the selected model when `--start` is used. Login
status alone is insufficient. A real auth/network failure is not a healthy startup;
report the observed failure without pretending a worker started.

## Install and launch

Use a static profile for project-specific scope and resource overrides. The defaults
already provide rolling native workers, advisory dependency start ordering, memory
admission, resumable recovery and scoped validation. Keep required checks mandatory;
never globally demote rubrics or auto-approve manual requirements to accelerate a
run. Exclude unrelated legacy subsystems only when that matches the owner's scope.

Run on the resolved Linux host, using its real paths and ports:

```sh
python3 "$FACTORY/scripts/af-runtime-setup.py" \
  --project "$PROJECT" --worktree "$WORKTREE" \
  --pg-port "$PG_PORT" --redis-port none \
  --python "$FACTORY_PYTHON" \
  --claude-config-dir "$EXISTING_CLAUDE_CONFIG" \
  --profile "$PROJECT_PROFILE" \
  --start
```

Omit `--profile` when defaults match. Supply `--environment-file` and optionally
`--application-env-file` only when the existing project settings do not already
provide those values. Private JSON inputs must be owner-only files. Do not expose
their contents when diagnosing startup. Redis may be a resolved port instead of
`none`. Do not pick the default Postgres port merely because the argument permits it.

The setup pins this project's environment, installs its own user units, snapshots
committed factory code and starts the supervisor. On WSL it also creates a
service-bound Windows keepalive. It does not change global WSL settings, restart
other services, pull/reset a shared checkout or push code. `--render-only` previews
the isolated artifacts without changing the project or starting anything.

## Confirm and return

Verify the actual owning service is active, inspect its journal, and confirm a
native worker was launched or the driver is in a specifically identified legitimate
preflight/recovery phase. A transient observation timeout is not proof it stopped.
Do not replace a quiet but live worker solely because a poll expired.

Report the observed state plus these commands with the actual project substituted:

```sh
systemctl --user status praxis-af-<project>.service
journalctl --user -u praxis-af-<project>.service -f
~/.praxis/runtimes/<project>/bin/af-runtime observe
systemctl --user stop praxis-af-<project>.service
```

Return once startup is confirmed. The service owns retries, idle continuation,
resource admission and twenty-minute timing observations; do not hold an SSH session
or add another polling coordinator to keep it alive. Observations are diagnostics,
not validation passes. When asked to optimize a slow ticket, use its original elapsed
clock and actual commands/queues; preserve required coverage and compatible evidence.
