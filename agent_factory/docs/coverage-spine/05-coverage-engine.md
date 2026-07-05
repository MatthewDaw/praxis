# The Coverage Engine (judging at scale)

> Companion to [`00-overview.md`](00-overview.md). The shared engine behind the plan-repro
> eval (now) and the validation gate (later). Resolves: *how do you judge a target against an
> arbitrarily large, growing set of insights — robustly?*

## The model: per-part sweep + thorough per-part query
**No materialized matrix.** The harness:

> **for each PART of the target → thoroughly query everything related to that part → run the checks against that part.**

- **PART** = a unit of the target: a requirement/surface of a plan, or a module/file/endpoint of code.
- **Thorough query** = pull *everything related to that part* from the insight set — exhaustive
  for that part, not a top-k sample. The relevant insights come back regardless of which
  "angle" they represent; angles are implicit in what the query returns.
- **Run the checks** = judge each returned insight/check against the part.

Two simple properties give the completeness guarantee (replacing matrix bookkeeping):
1. **Systematic over parts** — the loop visits every part, so no part is skipped (the part list must be complete).
2. **Thorough per part** — the per-part query is exhaustive *for that part*, so no related insight is silently missed.

## Why it scales to thousands of insights
- Each part's judgment only ever sees *what's related to that part* — bounded context no matter how large the total insight set grows.
- Parts **fan out in parallel** (Workflow), each an independent bounded unit.
- **Adaptive pull count** within a part (pull down the relevance gradient until it drops off) — a sharp need returns one insight, a broad one returns several; never the whole set.
- **Cache** per-part judgments by `(part, related-insight-set hash)` so re-runs only re-judge what changed.

## Robustness (targeted adversarial)
Per insight/check applied to a part:
1. **Evidence-required match** — "covered/passed? you must quote the specific evidence (plan text / code / test); default to MISSING-or-FAIL if you can't." Kills the dangerous false-positive.
2. **Targeted adversarial confirm** — an independent refuter (N-of-M) runs *only* on claimed-pass items that are `derived` / critical / low-confidence. Rigor is concentrated where a wrong answer costs, not sprayed uniformly.
3. **False-negative guard** — for a judged hole/fail, widen the per-part query once before declaring it, so noise doesn't masquerade as a finding.

## Honest residual (what this does and doesn't guarantee)
- **Guaranteed:** every part is considered, and everything *known to be related* to each part is applied to it. This closes the retrieval-completeness gap (G1) — *given* the per-part query is genuinely thorough.
- **Not guaranteed:** that the insight set itself is complete (you can't apply an insight nobody has added). That residual is carried elsewhere — the **plan-repro eval** measures the hole rate vs. the golden, and the **closed loop** turns every escaped hole into a new insight.

## Shared engine signature
```
coverage(parts, related_query, item_evaluator) -> per-part report -> holes/fails
```
- **Eval instantiation (now):** parts = plan requirements/surfaces; related_query = related golden features; item_evaluator = evidence-required semantic match (+ targeted adversarial on derived).
- **Validation instantiation (later):** parts = code modules/surfaces; related_query = related Praxis validation checks; item_evaluator = run-test / agent-eval (+ targeted adversarial on critical).
- Remediation stays in each skill (planner: ask/expand; coder: test/fix).

## Deferred / outstanding
- **Part enumeration for code** (modules/files/endpoints) — harder than for a plan; belongs to the validation thread.
- **Insight tagging** (`applies_to`, `angle`/category, `severity`) — the per-part query is only as good as the tags; needs a convention. `severity` feeds the targeted-adversarial selection.
- **The exhaustive per-part retrieval query** (G1) — promoted from optional to **needed**: "thorough" over thousands of insights requires a complete `related-to(part)` retrieval, not semantic top-k. See [`01-praxis-changes.md`](01-praxis-changes.md).
