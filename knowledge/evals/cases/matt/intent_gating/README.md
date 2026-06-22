# Intent-gating evals (distractor / no-leak)

The **acceptance test** for intent-aware ingestion + gated retrieval. Design spec:
[`knowledge/injestion/INTENT_ENCODING.md`](../../../../injestion/INTENT_ENCODING.md).

## What these test (and how they differ from `applications/`)

The `applications/` suite grades the **final answer** an agent writes. These grade
the **retrieval set itself** — did the right facts surface, and did the wrong ones
stay hidden? That distinction is the whole point: you cannot tune intent-gating
against answer-quality alone, because a smart agent can write a fine answer while
the retriever quietly hands it the wrong context.

Each case is a `component: graph_reader` case (no agent, no sandbox, no credit):

1. A small **mixed corpus** is ingested raw (`seeded_insight.via_ingestor`):
   autobiographical experience facts (Praxis, Gauntlet, BENlabs) **plus**
   general-knowledge facts on the *same topics* (two-tower models, RAG, cosine
   similarity). Same topic, different situation-of-use.
2. `seed_prompt` is the **situation/query** handed to `reader.read`.
3. Deterministic checks assert experience tokens are present (recall) or absent
   (no-leak).

| case | situation | expectation |
|---|---|---|
| `application_surfaces_experience` | application | experience **surfaces** |
| `narrative_surfaces_experience` | personal story | experience **surfaces** |
| `general_two_tower_hides_experience` | general knowledge | experience **hidden**, general two-tower fact kept |
| `general_rag_hides_experience` | general knowledge | experience **hidden**, general RAG fact kept |
| `general_embeddings_hides_experience` | general knowledge | experience **hidden**, general cosine fact kept |

## Why they're all `xfail`

Today the reader has **no situation gating** — it retrieves by pure similarity, so
a general-knowledge query on an overlapping topic pulls the autobiographical facts
too. The `*_hides_experience` cases therefore fail today, which is correct and
expected: they encode a capability that does not exist yet. The harness reports
these as `XFAIL` (expected red), not `FAIL` (regression).

When the tagger (`meta.intent`) and the two-tier gated reader land, these flip to
`XPASS` — the harness's built-in "a spec became a capability" signal
(`run.py:status_of`). **Promote them to real assertions then** (drop the `xfail`).

The recall cases (`*_surfaces_experience`) may already pass today; that asymmetry
is the signal — current retrieval has recall but no precision.

## Run

```bash
# all intent-gating cases (component cases run deterministically; the run loop
# still needs an embedder for the in-memory vector read)
uv run python -m knowledge.evals.run matt_intent_general_two_tower_hides_experience
uv run python -m knowledge.evals.run $(printf 'matt_intent_%s ' \
  application_surfaces_experience general_two_tower_hides_experience)
```

## Edit cases via the generator

`case.yaml` files are generated — edit [`_generate.py`](./_generate.py) (the
corpus, the queries, or `EXPERIENCE_TOKENS`) and re-run it, mirroring the
`applications/` convention:

```bash
uv run python knowledge/evals/cases/matt/intent_gating/_generate.py
```

## Harness note

`run_component`'s `graph_reader` branch was extended to ingest `via_ingestor`
before reading (previously it seeded only `direct_to_graph`, bypassing the
ingester). That makes a reader case able to exercise the real
*write-intent → gated-read* path. The change is additive — existing reader cases
that set only `direct_to_graph` are unaffected.
