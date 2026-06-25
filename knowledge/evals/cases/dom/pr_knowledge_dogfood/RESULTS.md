# RESULTS — PR-knowledge dogfood experiment

**Verdict (R8 go-gate): NO-GO — *not proven yet; iterate the apparatus*, not *knowledge doesn't help*.**
The strict gate (first-pass token/turn + footgun-flip) fails on apparatus weakness, but the
[cost-to-correct](#cost-to-correct--the-first-pass-metric-understates-the-control) measurement points
GO-ward — knowledge-upfront was cheaper than knowledge-as-rework on every task measured.

Run: 6 cases × 3 trials (18 real Claude Code sessions, subscription, `whole_file` reader, sealed box,
no repo, Bash/web disallowed). Raw per-trial numbers: [RESULTS.data.json](RESULTS.data.json). Re-run
with `uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3`.

## Per-task results

| Task | Type | Footgun flip | Treat tokens | Control tokens | Token Δ | Treat turns | Control turns |
|------|------|:---:|---|---|---|---|---|
| `umap_neighbors`  | footgun (strong) | ✅ **yes** | 1204 ± 10 | **2827 ± 504** | **−1623** | 4.3 | 6.0 |
| `phoenix_tracing` | footgun (invalid) | ❌ no | 1274 ± 80 | 975 ± 73 | +299 | 5.0 | 3.0 |
| `supersedes_edge` | convention | ✅ yes | 1535 ± 134 | 949 ± 263 | +586 | 5.0 | 5.0 |

- **Footgun "avoid" rates** (treatment avoided / control exhibited the footgun, over 3 trials each):
  `umap_neighbors` 3/3 vs 2/3 · `phoenix_tracing` 3/3 vs **0/3** · `supersedes_edge` 3/3 vs 3/3.
- "Tokens" = `input_tokens + output_tokens`, but the appended-knowledge block is **cache-read** and not
  counted in `input_tokens` (~tens of tokens/run), so the figure reflects **output volume**, not full
  prompt cost. See limitations.

## What each task showed

### `umap_neighbors` — a clean, textbook win (both signals)

The one task where the footgun was genuinely blind-tempting **and** the curated fact was decisive:

- **Flip:** all 3 treatment trials lowered `n_neighbors` off 15; 2 of 3 control trials left it at 15
  (the 3rd found the fix blind). The agent avoided a documented footgun it otherwise hits.
- **Cost:** the control burned **~2.3× the output tokens** (2827 vs 1204) and more turns (6.0 vs 4.3)
  flailing through the clustering code without the fact — one blind trial ran 9 turns / 2807 tokens and
  *still* didn't fix it. With the fact, the agent went straight to the one-line change.

This is exactly the "watched footgun-avoidance **and** measurable token/turn reduction" R8 asks for.

### `phoenix_tracing` — an invalid footgun construct (proves nothing)

The control exhibited the footgun **0 / 3 times**: every blind agent already placed `setup_tracing()`
at module-import scope. The "put startup wiring in `__main__`" trap simply isn't tempting to a competent
agent here. So there was no footgun for the treatment to "avoid," and no flip. The treatment also cost
*more* (1274 vs 975 tokens; 5 vs 3 turns) — on an already-easy task, the 13-fact block is pure overhead.

This is a **measurement-construct failure flagged in advance** (see the README validity caveat), not
evidence about knowledge value. The positive-assertion control surfaced it loudly as 3× control FAIL;
under the plan's original `xfail` control these would have been silent XPASSes masquerading as wins.

### `supersedes_edge` — knowledge corrects behavior, and is cheaper *to-correct*

Knowledge reliably installed the project's `supersedes` edge name (treatment 3/3; control 0/3 — blind
agents chose `replaces` / `replaced_by` / etc.). A clean **correctness** flip. On *first-pass* tokens the
treatment looked more expensive (1535 vs 949). **But that comparison is unfair** — the control's cheap
first pass produced a *wrong* artifact, whose cost is the downstream review-and-fix loop, which the
first-pass number bills to nobody. Counting it flips the result: see *Cost-to-correct* below.

## Cost-to-correct — the first-pass metric understates the control

The 18-run table measures **first-pass** tokens. But a control that produces a *wrong* artifact isn't
done — the error gets caught and fixed, and that rework is a real cost the first-pass number hides. The
honest comparison is **cost-to-correct**: knowledge delivered *upfront* (treatment, one pass) vs the
*same* knowledge delivered *late* as review feedback (control's wrong first pass **+** a corrective
fix turn). Measured directly (`rework_cost.py`, 2 trials/task; the fix turn mounts the control's wrong
file and supplies the fact as review feedback):

| Task | Treatment (upfront) | Control first-pass | + rework to fix | **Control total** | Winner |
|------|---:|---:|---:|---:|---|
| `supersedes_edge` | **1862** | 935 / 857 | +1314 / +1624 | **2249 / 2481** | treatment ~20–30% cheaper |
| `umap_neighbors`  | **1496** | 2409 / 3169 | +895 / +662 | **3304 / 3831** | treatment ~2.2–2.6× cheaper |

Every fix turn succeeded (the late fact fixed the artifact), so this is a fair like-for-like: same
knowledge, only the *timing* differs. **Once rework is counted, knowledge-upfront is cheaper on every
task measured** — including `supersedes_edge`, which the first-pass metric had scored as knowledge
*costing more*. The first-pass token comparison is not just noisy in the sealed box; it is **biased
toward the control**, because it credits the control's wrong output as if it were free.

Reproduce: `uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/rework_cost.py`.

## Diagnosis (per plan U5: weak curation vs. genuinely-unhelpful knowledge?)

**Neither.** The gap is not weak curation (the facts are accurate and the neutralizing facts were
present and injected — verified) and not genuinely-unhelpful knowledge (where the construct was valid —
`umap_neighbors` — knowledge was *decisively* helpful on both signals). The NO-GO is driven by two
**apparatus** weaknesses:

1. **Two of three task constructs were weak.** `phoenix_tracing` was not blind-tempting (invalid
   footgun); `supersedes_edge` measures output correctness, not cost. Only `umap_neighbors` was a valid
   dual-signal footgun — and it cleanly passed.
2. **The first-pass token metric is biased toward the control (now measured, not just suspected).**
   The parent proposal's savings lever is *fewer repo-exploration turns*, but these cases mount no repo
   and forbid Bash/web — there is little exploration to shortcut, so absent a footgun-induced flail
   (umap) the injected fact block tends to *add* first-pass output rather than remove it. Worse, the
   first-pass count credits the control's *wrong* output as free: the cost-to-correct measurement above
   shows knowledge-upfront is actually **cheaper on every task once rework is counted** — the gate's
   metric scored `supersedes_edge` exactly backwards. Re-score on `cost_usd` / `num_turns` /
   footgun-flips / cost-to-correct.

## Recommendation

**Do not start the ingestion pipeline on this result — but do not kill the bet either.** Iterate the
apparatus, then re-decide:

- Replace `phoenix_tracing` with a second *genuinely blind-tempting* footgun (validity-gate it: confirm
  control exhibit-rate ≥ ~2/3 before counting it).
- Add at least one **repo-mounted** task (clone a real Praxis subdir, allow Read/Grep) so the
  exploration-savings lever the parent proposal actually bets on is exercised — the sealed box can't
  show it.
- Make **cost-to-correct** the primary cost metric (control first-pass + rework vs treatment), with
  `cost_usd` for the per-run figure (it captures cache-read input the token count misses) and
  `num_turns` / footgun-flip as co-signals. The first-pass token count is biased toward the control and
  should not gate the decision.
- Keep `umap_neighbors` as the proven-valid template for what a discriminating footgun task looks like.

## Limitations

- n = 3 trials/arm; the `umap` dual-signal is one valid task, not a suite. `umap` control variance is
  high (σ ≈ 504 tokens) — directionally unambiguous here, but a re-run should raise the trial count.
- `input_tokens` excludes the cache-read knowledge block, so "tokens" ≈ output volume (see above).
- No rubric judge was used (deterministic footgun/convention checks + token/turn carried both signals),
  so output *quality* beyond the specific checked pattern is unmeasured.
