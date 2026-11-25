# Durable local audit runner (the fast, unattended way)

Use this for any long local audit (many cases, hours or days). It is the pattern that ran the MVPVU
post-adoption audit (pinned build 993b78598d2c, run `post-adoption-993b78598d2c`) through hundreds of
turns unattended. A working copy of every file is in `references/durable-runner-template/`; copy it into
the audit's own directory (`~/.praxis/audits/<run>/durable/`) and change only the constants at the top.

## Why it is fast

| Choice | Effect |
|---|---|
| launchd runs a small Python supervisor every 45 seconds (`StartInterval`), not an interactive agent session | No agent sits idle polling, nothing is lost when a chat or terminal closes, and the machine sleeping or rebooting only pauses it |
| Each wake does ONE bounded model turn: `codex exec --ignore-user-config --ephemeral --json -s danger-full-access -c approval_policy=never -C <worktree> -` with the prompt on stdin, killed after 600 seconds | `--ignore-user-config` skips the operator's MCP servers, plugins and long instructions, so a turn starts in seconds; `--ephemeral` keeps no session history to reload; a hung turn cannot stall the run |
| Up to 4 Praxis cases per turn, taken from the Praxis queue's next pending or retry batch, skipping cases that are cooling down or blocked | Fewer startups per case and one browser script covers several cases on the same page |
| The turn runs inside `sandbox-exec` with a generated seatbelt profile that only allows the disposable audit app, DNS and TLS for Praxis and the model | Safe to run with full access and no approvals, so no turn ever waits for a human |
| The app under test is a separate, pinned, disposable stack (its own ports and database, started by `audit-stack.sh`, healed by `recover-stack.sh`) | Audit turns never fight the developer's localhost, migrations or landings |
| The supervisor itself (not the model) submits results to Praxis (`ledger check`, record-batch) and writes a checkpoint after every step | A crashed or timed-out turn is resumed, not redone; Praxis is the single source of truth |
| Confirmed failures are written to `fix-handoff/<case>.json` and a SEPARATE launchd repair queue (every 60 s, 450 s turns, 3 attempts per case) prepares fix lanes | Auditing and repairing run in parallel and never block each other |
| The coordinator only reviews `repair-candidates/<case>.json`, runs the scoped checks and lands with the project's standard landing script, then the next audit turn rechecks the case | No full test sweeps during the audit; the human-facing coordinator stays cheap |
| Failures back off with a capped delay (15 s doubling to 15 min) and a lease file prevents overlapping turns | Outages and restarts recover by themselves |

## Files (in `durable-runner-template/`)

| File | Role |
|---|---|
| `audit-supervisor.py` | One wake: check the pause file and lease, verify the Praxis run is auditing the pinned build, choose the next batch, run one bounded Codex turn, submit records, queue confirmed failures, update `checkpoint.json`. `--smoke` runs a one-command health turn |
| `repair-queue.py` | One wake: first reap finished repair worktrees (`reap_lanes()`: remove landed lanes with their branch; for given-up lanes save work on the branch and remove the worktree) and stop if 3 are still open; then take the next `fix-handoff` case, run a bounded Codex repair turn in that case's own worktree, write `repair-candidates/<case>.json` with files, targeted checks and the focused recheck |
| `audit.plist`, `repair.plist` | LaunchAgents (`StartInterval` 45 and 60) pointing at the two scripts |
| `audit-stack.sh` | Starts, stops and reports the disposable app stack (its own ports and database); tracks only its own pids and never touches the developer's ports |
| `recover-stack.sh`, `record-results.sh` | Restart missing audit listeners on the same database; record turn results |

Per-project constants at the top of `audit-supervisor.py`: `BUILD` (pinned SHA), `RUN`, `WORKTREE`, `BROWSER`
(Playwright install), `ORIGIN` (audit app URL), `SKILL` (this skill's SKILL.md), `CODEX` (absolute codex
binary path), batch size (4), turn timeout (600). Secrets come from an env file sourced at run time
(for MVPVU `~/.praxis/sports-analysis.env`); never copy secrets into the template.

## Start, check, pause, stop

| Action | Command |
|---|---|
| Start the app stack | `~/backups/mvpvu-stack-ops/audit-stack.sh start` (MVPVU: web :5190, API :18090, DB `mvpvu_audit`) |
| Smoke the model path | `python3 <durable>/audit-supervisor.py --smoke` (must print a turn directory) |
| Load the LaunchAgents | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist` for both, then `launchctl kickstart -k gui/$(id -u)/<label>` |
| Check progress | `cat <durable>/checkpoint.json` (state, last_turn, case_ids, retry, error); newest `turns/<ns>/events.jsonl`; `launchctl print gui/$(id -u)/<label>` (runs, last exit code); Praxis `ledger status` |
| Pause without unloading | `touch <durable>/paused` (remove the file to resume) |
| Stop | `launchctl bootout gui/$(id -u)/<label>` for both, then `audit-stack.sh stop` |

## Rules

Do not run the audit as an interactive agent session that loops and polls: that is the slow path. Do not
add parallel audit turns on one machine (the lease keeps one at a time; parallelism comes from the separate
repair queue). Keep the model turn prompt short and case-specific; the skill is read inside the turn. Never
let audit or repair turns land code: only the coordinator lands, with the project's scoped checks. The pinned
build is changed only by starting a new run.
