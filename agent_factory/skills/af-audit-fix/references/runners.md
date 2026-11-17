# Codex and Claude runners

The same `SKILL.md`, browser workflow, Praxis records and acceptance conditions apply
to both engines. Install the skill directory in `~/.agents/skills/af-audit-fix` for
Codex and `~/.claude/skills/af-audit-fix` for Claude (symlink the canonical source
where supported). Codex invocation: `$af-audit-fix`. Claude personal-skill invocation:
`/af-audit-fix`. When installed as part of the `agent-factory` Claude plugin,
the namespaced invocation is `/agent-factory:af-audit-fix` instead.

For unattended Linux/WSL jobs, use the existing
[`scripts/af-runtime-setup.py`](../../../scripts/af-runtime-setup.py) and
[`docs/managed-runtime.md`](../../../docs/managed-runtime.md). The runtime owns
service lifetime, recovery and worker dispatch. Select its backend explicitly and
preserve the operator's model choice. Do not create a second audit supervisor.
Praxis must be reachable using the execution host's own configured identity; a
successful model response alone does not prove Praxis authentication works.

## Bounded compatibility runs

`scripts/run-agent.mjs` is a one-shot process adapter for either CLI, including native
Windows. It does not schedule, retry, resume or decide audit phases. It leaves model
selection and existing CLI configuration intact and never falls back to another
provider. Example (substitute paths using your shell's normal quoting):

```sh
node scripts/run-agent.mjs --engine codex --cwd /trusted/checkout \
  --prompt-file /private/audit-prompt.txt --output-dir /private/attempt-1
node scripts/run-agent.mjs --engine claude --cwd /trusted/checkout \
  --prompt-file /private/audit-prompt.txt --output-dir /private/attempt-2
```

Use an existing trusted Git checkout with Codex; the adapter does not disable its
repository trust check. Authentication and configured MCP servers remain CLI-owned.
Explicitly name the installed binary with `--cli-path` if it is absent from PATH.
Windows npm `.cmd` shims are bypassed through their installed JavaScript entry point
using Node; arbitrary batch files are refused rather than passed through `cmd.exe`.
Native Claude `.exe` and Codex executables work directly.

Prompts go through stdin, never process arguments. The output directory contains
private `events.jsonl`, `stderr.log`, `final.txt` and `process.json`; use a new directory
for each attempt. Logs may contain private application data. On Windows secure the
parent directory's ACL: POSIX file modes do not enforce Windows ACLs. `process.json`
is process diagnostics only, never authoritative audit state or evidence of a pass.
Nonzero exit, provider error events, malformed output, missing completion events,
interruption or timeout make the adapter fail. The optional `--timeout-ms` bounds a
smoke test; omit it for healthy long-running audit work under the managed runtime.

Default permissions preserve configured capabilities and deny interactive permission
prompts. `--permissions workspace-write` explicitly maps to Codex `workspace-write`
and Claude `acceptEdits`; Claude still needs configured permission for shell commands.
`--permissions full-access` maps to Codex `danger-full-access` and Claude
`bypassPermissions`. Use that only within already authorized execution scope; the
adapter never chooses it automatically. Do not copy another machine's entire config
or real browser profile merely to satisfy authentication.

Compatibility checked against local Codex 0.169.0 and Claude Code 2.1.326 CLI help.
Claude's official [programmatic execution documentation](https://code.claude.com/docs/en/headless)
describes print mode, structured output and permission handling; its
[skills documentation](https://code.claude.com/docs/en/skills) describes personal
and plugin command discovery. Recheck installed CLI help when upgrading either CLI.
