# RESULTS — PR-knowledge dogfood experiment (v2)

**Verdict (R8 go-gate): NO-GO — but the bet is better-supported than v1.** A *valid* footgun
(`yoyo_lazy_import`) flipped cleanly, and the cost-to-correct metric favors knowledge on **3 of 4
tasks**. The NO-GO is driven by footgun-construct variance (`umap`) and one null (the repo-exploration
lever), not by evidence that knowledge is unhelpful.

Run: 4 tasks × 3 trials × (treatment + control + rework-when-control-wrong) on the real
`ClaudeCodeRunner` (subscription, `whole_file` reader, sealed box). Raw per-trial data:
[RESULTS.data.json](RESULTS.data.json). Re-run:
`uv run python knowledge/evals/cases/dom/pr_knowledge_dogfood/analyze.py --trials 3`.

## What changed from v1

v1 (commit history) was a strict NO-GO, apparatus-attributed. v2 acted on that diagnosis:

- **Replaced the invalid `phoenix_tracing` footgun** (its control never hit the footgun) with
  **`yoyo_lazy_import`**, grounded in the repo's real migration convention.
- **Added `repo_mounted_dsn`**, a repo-mounted task (real `db.py` in the box) to exercise the parent
  proposal's "fewer exploration turns" lever, which the no-repo sealed-box tasks can't.
- **Re-gated on cost-to-correct** (`cost_usd`: treatment vs control-first-pass + rework) instead of
  first-pass token volume, which v1 showed is biased toward the control (it credits the control's wrong
  output as free).

## Per-task results (cost-to-correct, USD)

| Task | Type | Footgun flip | Treat cost | Control cost-to-correct | Δ | Treat turns | Ctrl turns |
|------|------|:---:|---:|---:|---:|---:|---:|
| `umap_neighbors`  | footgun (variance-prone) | ❌ no | **0.046** ± .003 | 0.073 ± .023 | **−0.027** | 4.0 | 6.7 |
| `yoyo_lazy_import`| footgun (strong) | ✅ **yes** | **0.053** ± .006 | 0.091 ± .006 | **−0.038** | 3.3 | 4.7 |
| `supersedes_edge` | convention | — | **0.060** ± .003 | 0.086 ± .031 | **−0.026** | 7.0 | 4.7 |
| `repo_mounted_dsn`| quantitative | — | 0.045 ± .003 | **0.042** ± .000 | +0.004 | 3.7 | 3.0 |

Footgun exhibit rates (control exhibited / treatment avoided, 3 trials): `umap` **0/3** vs 3/3 ·
`yoyo` **3/3** vs 3/3.

## What each task showed

### `yoyo_lazy_import` — the clean win (valid footgun, both signals)

The footgun that v1's phoenix wasn't. All 3 control trials wrote a **top-level `from knowledge...`
import** (the universal Python instinct) — which raises `ModuleNotFoundError` under yoyo's loader; all
3 treatment trials deferred the import inside the step, as the curated fact instructs. A clean **flip**
(control exhibit 3/3, treatment avoid 3/3), and treatment was **cheaper to-correct** ($0.053 vs $0.091
once each wrong control is charged for its rework). This is the textbook dual-signal result, on a
footgun whose control reliably fires.

### `umap_neighbors` — footgun validity is variance-prone (demote), but cost still favors knowledge

This run the blind control lowered `n_neighbors` **every time** (exhibit 0/3) — so no flip. In v1 it
exhibited 2/3. The footgun is real but only sometimes blind-tempting; at n=3 the flip is a coin-flip,
which fails the validity discipline (exhibit-rate ≥ ~2/3). **Demote it** (or redesign the fixture so the
blind fix is less reachable). Notably, the **cost signal survives the non-flip**: the blind control
flailed far more even when it eventually got it right — one control trial burned 6995 tokens / 7 turns
to reach the same one-line fix the treatment made in 4 turns / 1098 tokens — so treatment was still
~37% cheaper to-correct.

### `supersedes_edge` — knowledge cheaper to-correct

Control chose the wrong edge name 2/3 (reworked); treatment used `supersedes` 3/3. Cheaper to-correct
($0.060 vs $0.086). Convention knowledge both corrects the artifact and pays for itself once the
control's rework is counted.

### `repo_mounted_dsn` — the exploration-savings lever did **not** show (honest null)

The one task that mounts real repo code (`db.py`). The hypothesis: a cold agent must *read* it to learn
the DSN convention, while the fact-equipped treatment skips that. It didn't pan out — treatment was
**not** cheaper ($0.045 vs $0.042) and took *more* turns (3.7 vs 3.0). One mounted ~100-line file isn't
enough read-cost for a 13-fact context block to save against; the treatment paid for the larger prompt
without a big enough reading shortcut. The lever may need a heavier mount (several files, answer not in
the obvious one) or may simply be marginal in a Read-only box.

## Diagnosis

- **The cost-to-correct metric works** and favors knowledge on 3/4 tasks — the v1 methodological fix
  landed. It is robust to the first-pass bias that scored v1's `supersedes` backwards.
- **A valid footgun now flips cleanly** (`yoyo`), which v1 never achieved — the bet's core "watched
  footgun-avoidance" signal is demonstrated on a reliable construct.
- **Two gaps remain, both apparatus:** (1) `umap`'s footgun is too variance-prone at n=3 to gate on;
  (2) the repo-exploration lever isn't demonstrated by a single-file mount.

## Recommendation

**Trending toward GO, not yet clean-green.** The bet now has a valid-footgun flip *and* a working
cost-to-correct signal favoring knowledge broadly — materially stronger than v1. Before declaring GO:

- **Demote `umap_neighbors`** to a non-gating cost task (keep it for the cost signal; stop counting its
  flip), and add **one more reliably-blind-tempting footgun** alongside `yoyo` so the gate rests on ≥2
  valid flips, not one.
- **Strengthen `repo_mounted_dsn`** (mount several files; make the answer require reading) or accept
  that the sealed Read-only box understates the exploration lever and test it in a repo-mounted agent
  with Grep/Bash instead.
- With those, re-run; if the cost-to-correct + ≥2 valid flips hold, that is a GO for the
  [auto-distill slice](../../../../../docs/proposals/2026-06-24-pr-knowledge-auto-distill-slice.md).

## Limitations

- n = 3 trials/arm. `umap` and `supersedes` control costs are high-variance (σ up to $0.03); `yoyo`
  is tight. A gating re-run should raise the trial count on the cost comparison.
- `cost_usd` is the per-run subscription cost the CLI reports; it captures the cache-read prompt the raw
  token count misses, which is why it (not first-pass tokens) is the gate metric.
- No rubric judge — deterministic footgun/convention checks + cost carried the signals; output quality
  beyond the checked pattern is unmeasured.
