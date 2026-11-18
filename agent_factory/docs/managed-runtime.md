# Managed unattended builds

Use `scripts/af-runtime-setup.py` for a long Linux/WSL build. It installs the shared
factory helpers and their launch wiring together. Do not recreate a project-specific
supervisor, node wrapper or model proxy.

The runtime defaults to native Sonnet workers, one decision-making conversation per
ticket, rolling parallel dispatch (up to eight, further limited by available memory),
advisory start ordering, and affected-code validation. It retains required acceptance,
authorization, isolation, persistence, production-build and bundle checks. A timing
target never skips a check, changes an assertion or terminates healthy work.

## Start a new run

Resolve the actual worktree, project/org, application environment and database ports
first. The project must already have its Praxis plan and checks. Use an existing
Python environment that can run the factory; the setup does not reinstall application
dependencies or alter other projects.

```sh
python3 /path/to/agent_factory/scripts/af-runtime-setup.py \
  --project checkout-production \
  --worktree /workspace/checkout-production \
  --pg-port 55439 --redis-port none \
  --python /path/to/factory-venv/bin/python \
  --claude-config-dir /home/user/.praxis/auth/claude \
  --profile /workspace/checkout-production/.praxis-build.json \
  --start
```

The setup reads the existing `.claude/settings.local.json` environment, or an
explicit `--environment-file /private/project-env.json`. An optional
`--application-env-file /private/application-env.json` supplies application/test
settings separately. These input files must be owned by the current user with no
group/other permissions. Never put credential values in a checked-in profile.

A profile is static configuration; omit fields that should use the shared defaults:

```json
{
  "backend": "sonnet",
  "model": "sonnet",
  "batch_max": 8,
  "scope_policy": "Build registration and league features only. Do not repair unrelated legacy video features.",
  "selected_test_directories": ["src/features/league/", "src/features/forms/"],
  "excluded_commands": ["test:parity", "check:architecture"],
  "fixture_default_keys": ["APP_ALLOWED_ORIGINS", "APP_API_PROXY_TARGET"]
}
```

Those example directories and exclusions belong to the example project, not every
Praxis project. The shared runtime itself has no application name, user, org, IP
address or application credentials embedded in it. It derives smaller memory budgets
on smaller Linux hosts; the 24+ GiB laptop profile uses an 8 GiB soft/12 GiB hard
project cap, three 3 GiB validation slots, one slot for unit/API/quality checks and
two for a build/browser check. Override the numeric resource fields in the profile
when the project's measured requirements differ. Host headroom and per-check caps
remain enforced, including while independent projects share the machine.

`--render-only` writes a private runtime and unit previews without pinning the
project, installing user units or starting anything. A normal install pins only this
project's runtime environment and installs its own `praxis-af-<project>` user units.
It takes the driver's worktree mutex before changing settings and refuses to
reconfigure a live service. `--start` preflights the selected model, then enables and
starts the owned service. It never restarts an existing build or updates a live
factory snapshot.

Starting also enables lingering for the current Linux user without an interactive
password request, so closing SSH does not tear down the user manager. This is a
per-user service-lifetime setting; no global WSL configuration is changed.

## Deployed website audits

`af-audit-fix` uses the same installer and resource scopes with `"mode": "audit"`
in its static profile. Select `"backend": "codex"` or `"backend": "sonnet"`
(Claude); the audit adapter does not override the selected CLI's model defaults.
An audit profile also supplies:

```json
{
  "mode": "audit",
  "backend": "codex",
  "audit_run": "release-audit-20261117",
  "audit_prompt_file": "/private/audit-assignment.md",
  "audit_browser_probe": ["/usr/bin/node", "/private/browser-probe.mjs"],
  "audit_phases": ["audit", "reaudit"],
  "audit_wait_phases": ["fix", "deploy"]
}
```

The probe must launch the browser, navigate to the intended target and assert
readiness. Use job-owned browser dependencies and an explicit target.
`--start` checks canonical Praxis state and verifies model file access before
enabling the service. For mapped initial audits, the supervisor runs the browser
probe after all outstanding scenarios have source-based plans. Other phases
retain the setup probe. `bin/af-runtime check` runs the bounded setup check.

The default preparation pass writes resumable plans to
`runtime/state/audit-preparation-<run>.json`. It skips terminal originals and
already prepared IDs, passes scoped evidence to up to three preparation workers, and
continues to execution after validating the remaining plans. The audit worker
uses live checkpoints and verdicts to reuse completed work.

The project stays the real Praxis project. Audit units and the default runtime
directory add `-audit-<run>` so they cannot replace that project's build service.
For example, `praxis-af-checkout-production-audit-release-audit-20261117.service`.
Use a separate worktree and private environment file; do not repin a live build.
The assignment prompt is copied privately into the runtime at installation.

The remote coordinator can own `audit` and `reaudit`, waiting for a separately
assigned local coordinator to finish `fix` and `deploy` on the same Praxis run.
The supervisor checks canonical phase after every bounded agent turn; process
exit zero never passes a case. Three consecutive turns without canonical progress
stop with exit 78 and require attention, rather than retrying indefinitely. Configure
`audit_no_progress_limit` and `audit_batch_timeout_s` (default1800 seconds) only
when the workload warrants it. `complete` and `partial` are reported distinctly;
a terminal partial run still has untested coverage. An explicit stop retains
Praxis progress and evidence for a later restart.

For hybrid audits, create a separate run for the local target and use
`audit_smoke_case_ids` to limit the deployed worker to environment-dependent
originals. The run IDs keep reports, evidence hashes, and verdicts separate.
The worker checks only the configured scope while the full inventory stays
available in the original ledger.

## Smoke fixture recovery

A smoke audit can configure `audit_smoke_fixture_providers` as an object keyed
by fixture type. Each entry supplies `case_ids` or exact `blocker_prefixes`,
a private handoff description,
and operator-authored `probe` and `provision` argv arrays with absolute
executables. Use run-owned disposable organizations and fixture identities for
writes. Store new resource ownership and cleanup references with the audit run.

When both selectors are set, a blocked case must match both. The supervisor probes only providers with matching blocked cases in its assigned inventory. A successful probe
returns `{"available": true, "revision": "opaque-fixture-revision"}`. The
supervisor can provision against a missing fixture, reprobes, and requeues
affected smoke cases once per verified fixture revision. Recoverable attempts
are bounded and saved in Praxis. The supervisor stays live through a retry
cooldown when a provider still has an attempt available. If the queue has
unresolved blockers after recovery is exhausted, it exits with status 78.
Provider commands come from the runtime profile.

Set `audit_smoke_local_recovery: true` for a loopback-only disposable run.
For a blocked smoke result with the prefix `Local browser interrupted:`, the
supervisor uses the configured browser preflight to verify that a fresh
browser can load the app. A successful API health check alone cannot requeue
this browser-specific interruption. The retry preserves completed checks.
When no verified fixture retry is already queued, the handler queues one
worker-led recovery alongside ordinary work. Each eligible blocker gets one
such recovery attempt.
The prior blocker stays in the ledger, with the recovery outcome attached to
the same original case. This does not apply to deployed targets, reserved
assignments, or cases already deferred by a prerequisite handler.

## Existing laptop login

Reuse an authenticated private Claude config directory. No API key is needed. When
an import is needed, use the helper; it rejects an MCP-only identity and never prints
a token:

```sh
# On the laptop with a Claude Code Keychain login:
python3 scripts/af-claude-auth.py --from-keychain \
  --destination /private/claude-login

# Or on WSL, from an already securely transferred credential file:
python3 scripts/af-claude-auth.py --from-file /private/existing-credentials.json \
  --destination "$HOME/.praxis/auth/claude"
```

For a private transport pipe, the receiver also supports `--stdin`. Never echo
credentials into a command, journal, prompt or repository. Do not use a default
Keychain record without checking the account: the helper selects the current user's
`Claude Code-credentials` record and validates subscription OAuth fields. Existing
config/login storage is reused by subsequent runs; worker and judge launches remove
alternate provider/API environment overrides. The generation preflight uses the
same selected model and credential directory as the workers.

## Observe, stop and resume

```sh
systemctl --user status praxis-af-checkout-production.service
journalctl --user -u praxis-af-checkout-production.service -f
~/.praxis/runtimes/checkout-production/bin/af-runtime observe
systemctl --user stop praxis-af-checkout-production.service
```

The maintenance timer checks unfinished idle native conversations every minute,
leaving active/background commands alone. Every approximately twenty minutes it
records fresh Praxis counts and full first-launch-to-finished timings in
`state/timing-observations.jsonl`. Resuming a conversation never resets the recorded
start. These observations are diagnostics, not a replacement for authoritative
Praxis state or proof a validation passed. A slow ticket is reported honestly;
optimization still requires inspecting its actual bottleneck.

Scopes bind to the owning service, so an explicit stop stops that job's workers,
validators and maintenance. A failed driver is retried with backoff. Before cleanup,
live unfinished workers are placed in a bounded, private, validated adoption handoff;
the next driver adopts the same conversations and checkouts. Pending integration
verification survives retry and runs before the integration tree changes. Missing or
failed verification never becomes a passing verdict. Completed idle conversations
are archived before their CLI memory is released; a later dispatch resumes their
original session.

On WSL, `--start` also starts a hidden Windows `wsl.exe` invocation that stays alive
only while this owned service runs. Its keepalive exits after the service stops.
It changes neither global WSL memory settings nor other jobs. Windows interop and a
working user service manager must already be available. No software can keep a run
executing through physical host shutdown or sleep; enabled user units provide the
restart path when the host/user manager starts again.

## Validation rules carried into every role

- Explicit changed-behavior tests, including affected runtime callers and permission
  boundaries; no implicit whole-package/API/browser/video census per ticket.
- The same owner policy goes to ticket workers, conflict resolvers and post-merge
  verifiers. Integrated checks cover the union of landed changes. Required actual
  production builds and bundle budgets remain mandatory.
- `af-check-ticket-types.mjs --base <recorded-sha>` checks authored roots using their
  compiler configuration and imported signatures, retaining global diagnostics.
  It resolves TypeScript from the authored workspace, rather than assuming an app
  directory name. Run authored-file lint separately.
- Guarded commands acquire a reservation per discrete check, not for a persistent
  console. Inherited admission tokens prevent nested tool launches deadlocking their
  own checkout. Queue time is recorded separately from execution time.
- Browser fixtures remove only matching inherited application origin/proxy defaults;
  explicit test overrides survive. With an application-env file, conventional
  `*_ALLOWED_ORIGINS` and `*_API_PROXY_TARGET` keys are discovered automatically.
- Reuse completed evidence only when relevant source/dependency/configuration,
  command and fixture inputs agree. Keep original revision and output. Do not create
  another checkout solely to recheck an already committed clean worktree.
- Required runtime reviewers receive actual sanitized execution evidence, not just
  test source. Resolve review findings before rerunning unrelated final checks.
  `af-sonnet-judge.py` provides a fresh tool-free judge without MCP/session startup;
  its stdin must include evidence contents because it cannot open artifact paths.
- Worker type/lint results cannot be recorded as graded-review passes. Advisory
  reviews retain their honest result without reopening otherwise complete work.
  A rubric requiring a located defect is not failed for vague, below-confidence
  preferences. No mandatory risk-specific rubric is demoted by the setup.
- Store logs/screenshots under `$AF_STATE_DIR/worker-evidence/<ticket>/<revision>/`,
  outside disposable worktree roots. Preserve imported provenance, dropped merge
  intent, claims and all compatible check evidence across recovery.

## Source and upgrades

The installer snapshots committed factory source into a content-addressed,
read-only `runtime/factory/<digest>/` directory. A dirty checkout cannot silently
replace the release. Each project pins its snapshot; updating canonical source does
not overwrite Bash or Python files being used by a running build. Installed plugin
packages without Git use their packaged files, still hashed and made read-only.
Credentials, environments, caches and local runtime state are excluded.

Update and commit canonical source, refresh the plugin release, then run setup for
new jobs. For an existing job, drain it to a safe boundary before installing a new
runtime. Do not reset the remote factory checkout: it may contain unpublished work.
The active build's snapshots remain in place for recovery and audit.
