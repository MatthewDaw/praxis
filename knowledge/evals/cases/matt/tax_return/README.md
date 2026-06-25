# Tax-return evals (Matt)

Goal: prove the contract **"an external agent can offload its knowledge layer to
Praxis."** A hackathon-style tax-filing assistant should only have to drop its
documents in and ask for context; everything KG-shaped (distillation, dedup,
retrieval ranking) stays inside Praxis. This eval exercises that whole pipeline
on real TY2025 tax material: **raw 1040 instructions + W-2(s) + intake Q&A →
ingest/distil → retrieve → the agent fills the return → graded.**

## Design

Each scenario is a **full-pipeline case** (`component: null`):

1. **Raw ingestion.** The shared `sources/form_1040_instructions.txt` (line-by-line
   rules, TY2025 standard deductions, single/MFJ/HOH brackets) plus the scenario's
   own W-2(s) and intake Q&A (written into `sources/<slug>/`) are fed in raw
   through `seeded_insight.via_ingestor`. The ingestor distils them into the
   knowledge graph. We do **not** hand-write pre-distilled facts.
2. **A generic prompt is the query.** The `seed_prompt` just says "fill out the
   return" — it names no numbers. The agent must pull the W-2 amounts, the filing
   status / deduction choice from the Q&A, and the rules + brackets from the
   instructions out of the ingested knowledge, then do the arithmetic itself and
   write the completed return to `answer.md`.
3. **Grading.** `deterministic_checks` (`output_nonempty` + comma-optional
   `regex_matches` for total income, standard deduction, taxable income, computed
   tax, withholding, and the refund/owe bottom line, plus a `regex_absent` guard
   against fabricated credits) assert the figures land. A `rubric` grades
   grounding / arithmetic / completeness / honesty.

So the chain under test is: **raw docs → ingest/distil → retrieve → agent fills
the 1040 → graded.**

## Source of truth

All arithmetic is computed once, in `_generate.py`, so the deterministic-check
regexes and the rubric's arithmetic criterion can never drift from the canonical
figures. To change a scenario or the rules, edit `_generate.py` (or
`sources/form_1040_instructions.txt`) and re-run it — **do not hand-edit the
generated `case.yaml` files.**

```powershell
uv run python knowledge/evals/cases/matt/tax_return/_generate.py
```

## Scenarios (TY2025)

| Case (folder) | Filing status | Wages / withheld | Std deduction | Taxable | Tax | Bottom line |
|---|---|---|---|---|---|---|
| `single_w2` | Single | 40,000 / 3,200 | 15,750 | 24,250 | 2,672 | **refund 528** |
| `mfj_two_w2` | MFJ (two W-2s) | 40,000 + 35,000 = 75,000 / 5,800 | 31,500 | 43,500 | 4,743 | **refund 1,057** |
| `single_owes` | Single | 90,000 / 9,000 | 15,750 | 74,250 | 11,249 | **owes 2,249** |

- `single_w2` mirrors the hackathon taxpayer profile (single, one W-2, ~$40k/yr).
- `mfj_two_w2` forces the agent to aggregate two W-2 Box 1 amounts.
- `single_owes` is under-withheld, so it must land an **amount owed**, not a refund.

## Retrieval recall case (`retrieval_recall/`)

`matt_tax_return_retrieval_recall` is a `component: graph_reader` case. It
re-ingests the single-filer's docs and queries with a fill-the-return question,
then asserts the reader output surfaces the salient facts an agent needs —
wages (40,000), withholding (3,200), the 15,750 standard deduction, the `single`
filing status, and the "standard deduction" concept. This grades the **retrieval
set itself** (recall), not only the final answer, so retrieval can't be tuned on
answer quality alone.

## Bracket-collision recall case (`bracket_collision/`)

`matt_tax_return_bracket_collision_recall` is a `component: graph_reader` case
that **captures a real distillation failure** found by auditing the live graph.
When two filing statuses share the same numeric bracket range, Praxis's
near-duplicate / overwrite-conflict pass collapses them and **silently rejects**
one — with no contradiction flagged. In TY2025 the Married-filing-separately
ladder shares the 12% / 22% / 24% / 32% lower-bracket boundaries with Single
("MFS thresholds match Single except in the top two brackets"). In the observed
run, distillation kept all seven MFS brackets but dropped the **Single 12%
($11,925–$48,475), 24% ($103,350–$197,300), and 35% ($250,525–$626,350)** facts,
plus the computation rule **"sum the tax from each bracket"** — the 12% gap is
exactly the bracket the flagship single-filer/~$40k case needs.

The case ingests the Single ladder, MFS ladder, and computation rule as separate
docs, runs them through the **full live-like write policy** (`merge_model` +
`conflict_model` wire the LLM merge judge and the claim/semantic conflict
detectors — the reject-the-loser path), then asserts the reader surfaces each
Single bracket as a **distinct, status-labelled** fact, that the
**sum-of-brackets rule** survives, and that the **MFS twin survives alongside**
the Single one. Each check binds the *filing-status label* to the rate + bound,
because the bracket *numbers* survive via the MFS twin even when the Single fact
is dropped — only a label-bound check catches the silent rejection.
`recall_single_32pct_control` anchors a Single bracket whose range is unique.

### Status: GREEN — and what that revealed

The case currently **passes**. The canonical write policy with `gpt-4o-mini`
judges does the right thing: it keeps all 14 brackets distinct. The live
silent-rejection **could not be reproduced** through this offline path — a
combined doc, sequential docs, the full conflict+merge policy, and a double
re-seed were all tried, and every variant keeps the brackets. So the live drop
comes from something this case does not yet replicate. Two leading suspects:

1. **Deployed judge config.** The running serve may use a different judge model
   or a looser dedup/merge threshold than `gpt-4o-mini`, so a clash the eval
   judge keeps, the live judge collapses.
2. **Full-corpus collision pressure.** The live graph was seeded from the whole
   26-doc `rules.py` set (standard deductions where Single == MFS == $15,750,
   every status's brackets, W-2 + return facts) — far denser near-duplicate
   pressure than these three docs.

So this is committed as a **regression guard** that encodes the correct
expectation and exercises the real write policy. To turn it RED against the
actual defect, the next step is to seed the real `rules.py` `RULE_DOCUMENTS`
corpus through this same policy, and/or pin the case's judge model/threshold to
the deployed serve's. (An alternative is an integration test that drives the live
`/ingest` with `auto_resolve` and asserts the Single brackets are not rejected.)

## Full-pipeline knobs (per scenario)

Copied from `matt/applications`, with the same rationale:

- `substrate: vector` — real `VectorGraph` write policy (redact/dedup), not the
  in-memory stub.
- `embedder: cached` — committed real vectors that replay offline.
- `ingest_model: openai/gpt-4o-mini` — real LLM distillation, not passthrough.
- `ingest_state: active` — distilled facts land active + retrievable (the default
  `proposed` would be gated out of the reader).
- `reader: retrieving`, `reader_top_k: 0` — rank all active facts against the
  prompt with the volume cap off, so facts from all three docs compete together.
- `needs: [file_io]` — the agent writes `answer.md`; checks grade that text.
- `target_commit: 0*40`.

## Run

```powershell
# one scenario, real Claude Code — ingests, fills answer.md, grades
uv run python -m knowledge.evals.run matt_tax_return_single_w2
```

The full-pipeline scenarios need a file-producing agent (`needs: [file_io]`), so
the offline `--fake` backend **skips** them (there is no agent to write
`answer.md`). The `graph_reader` retrieval_recall case has no `file_io` need and
grades reader output directly.
