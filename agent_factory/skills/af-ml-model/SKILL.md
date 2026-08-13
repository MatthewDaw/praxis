---
name: af-ml-model
description: >
  Run Karpathy's autoresearch loop LOCALLY: an autonomous ML research session that edits train.py,
  runs a fixed-wall-clock training experiment, and keeps or reverts the change based on val_bpb --
  forever, until a human stops it. This skill is a THIN TRIGGER, not a reimplementation: the protocol
  of record is program.md in the autoresearch checkout, and the agent follows that file, not this one.
  Use when the human says "run autoresearch", "start the ML research loop", "/af-ml-model", or points
  at an autoresearch checkout and wants experiments running. For a long unattended run on the EC2
  devbox use af-ml-model-remote instead. It does not push, and it does not stop on its own.
---

This runs **Karpathy's loop, unmodified**. The instructions the agent obeys live in
`program.md` inside the autoresearch checkout — read that file and follow it literally.
Everything below is setup and the environment-specific facts program.md cannot know.

> **Do not paraphrase program.md into this file, and do not follow a summary of it.**
> It is ~110 lines and it is the spec. Read it at the start of every run. If it has changed
> upstream, the changed file wins over anything written here.

## Where it lives

| | |
|---|---|
| upstream | `https://github.com/karpathy/autoresearch` (no declared license as of 2026-08-13) |
| local checkout | `~/Documents/official_repos/autoresearch` — a sibling of the praxis repo, NOT inside it |
| pinned upstream commit | `228791f` (2026-03-26) |
| data + tokenizer cache | `~/.cache/autoresearch/` (~485 MB at `--num-shards 4`) |
| protocol of record | `<checkout>/program.md` |

The checkout is a **separate repo**. Never commit autoresearch experiments into praxis, and never
run the loop from inside the praxis working tree — the loop does `git reset` on discard, and pointing
that at praxis would destroy work. Verify `git rev-parse --show-toplevel` before the first commit.

## This machine is not an H100 — read this before promising throughput

Upstream targets a single H100 and every default assumes it. This checkout carries a one-time
**device-adaptive port** so the same files run on CUDA, Apple silicon (MPS), and CPU. On a CUDA box
every branch below takes the upstream path, so a GPU is a pure upgrade with no further edits.

| concern | CUDA (upstream) | MPS / CPU |
|---|---|---|
| attention | FlashAttention-3 kernel | `F.scaled_dot_product_attention`, sliding window as an additive mask |
| dtype | bf16 autocast | fp32 (`nullcontext`) |
| `torch.compile` on the model | on | off (Muon's `fullgraph=True` ops do not lower) |
| `peak_vram_mb` | `torch.cuda.max_memory_allocated()` | process RSS |
| `TOTAL_BATCH_SIZE` | `2**19` | `2**16` |
| `DEPTH` / `DEVICE_BATCH_SIZE` | 8 / 128 | 4 / 8 |
| `EVAL_TOKENS` | `40 * 524288` | `2 * 524288` |

**Two of these are honest deviations, not just plumbing, and must be stated in any report:**

1. **`EVAL_TOKENS` is cut ~20x off CUDA.** It lives in `prepare.py`, which program.md declares
   read-only. The upstream 21M-token eval costs seconds on an H100 and ~16 minutes on an M3 — triple
   the 300s training budget — so every experiment would exceed program.md's 10-minute kill and log as
   `crash`, and the loop would spin forever recording nothing. The cut preserves the only property the
   ratchet needs: every experiment in a run is scored by the identical metric on the identical
   validation shard. **Never change it mid-run** — doing so invalidates every earlier row in
   `results.tsv`.
2. **fp32 instead of bf16** is a compute-precision change.

Consequence: **`val_bpb` from this machine is not comparable to a CUDA run's number, or to any
number Karpathy published.** It is only comparable within a single run. Never present a local
`val_bpb` as if it sat on his leaderboard.

Measured on an M3 (2026-08-13): ~14k tok/s, ~4.6 s/step, ~65 steps inside the 300s budget, model
11.5M params. Expect **~6-7 minutes per experiment** end to end — close to his ~12/hour cadence.
`DEPTH` and `DEVICE_BATCH_SIZE` are the knobs the loop is most likely to push back up as it finds
headroom; that is the loop working, not a regression.

## Setup

Do program.md's own setup section, in its order. The only environment-specific parts:

1. **Confirm the checkout exists** and is the autoresearch repo, not praxis. Clone it if missing.
   If `uv sync` fails on macOS, `pyproject.toml` scopes the CUDA-only `pytorch-cu128` index to
   Linux (`marker = "sys_platform == 'linux'"`); without that, torch does not resolve at all.
2. **Verify the data cache**: `~/.cache/autoresearch/` needs `data/*.parquet` and
   `tokenizer/tokenizer.pkl` + `token_bytes.pt`. If absent, run `uv run prepare.py --num-shards 4`
   (the full download is far larger; 4 shards is enough and the laptop runs near-full on disk).
   The validation shard `shard_06542.parquet` is pinned and must be present.
3. **Agree the run tag with the human** and create `autoresearch/<tag>` — program.md requires the
   branch not already exist.
4. **Create `results.tsv` with the header row only**, and leave it untracked.

Then confirm and start. After confirmation program.md's **NEVER STOP** rule applies: do not ask
whether to continue.

## The one failure mode this environment adds

program.md keeps a **single agent session running across hundreds of experiments**. In this repo
that exact pattern hit 100% context mid-run twice, each time losing work that had to be salvaged by
hand — it is the whole reason `af-ticket-loop.sh` runs one fresh session per batch. The loop is
being run as written anyway, deliberately.

So know the signature: as context fills, the session starts re-reading files it already read,
re-deriving decisions already in `results.tsv`, and eventually dies mid-experiment with the branch
advanced but the row unlogged. **`results.tsv` is the durable artifact — it is untracked, so it
survives every `git reset` the loop performs, and it is what a fresh session reads to resume.**
On resume: read `results.tsv` first, `git log --oneline` second, and continue from there rather
than restarting the run.

Do not "fix" this by wrapping the loop in a driver script unless the human asks. It changes
Karpathy's design, which is the thing being evaluated.

## Authoring an af-build ticket that calls for a research run

A normal ticket has a binary acceptance condition. A research ticket does not — program.md's loop
is open-ended by construction and stops only when a human stops it. The bridge is that af-build
requires the acceptance signal to be **external**, and `results.tsv` is exactly that: the loop writes
it, and it cannot be argued into passing.

The check is `agent_factory/scripts/checks/af_ml_research_target.py`. Pin it as the ticket's
validation `run` command:

```
python3 agent_factory/scripts/checks/af_ml_research_target.py \
  --results ~/Documents/official_repos/autoresearch/results.tsv \
  --min-experiments 20 --min-improvement 0.02 \
  --max-experiments 200 --budget-exhausted-ok
```

Exit 0 = accepted, 1 = not met (regress, keep running), 2 = malformed ledger.

Pick the goal deliberately — the two forms encode different research bets:

- `--min-improvement D` — "beat the baseline by D". The honest framing for most research tickets,
  because nobody knows the reachable number in advance.
- `--target-bpb X` — "reach X". Only when a specific number actually matters.

**`--budget-exhausted-ok` is the decision that matters most, and it has no safe default.** Without
it, a ticket that never finds an improvement can never close, and it wedges the entire build set
behind af-build's completeness gate — the loop is not guaranteed to succeed, and an indefinitely
open ticket is a real failure mode, not a hypothetical. With it, "we ran 200 experiments and the
best was X" closes the ticket as a legitimate research outcome. Choose per ticket and say which.

`--min-experiments` exists so a ticket cannot pass on a lucky first row; the baseline row is always
row one, per program.md.

Two constraints on the ticket body:

- The run happens in the **autoresearch checkout**, not in a praxis worktree. An af-build worker
  operating in its own throwaway worktree must point `--results` at the checkout's absolute path.
- Because the loop never self-terminates, the ticket describes **a bounded research campaign**
  (a tag, a budget, a goal), not "run autoresearch". Whoever closes the ticket stops the loop.

## Never

- Never run the loop from inside the praxis working tree — discard does `git reset --hard`.
- Never commit `results.tsv` (program.md step 7).
- Never let `train.py` output into the session — always `> run.log 2>&1`, then grep. Flooding
  context with training logs is what kills the session early.
- Never change `EVAL_TOKENS`, `TIME_BUDGET`, `MAX_SEQ_LEN`, or `evaluate_bpb` mid-run.
- Never report a local `val_bpb` as comparable to a GPU or published number.
- Never push, and never open a PR — the run's artifact is a local branch plus `results.tsv`.
