# Application-filling evals (Matt)

Goal: test that the system, after **ingesting Matt's raw profile** (resume,
LinkedIn, BYU ACME degree), can correctly fill out real job-application questions.

## Design

Each case is a **full-pipeline case** (`component: null`):

1. **Raw ingestion.** The actual source documents — `sources/resume.txt`,
   `sources/linkedin.txt`, `sources/degree.txt`, and `sources/gauntlet.txt` (the
   Gauntlet AI program page, https://gauntletai.com/apply) — are fed in raw
   through `seeded_insight.via_ingestor`. The ingestor distils them into the
   knowledge graph. We do **not** hand-write pre-distilled facts
   (`direct_to_graph`); the point is to exercise ingestion on messy real source text.
2. **The application question is the query.** The job posting itself is *not*
   ingested — only the question becomes the `seed_prompt`, which drives the boxed
   Claude Code agent to write its answer to `answer.md` using only the injected
   knowledge graph (Bash/WebFetch/WebSearch are disabled, so it can't look
   anything up).
3. **Grading.** `deterministic_checks` (case-insensitive `regex_matches` +
   `output_nonempty`) assert the filled answer drew the right facts from the
   ingested profile. A `rubric` grades grounding, relevance, specificity, and
   honesty (does it overclaim where Matt's experience only partially fits?).

So the chain under test is: **raw docs → ingest/distil → retrieve → agent fills
the application → graded.**

## Source of truth

All cases share the same four raw sources, so they live once in `sources/` and
are stamped into every `case.yaml` by `_generate.py`. To add or edit a question,
edit `COMPANIES` in `_generate.py` and re-run it — do not hand-edit the generated
`case.yaml` files.

```powershell
uv run python knowledge/evals/cases/matt/applications/_generate.py
```

## Companies & questions

### Hightouch — Software Engineer, AI Agents (`hightouch/`)
0→1 AI products, production LLM pipelines, agentic systems, backend scaling,
customer data-warehouse work, education, location/visa, TypeScript stack.

### Sekai — Senior ML Engineer, Search & Recommendations (`sekai/`)
Owned recsys/search/ranking system, embedding retrieval & two-tower models,
production ML pipelines, an end-to-end experiment, cold-start strategy, and
why-Sekai/remote fit.

> Note on transfer cases: several Sekai questions (two-tower, recsys ownership)
> and the Hightouch TypeScript question probe areas where Matt's experience is
> only a *partial* match. Those lean on the `honest` rubric item — a good answer
> maps the closest transferable experience and does not fabricate.

## Run

```powershell
# one case, real Claude Code (subscription) — ingests, fills answer.md, grades
uv run python -m knowledge.evals.run matt_hightouch_complex_ai_products_0_to_1

# all 14 application cases run through the default real backend
uv run python -m knowledge.evals.run $( \
  (Get-ChildItem -Recurse knowledge/evals/cases/matt -Filter case.yaml | \
   Select-String '^id:' ).Line -replace 'id:\s*','' )
```

These are full-pipeline cases that need a file-producing agent (`file_io`), so the
offline `--fake` backend **skips** them (no agent to write the answer). They grade
the answer text, mount no fixture, and run no code, so they don't require a full
`sandbox` — the structured backend can grade them too.
