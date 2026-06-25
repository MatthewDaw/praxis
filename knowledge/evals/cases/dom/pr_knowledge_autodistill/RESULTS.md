# PR-knowledge auto-distill slice — results

**Run:** 2026-06-25 · 2 tasks × 3 trials × {auto, control, (+curated on the gating task)} =
15 real Claude Code runs · distiller `openai/gpt-4o-mini`, embeddings
`openai/text-embedding-3-small` (replayed from the committed cache).

> **Correction (2026-06-25).** A first run reported GO (provisional). Code review found that
> the autodistill case ids collided with the sibling dogfood suite, so `load_cases()` silently
> shadowed the retrieving auto arm with the dogfood whole-file arm — the first run measured the
> *wrong* case. Ids are now namespaced (`autodistill_*`, guarded by a uniqueness test) and the
> slice was re-run on the genuinely-retrieving auto arm. The numbers and verdict below are the
> corrected run; the headline flipped from GO to NO-GO.

## Verdict: **NO-GO**

The gating footgun **flipped** — auto-distilled-and-retrieved knowledge let the agent avoid the
`yoyo` footgun every trial while the blind control hit it every trial, with the fact both
extracted *and* retrieved 3/3. But the gate also requires a token/turn reduction on a majority of
tasks, and that failed: retrieving from the 154-fact corpus is net cheaper only when the footgun
actually traps the blind agent. On `umap`, whose control was never trapped (0/3), the retrieved
context was pure overhead and the auto arm cost **more**.

## Numbers

| Task | Arm | tokens (mean) | turns | footgun avoided |
|------|-----|---------------|-------|-----------------|
| **`yoyo_lazy_import`** (gating) | auto | **2009** | 8.0 | 3/3 |
| | control | 2326 | 7.3 | 0/3 (exhibited **3/3**) |
| | curated ceiling | — | — | 3/3 |
| **`umap_neighbors`** (cost-signal) | auto | 3211 | 5.7 | 3/3 |
| | control | 2750 | 4.3 | 3/3 (exhibited **0/3**) |

- **Gating flip = True:** auto avoid-rate 1.00; control exhibit-rate **1.00** (≥ the 2/3 bar). The
  curated ceiling also flipped (`curated_flip = True`).
- **Token delta:** yoyo auto −317 (−14%, cheaper); umap auto **+461 (+17%, more expensive)**.
  Majority-reduction criterion fails (1/2) → **NO-GO**.

## R7 attribution

Both R7 inputs are green for **both** tasks: the neutralizing fact was **extracted** into
`facts.insights.json` and **surfaced by the retriever 3/3** (semantic-only, top-15 of 154). So the
three-way shortfall machinery (EXTRACTION / RETRIEVAL / KNOWLEDGE-VALUE) stays dormant — there is
no retrieval or extraction gap to attribute. **The NO-GO is a cost finding, not a knowledge
finding.**

Two distinct signals, pulling apart:

1. **Knowledge value — positive.** On the one footgun that genuinely traps a blind agent
   (`yoyo`, control 3/3 exhibit), auto-distilled-and-retrieved knowledge neutralized it 3/3, *and*
   the curated ceiling confirms the knowledge itself is the cause (curated also flipped). Extraction
   and retrieval both work end-to-end.
2. **Cost — mixed, and that's the honest result.** Injecting retrieved context is not free. It
   pays for itself only when it prevents flailing. `yoyo`: blind control flailed → knowledge saved
   net (−14%). `umap`: blind control already solved it cheaply (0/3 exhibit — not blind-tempting) →
   knowledge added 154-fact retrieval overhead for no avoidance benefit → +17%.

This is exactly the dynamic the dogfood cost-to-correct lesson predicted: the token benefit is
real **only on a footgun that actually bites**. `umap`'s demotion to a non-gating cost signal was
correct, and its cost regression here is informative, not a defect.

## Caveats

1. **One gating footgun.** `yoyo` is the only validated gating construct; its control exhibit-rate
   was a clean 3/3 this run (vs the borderline 2/3 the shadowed run showed). Still thinner than the
   ≥2 the dogfood lesson recommends.
2. **Cost is corpus-size-sensitive.** Auto seeds the full 154-fact corpus; the retriever returns
   top-15. A smaller or better-scoped corpus could cut the umap overhead — but tuning the corpus to
   pass the gate would be measurement-gaming, not a finding.
3. **n = 3.** The flip is clean (3/3 vs 3/3) but the sample is small.

## Follow-up analysis — the umap cost IS a ranking gap (corrects the read above)

An offline rank probe over the seeded graph shows the umap regression is **not** over-injection —
it is a **ranking** failure that semantic-only retrieval can't fix:

| Task | neutralizing fact rank (semantic-only) | what out-ranks it |
|------|----------------------------------------|-------------------|
| `yoyo` | **#2** of 8 injected | one `repo`-scoped distractor ("single source of truth") |
| `umap` | **#6** of 8 injected | **5 `repo`-scoped clustering distractors** (cluster-id stability, super-nodes, dashboard restructure) score 0.47–0.50 vs the n_neighbors fact at 0.42 |

So the agent must inject the 5 higher-scoring distractors to reach the rank-6 fact — *that* is the
over-injection cost, and **tightening `top_k` would make it worse** (top_k<6 drops the umap fact
entirely). The relevant fact is simply out-ranked.

**The facts already carry the scope to fix it.** The umap fix is
`scope=file:knowledge/knowledge_graph/clustering.py` and the task edits `clustering.py`; the yoyo
fix is `scope=file:migrations/...` and the task writes a migration — while every distractor is
`scope=repo`. An offline scope-match boost (+0.15 for a file-scoped fact whose path matches the
task target) re-ranks the neutralizing fact **#6 → #2 (umap)** and **#2 → #1 (yoyo)**, surrounding
it with other clustering-/migration-scoped facts instead of repo chatter. With scope-aware ranking
+ a tight `top_k`, the auto arm would inject ~2 relevant facts instead of 8 — the lever that
addresses the cost without dropping the fact.

**This is the retrieval-attributed evidence the parent proposal was waiting for.** Earlier this
doc said retrieval "did not bite" and the cost argued against richer ranking — both wrong: umap
*is* a ranking shortfall, and scope-aware retrieval (the parent's deferred two-lane work) is now
**evidence-motivated**, not speculative.

## Second footgun — VALIDATED (3/3 blind)

A second gating-footgun candidate, **`delete_active_guard`**, was validated empirically (control in
`delete_active_guard_before/`): asked to "write a function that deletes a fact by id," the blind
agent wrote the unguarded `DELETE FROM facts WHERE id = %s` — **3/3 trials**, no state guard. This
is a real data-loss footgun (deleting an `active` fact), it carries the same clean check shape as
`yoyo`, and its neutralizing fact is already in the frozen artifact
(`scope=file:knowledge/serve/facts_candidates.py`: *"Deletion of facts is gated to only 'proposed'
or 'rejected' states…"*). At 3/3 it clears the ≥2/3 validity bar — **a genuine second validated
construct**, which is exactly what the single-footgun caveat called for.

Completing it into a gating arm (so the gate rests on **two** validated footguns, not one) is the
next metered step: add its `auto` (retrieving) + `_curated` arms, refresh the embed cache for the
new task query (a current cache miss — only the yoyo/umap queries were embedded), then re-run the
slice across both footguns.

## Next increment

- **Promote `delete_active_guard` to a 2nd gating arm** (validated above) — the highest-value
  strengthening, and now de-risked (the construct is empirically confirmed, not invented blind).
- **Scope-aware retrieval is now justified by data** (the rank probe above). It is the parent
  proposal's next slice and needs real work — write-path provenance threading so the store carries
  `scope` (this slice seeds text-only), plus a scope-aware reader/boost. Warrants its own plan; an
  eval-side scope reader could confirm the offline demo on real agent runs first.
- **Add a second validated footgun** so the knowledge-value signal doesn't rest on one construct —
  empirical and metered (validate a candidate's blind control ≥2/3 before trusting it; do not
  invent one blind).
- **Establish the dogfood premise** (≥2 reliably-blind-tempting footguns) before any GO here is
  trustworthy.

*Raw records, diagnostics (incl. `surfaced_trials`), per-arm aggregates, and gate in
`RESULTS.data.json`.*
