# Intent-aware ingestion & gated retrieval — design spec

Status: **proposed** (acceptance tests live as `xfail` cases under
`knowledge/evals/cases/matt/intent_gating/`). This documents the target; nothing
below is wired yet.

## The problem

The ingester writes only `raw_text`; the reader returns facts by pure cosine
similarity. So autobiographical facts (gauntlet/acme/employer experience) surface
indiscriminately — including when the user is just discussing a topic in the
abstract. We want experience to surface when **filling an application** or
**talking about my experience**, and stay quiet when the situation is general
knowledge — even when the *topic* overlaps.

## The one reframe everything hangs on

Split two axes and never let them touch:

| Axis | Question | Where it lives | Type |
|---|---|---|---|
| **Topic** | what is this fact *about*? | the embedding (already there) | continuous similarity |
| **Situation** | what am I *doing* when I'd want it? | intent tags | discrete filter |

The tagger is **forbidden from emitting anything topical** ("RAG", "embeddings",
"AI engineering"). Topic is the embedding's job and it is already good at it.
Tags encode *only situation-of-use*. A gauntlet RAG project is *about* RAG
(vector handles that) and is *for* `job_application` + `personal_narrative` (tags
handle that). This is why "explain how attention works" stays clean: it is
topically close to Matt's transformer work (vector lights up) but its **situation
is general knowledge**, which no autobiographical fact is tagged for. Topic
match, situation mismatch → suppressed. The leakage is structurally impossible
because the tagger cannot emit the topical tag that would cause it.

## Situation vocabulary (closed, versioned)

`vocab_version: "v1"`. Situations describe what the *user is doing*, never what
the fact is about. Start tiny; grow by bumping the version (which triggers a
re-tag, see below).

| situation | the user is… | example query |
|---|---|---|
| `job_application` | filling out / drafting an application answer | "Describe a production LLM pipeline you built." |
| `personal_narrative` | telling their own story / experience / bio | "Walk me through your background." |
| `coding_constraint` | doing work that must respect a learned rule/preference | "How do we handle migrations in this repo?" |
| `general_knowledge` | asking about a topic in the abstract | "How do two-tower retrieval models work?" |
| `other_personal` | **catch-all** for autobiographical facts whose situation isn't yet in the vocab | — |

`other_personal` is a **safety valve, not optional**: it guarantees no
autobiographical fact is ever situation-less and therefore silently invisible
(see Tier 2 below).

## Autobiographical band (discrete, not a float)

LLMs don't produce calibrated `0.0–1.0` scores reproducibly, so we ask for a
**labeled band with anchors**, and cross-check it against provenance (`source`).

| band | anchor | retrieval treatment |
|---|---|---|
| `lived_action` | "I personally did / built / shipped X" | **gated** — only surfaces when the situation matches its tags |
| `held_opinion` | "my take / preference / how I like to work" | gated by default (configurable to always-eligible) |
| `world_fact` | "generally true, not about me" | **always eligible** — never gated |

**Provenance cross-check (a second, non-LLM signal):** a `resume`/`linkedin`
source biases toward `lived_action`; a pasted article or docs page biases toward
`world_fact`. When the LLM band and the source-prior disagree, set
`flags: ["band_review"]` rather than silently guessing. When `source` is null
(common today), fall back to the LLM band alone — no worse than current behavior.

## `meta.intent` schema

Stored in the existing (currently unused) `facts.meta` jsonb column — **not** the
scalar `category`/`scope` columns (those stay for the orthogonal coding-agent
taxonomy). Storing in `meta` keeps intent a *disposable projection*, not a schema
commitment.

```json
{
  "intent": {
    "situations": [
      {"name": "job_application", "weight": 0.9},
      {"name": "personal_narrative", "weight": 0.7}
    ],
    "band": "lived_action",
    "vocab_version": "v1",
    "tagged_by": "<model>@<sha256(raw_text + vocab_version)>",
    "source_prior": "lived_action"
  }
}
```

Three load-bearing properties:

1. **`situations` is a weighted list, not a scalar** — a fact honestly serves
   several situations. Retrieval filter is a jsonb-overlap, not `category = ?`.
2. **`band` is the gate control** — `world_fact` is never suppressed; only
   autobiographical bands can be. This is the over-recall control.
3. **`tagged_by` is a content hash** — re-ingesting identical `raw_text` is a
   cache hit. Evals stop being flaky. Determinism by construction.

### Why this survives a change of mind (the write-side risk)

Tags are a **pure function of durable `raw_text` + `vocab_version`**. They never
touch the embedding. So changing the situation model is one command:

```
re-tag-all:  bump vocab_version -> invalidate cache -> re-run tagger over stored raw_text
```

No re-embedding, no manual work. The thing that normally calcifies write-side
intelligence (frozen intent) is exactly what the topic/situation split un-freezes.
Backfill of existing facts is the *same* command.

## Tagger prompt (sketch)

Runs as a second step in `PromptIngestor.synthesis()`, once per distilled
insight. Output is structured (JSON schema enforced, like the rubric judge).

```
You assign RETRIEVAL INTENT to one personal-knowledge fact. You are NOT
summarizing or classifying the topic — the topic is handled elsewhere. Decide
only: in what SITUATIONS would the user want this fact retrieved, and how
autobiographical it is.

Rules:
- Pick situations ONLY from this closed list: job_application,
  personal_narrative, coding_constraint, general_knowledge, other_personal.
- NEVER emit a topic as a situation (not "RAG", not "machine learning"). If you
  are tempted to, the correct tag is general_knowledge.
- A fact may have multiple situations, each weighted 0..1 by how central it is.
- band: lived_action (the person did/built X) | held_opinion (their
  preference/take) | world_fact (generally true, not about them).
- If the fact is autobiographical but no listed situation fits, use
  other_personal so it is never lost.
- Bias toward SUPPRESSION when unsure whether a lived_action fact belongs to a
  situation: omit the situation rather than over-tag. Noise is the feared
  failure.

FACT: {insight}
SOURCE: {source}        # e.g. "resume", "linkedin", "pasted article", null

Return: {"situations": [{"name": ..., "weight": ...}], "band": ...}
```

The "bias toward suppression when unsure" line encodes the user's stated
preference (over-recall is the worse failure) directly into generation.

## Gated read (two-tier, no hard delete)

`praxis_get_context(query, situation=None)`. **Situation is a caller-asserted
input first, classifier-inferred only as fallback** — this removes the
single-point-of-failure for the high-value path (an application-filling agent
*knows* it is filling an application and declares `situation="job_application"`).
The NL classifier runs only for ambient/unknown calls, where the
suppress-on-uncertainty default is the safe direction.

```
situation = caller_asserted ?? classify(query)     # classify defaults to general_knowledge when unsure

Tier 1 (always eligible, ranked by cosine):
    band == world_fact                              # semantic: never gated
    OR situation in {s.name for s in fact.intent.situations}

Tier 2 (backfill ONLY when Tier 1 underfills top_k):
    autobiographical fact whose situation did NOT match, ranked by cosine
```

Tier 2 makes "invisible fact" impossible (a mis-tagged or `other_personal` fact
is demoted, not deleted) while keeping noise near-zero: it only fires when there
is nothing more relevant to show. Underfill threshold = fewer than `top_k`
Tier-1 hits above a fixed cosine floor — one constant, fails safe in both
directions.

The judgment surface collapsed to a few **enumerable** policy choices (which
bands are gated; the underfill floor), grid-searchable against the eval — not
continuous knobs to gradient-chase.

## Build order

1. **Distractor eval** (`cases/matt/intent_gating/`) — acceptance test; `xfail`
   today. This is the instrument that converts "gauntlet feels too loud" into a
   red number. Build first.
2. Situation vocab + bands (this doc) anchored to the `matt/applications` corpus.
3. Tagger writes `meta.intent` (source-cross-checked, hash-cached).
4. Two-tier gated reader + caller-asserted `situation` threaded through
   `praxis_get_context` and `/context`.
5. Grid-search the gating policy against the eval; pick the one that nails the
   precision (no-leak) cases without failing the recall cases.

When 3+4 land, the `xfail` cases flip to **XPASS** — the harness's built-in
signal that a spec became a real capability.
```
