---
name: af-ml-model-remote
description: >
  Trigger Karpathy's autoresearch loop on the EC2 devbox and return immediately -- the operator path
  for leaving an ML research run going for hours or overnight without tying up the laptop. Clones or
  refreshes the autoresearch checkout on the box, verifies data + Claude identity, launches a detached
  tmux session running the same program.md protocol af-ml-model runs locally, and reports the
  observe/tail/stop commands. Use when the human wants the research loop running unattended on the
  box. NOT for local runs -- use af-ml-model. It never waits for the run to finish and never pushes.
---

This **triggers the autoresearch loop on the devbox** and returns. It is the remote twin of
`af-ml-model`: same upstream repo, same `program.md`, same device-adaptive port. Read
`af-ml-model/SKILL.md` first — the protocol, the honest deviations from upstream, and the
`val_bpb`-comparability rules all live there and are not repeated here.

## Read this before launching: the box is currently the SLOWEST option

Verified 2026-08-13:

| | laptop (M3) | devbox |
|---|---|---|
| accelerator | MPS | **none** — CPU only |
| cores / RAM | — / 24 GB | 4 vCPU / 15 GB |
| free disk | ~22 GB | 107 GB on `/workspace` |

The devbox has **no GPU**, so a run there is slower per experiment than the same run on the laptop.
Its value is **duration, not speed**: it survives the laptop sleeping, closing, or being needed for
other work. If the human wants results *faster*, the answer is the laptop or a GPU instance — not
this box. Say so rather than launching a slower run silently.

**The GPU upgrade path is already wired.** `train.py` and `prepare.py` branch on
`torch.cuda.is_available()`, so attaching a GPU (resize to a `g5`/`g6`, or move `/workspace` to a
GPU instance) lights up FlashAttention-3, bf16, `torch.compile`, the full 21M-token eval, and the
upstream `DEPTH`/batch defaults **with no edit to any file and no change to this skill**. After such
an upgrade, `uv sync` must be re-run on Linux so torch resolves from the `pytorch-cu128` index
(`pyproject.toml` scopes that index to `sys_platform == 'linux'`), and every `val_bpb` recorded
before the upgrade becomes incomparable — start a fresh run tag and a fresh `results.tsv`.

## Box facts

| | |
|---|---|
| host | `ec2-user@52.22.249.49` |
| key | `~/.ssh/praxis-devbox.pem` |
| checkout | `/workspace/autoresearch` (its OWN clone — never a praxis worktree) |
| data cache | `~/.cache/autoresearch/` on the box |
| tmux session | `af-ml-<tag>` |
| run log | `/workspace/af-ml-<tag>.log` |

The autoresearch checkout is deliberately **outside** the af-build worktree layout under
`/workspace/*`. It is a different repo with a different loop, it must never be picked up by
`af-ticket-loop.sh`, and its `git reset` on discard must never be able to reach a praxis tree.

## Claude identity on the box

The loop runs a non-interactive `claude` session under `--dangerously-skip-permissions`. If the
box's identity is missing or half-configured, the session comes up at a dialog and sits there
forever while the tmux session looks healthy.

**Use the Box auth section of `af-build-remote-job/SKILL.md` verbatim** — Model A (shared
`~/.claude`) or Model B (isolated `CLAUDE_CONFIG_DIR`), plus the same smoke test, which is the only
valid one:

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'timeout 25 claude -p "reply with the single word READY" --dangerously-skip-permissions'
```

Expect exactly `READY`. A `-p` probe **without** `--dangerously-skip-permissions` proves nothing —
print mode skips every onboarding dialog that stalls a real session.

## Steps

**1. Resolve the run tag** from `$ARGUMENTS`, else propose one from today's date. The branch
`autoresearch/<tag>` must not already exist on the box — program.md requires a fresh run.

**2. Clone or refresh the checkout.** A stale checkout silently runs an old `program.md`.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'test -d /workspace/autoresearch \
     && git -C /workspace/autoresearch fetch -q origin && git -C /workspace/autoresearch status --porcelain | head -5 \
     || git clone -q https://github.com/karpathy/autoresearch.git /workspace/autoresearch; \
   git -C /workspace/autoresearch log --oneline -1'
```

A dirty checkout is a **REPORT, not something to force past** — it usually means a previous run's
branch is still checked out with uncommitted experiment state. Ask before touching it.

**3. Carry the device-adaptive port over.** A fresh clone is upstream and will die at import on a
CPU box (`torch.cuda.get_device_capability()` at module load). Apply the same port the local
checkout carries — `resolve_device()` in `prepare.py`, the `IS_CUDA` branches in `train.py`, the
Linux-scoped torch index in `pyproject.toml` — and commit it on the box as a single `cpu-baseline`
commit **before** creating the run branch, so experiment #1 starts from a working tree.

**4. Verify data + deps.** `uv sync`, then confirm `~/.cache/autoresearch/` has shards and the
tokenizer; otherwise `uv run prepare.py --num-shards 4`. The pinned validation shard
`shard_06542.parquet` must be present. Data prep is a one-time cost; do it in the same detached
session rather than blocking on it here.

**5. Refuse to stomp a live run.** If session `af-ml-<tag>` exists, a run is in flight — report it
and ask before killing.

```bash
ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 'tmux ls 2>/dev/null || echo "(no sessions)"'
```

**6. Launch detached** so the loop survives the SSH connection closing. The session runs `claude`
with the setup prompt; program.md's own **NEVER STOP** rule then keeps it going.

```bash
ssh -n -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  "cd /workspace/autoresearch && tmux new-session -d -s af-ml-<tag> \
     'claude --dangerously-skip-permissions \"Read program.md in this directory and follow it exactly. Run tag is <tag>. Begin the experiment loop and do not stop.\" \
      2>&1 | tee /workspace/af-ml-<tag>.log'; echo launched"
```

**7. Confirm it came up** — do not report success on the `launched` echo alone.

```bash
sleep 30 && ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 \
  'tmux capture-pane -t af-ml-<tag> -p | tail -20'
```

A pane sitting at a login/theme/trust dialog is the auth signature — go back to Box auth. A pane at
a bare shell prompt means the prompt never landed; kill and relaunch.

**8. Report** the session, the log, and:

- observe: `ssh -i ~/.ssh/praxis-devbox.pem ec2-user@52.22.249.49 -t 'tmux attach -t af-ml-<tag>'`
- results: `... 'cat /workspace/autoresearch/results.tsv'`
- tail: `... 'tail -f /workspace/af-ml-<tag>.log'`
- stop: `... 'tmux kill-session -t af-ml-<tag>'`

`results.tsv` is the artifact worth checking — it is untracked, survives every discard-revert, and
is the whole record of the run.

## Never

- Never wait for the run to finish; this triggers and returns.
- Never launch a fresh clone without applying the port first (step 3) — it dies at import on CPU.
- Never place the checkout inside a praxis worktree, or where `af-ticket-loop.sh` could find it.
- Never kill an existing `af-ml-*` session without asking.
- Never compare a devbox `val_bpb` to a laptop one, or to a published number — different device,
  different precision, different `EVAL_TOKENS`.
- Never push from the box; the run's artifact is a local branch plus `results.tsv`.
