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
