---
date: 2026-06-25
updated: 2026-06-25
topic: ce-compound-praxis-ingestion-bridge
---

# Proposal: Bridge `/ce-compound` into Praxis as a session ingestion source

**Status**: Open / exploratory · **Gated on**: the dogfood retrieval gate going clean-green
(read side) — write side can start sooner · **Raised**: 2026-06-25 · **Source**: brainstorm ·
**Sibling**: [2026-06-24-ingest-commits-and-prs.md](2026-06-24-ingest-commits-and-prs.md),
[2026-06-24-pr-knowledge-auto-distill-slice.md](2026-06-24-pr-knowledge-auto-distill-slice.md)

## Idea in one line

`/ce-compound` and the [`CommitIngestor`](../../knowledge/injestion/injestor_variants/commit_injestor.py)
are the **same extractor reached from two triggers** — session-end (human-in-loop) vs. post-merge
(async). Make ce-compound a third ingestion *source* into Praxis's candidate lifecycle instead of a
parallel flat-file knowledge store, so the repo dogfoods its own product on the knowledge it
generates about itself.

## Problem frame

`/ce-compound` is Praxis rebuilt as a markdown convention. It captures a solved problem at session
end and writes a structured doc to `docs/solutions/`. Praxis captures durable lessons, gates them
through review, dedups/reconciles them in a graph, and makes them retrievable. Same loop, two
maturity levels:

| | `/ce-compound` | Praxis |
|---|---|---|
| Capture | session-end, human-in-loop | session-end (this proposal) *or* async PR distill |
| Store | flat markdown in `docs/solutions/` | graph-backed facts (pgvector) |
| Dedup / conflict | none | merge on near-dup, supersede on conflict |
| Governance | in-chat "is this right?" | `proposed → suggested → active` + dashboard gate |
| Recall | agent must `grep` | semantic `praxis_get_context` |
| Proof | none | eval harness (cold vs. injected) |

Two facts make this concrete rather than academic:

1. **`docs/solutions/` holds exactly one doc here, and it's a real ce-compound output.**
   [`gate-eval-experiment-plans-on-validated-footguns.md`](../solutions/conventions/gate-eval-experiment-plans-on-validated-footguns.md)
   was produced by a `/ce-compound` run in session `fd866322`. So there is almost nothing to migrate —
   *and* that one doc plus its source session is a ready-made validation pairing (see Validation below).
2. **The extractor already exists.** `CommitIngestor` distills a unit into typed `Insight[]` with one
   structured LLM call and writes each through `Ingestor.ingest(..., state="proposed")` — straight
   into the candidate lifecycle. ce-compound needs the *same* extractor with a session-shaped input
   and prompt; it does not need new write machinery.

The risk of doing nothing principled: **two stores that drift.** A flat-file lesson and a graph fact
covering the same gotcha will diverge, and there is no dedup/conflict policy across the boundary.
Pick one system of record.

## Why route through the candidate lifecycle, not the MCP shortcut

The obvious shortcut is `praxis_add_insight` from the [MCP server](../matt/MCP_SERVER.md). It works,
but it is the wrong pipe for ce-compound output, for three reasons:

1. **It bypasses governance.** `add_insight` treats the in-chat confirmation as the human gate and
   lands the fact `active` at `confidence = 1.0`. That skips the `proposed → suggested → active`
   lifecycle and the dashboard review — which is Praxis's headline differentiator over "memory."
   Routing ce-compound through `Ingestor.ingest(state="proposed")` instead means self-captured
   knowledge is *reviewable*, dedup'd, and reconciled like every other candidate. **Dogfooding the
   gate is the point.**
2. **Granularity mismatch.** ce-compound produces a six-section doc (Problem / What Didn't Work /
   Solution / Why / Prevention). `add_insight` and the graph's dedup/conflict/embedding all operate
   per *atomic* fact ("one self-contained sentence or two"). The doc must be **distilled into
   atomic insights**, not stored whole — which is exactly what `CommitIngestor.synthesis` already does
   for PRs.
3. **Provenance.** `add_insight` carries only a `source` string. The ingestion path stamps `source`
   onto every written fact and preserves the audit trail. The rich markdown doc becomes optional
   human-readable provenance, not the source of truth.

## The bridge sketch: a `SessionIngestor`

The bridge is a fourth `Ingestor` variant that mirrors `CommitIngestor` field-for-field — same typed
single-call shape, same precision-first drop-malformed parsing, same closed category set — differing
only in (a) the input it distills and (b) the distillation prompt's framing.

**Input.** ce-compound's Phase 1 already extracts the solved-problem narrative (the Solution
Extractor's sections). Render that into one document string — the analogue of `CommitIngestor`'s
rendered `PRDocument`. No new extraction; reuse what ce-compound already collects.

**Output.** `Insight[]` written `state="proposed"`, `source="session/<id>"`. The section→category
mapping is natural and lets the prompt target the high-signal parts directly:

| ce-compound section | `Insight.category` | `scope` it tends to land in |
|---|---|---|
| What Didn't Work | `rejected` | `file:` or `module:` |
| Prevention | `gotcha` | `file:` (the footgun's site) |
| Why This Works / decisions | `decision` | `module:` or `repo` |
| Conventions established | `convention` | `repo` |

```python
# knowledge/injestion/injestor_variants/session_injestor.py
"""Distill one solved-problem session into Insight[] with a single structured call.

The session analogue of CommitIngestor: same typed {text, scope, category} contract,
same precision-first parsing. Differs only in the distillation prompt's framing —
a debugging/solve narrative rather than a merged PR. Writes state="proposed" by
default (inherited from Ingestor.ingest), so session knowledge enters the candidate
lifecycle and the human gate, NOT the active store directly.
"""
from __future__ import annotations

import json

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm

_DISTILL_PROMPT = (
    "You are distilling durable engineering knowledge from one solved-problem "
    "coding session for an agent that will work in this repository later.\n"
    "The session narrative names a problem, what was tried and failed, the fix, "
    "why it works, and how to prevent recurrence. Extract ONLY knowledge that stays "
    "true after the fix ships: the root-cause lesson, the gotcha that caused it, the "
    "decision and its rationale, the convention it established, and approaches that "
    "were tried and explicitly rejected.\n"
    "IGNORE the play-by-play: which file was opened first, transient error text "
    "already resolved, and anything restating WHAT was done without WHY it matters "
    "going forward.\n"
    "For each insight return:\n"
    "- text: one self-contained sentence or two, naming its own subject (no pronouns, "
    "no \"this session\"), stating the durable fact and — for a decision or gotcha — "
    "why it holds.\n"
    "- scope: where the fact applies, as `file:<path>`, `module:<name>`, or `repo`.\n"
    "- category: one of decision | gotcha | convention | rejected.\n"
    "Prefer precision over recall: emit nothing rather than a vague or speculative "
    "fact. Return an empty list when the session carries no durable knowledge."
)

_CATEGORIES = ("decision", "gotcha", "convention", "rejected")

# Identical schema to CommitIngestor._SCHEMA — extract to a shared module so the
# two variants cannot drift. (insights[].{text, scope, category}, strict json_schema.)
_SCHEMA = {...}  # see commit_injestor._SCHEMA


class SessionIngestor(Ingestor):
    """Distill a solved-problem session into typed insights with one structured call."""

    def __init__(self, graph: KnowledgeGraph, llm: Llm) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(self, raw_input: str, *, source: str | None = None) -> list[Insight]:
        content = f"{_DISTILL_PROMPT}\n\nSESSION NARRATIVE:\n{raw_input}"
        raw = self.llm.complete(
            [ChatMessage(role="user", content=content)], response_format=_SCHEMA
        )
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []  # precision-first: non-JSON reply -> nothing distilled
        insights: list[Insight] = []
        for item in data.get("insights", []) if isinstance(data, dict) else []:
            text = str(item.get("text", "")).strip()
            if not text:
                continue  # drop malformed, keep well-formed siblings
            scope = str(item.get("scope", "")).strip() or None
            category = str(item.get("category", "")).strip() or None
            insights.append(
                Insight(raw_text=text, source=source, scope=scope, category=category)
            )
        return insights
```

The diff against `CommitIngestor` is the prompt and the class name. That is the whole point: the two
variants should be **near-identical**, with the `_SCHEMA` (and ideally the shared scaffolding of
`synthesis`) factored into one module so a future schema change touches both at once.

**Where the LLM call is made.** `synthesis` calls `Llm.complete`. The distillation runs **backend**:
ce-compound POSTs the rendered narrative to a session-ingest endpoint that runs `SessionIngestor` and
writes `proposed` candidates through the same `Ingestor.ingest` path as `CommitIngestor` — one
extractor in one place, matching the deployed split where distillation + writes are already
backend-side. (A client-side variant — distill in-session, POST the `Insight[]` — stays open as a
later option if keeping the narrative warm proves worthwhile; see Key Decisions.) Either way the
*destination* is the candidate lifecycle, and the markdown doc is provenance, not the record.

## How ce-compound changes

Minimal, and opt-in per repo (a Praxis-configured project):

- After ce-compound's Phase 1 extraction, POST the rendered session narrative to the backend
  session-ingest endpoint, which distills it via `SessionIngestor` and writes the resulting `Insight[]`
  as `proposed` candidates.
- Keep writing the markdown doc as human-readable provenance that links to its facts — the graph fact
  is the record, the doc is a non-authoritative log.
- The Discoverability Check that ce-compound runs against `CLAUDE.md` becomes "agents can reach Praxis
  via `praxis_get_context`," not "agents can grep `docs/solutions/`."

## Key decisions

- **Candidate lifecycle, not `add_insight`.** ce-compound writes `proposed` candidates through the
  `Ingestor` path so the human gate, dedup, and conflict reconciliation all apply. The MCP
  `add_insight` shortcut is reserved for deliberate, single-fact, in-chat approvals — not for
  bulk session distillation.
- **One extractor, one schema, two prompts.** `SessionIngestor` and `CommitIngestor` share the typed
  contract and `_SCHEMA`; they differ only in distillation framing. Factor the schema out so they
  cannot drift.
- **Distill to atomic insights.** The doc's sections map to categories; each becomes one
  `Insight`. Do not store the six-section doc as a single fact.
- **Sequence: write side first, read side gated.** Writing candidates is cheap, reversible, and
  *generates the data the dogfood gate needs*. Cutting recall over to `praxis_get_context` should
  follow the dogfood suite's retrieval gate going clean-green — today it is a strict NO-GO with the
  gap attributed to semantic-only retrieval (see the
  [auto-distill slice](2026-06-24-pr-knowledge-auto-distill-slice.md)). Until then, ce-compound writes
  to Praxis but agents should not yet *rely* on Praxis as their only recall path.
- **Distill in the backend (revisit client-side later).** ce-compound POSTs the rendered narrative to a
  backend session-ingest endpoint that runs `SessionIngestor` and writes `proposed` candidates through
  the same `Ingestor.ingest` path as `CommitIngestor` — one extractor in one place, matching the
  deployed split where distillation + writes are already backend-side. A client-side distillation
  variant stays open as a later option if keeping the narrative warm proves worthwhile.
- **Keep the markdown doc as provenance.** The graph fact is the record; ce-compound still writes the
  human-readable doc as a non-authoritative log that links to its facts, keeping the artifact browsable
  and `ce-compound-refresh` working. This is not the "two stores" risk — one record (graph) plus a
  readable log, not two records of truth.
- **Overlap with `CommitIngestor`: dedup, don't couple.** A session that ends in a merged PR is
  distilled twice, and that is healthy: the sources aren't redundant (session = the journey + rejected
  approaches; PR = the reviewed result + review threads), and near-duplicate insights merge to bump
  `observation_count` (a confidence signal). Reject commit-trailer coupling between the triggers;
  consider source-keyed idempotency only if measured LLM cost justifies it.

## Requirements

- **R1.** `SessionIngestor` variant alongside `PromptIngestor` / `CommitIngestor`, reusing the
  `Ingestor` contract. Input = rendered session narrative; output = `Insight[]` via one structured
  `Llm.complete` call. `source = "session/<id>"`.
- **R2.** Shared `_SCHEMA` (and `synthesis` scaffolding) extracted from `CommitIngestor` so the two
  variants share one definition.
- **R3.** ce-compound (in a Praxis-configured repo) distills its Phase 1 narrative through R1 and
  writes the insights as `proposed` candidates — landing in the human gate, not `active`.
- **R4.** The markdown doc is optional provenance, not the record. No second flat-file store of truth.
- **R5.** Diagnostic capture mirroring the auto-distill slice's R7: extraction quality (signal-to-noise
  of distilled session insights) and, once read-side lands, retrieval hit rate.

## Success criteria

- Session-distilled insights reach the candidate dashboard as reviewable `proposed` facts with correct
  `scope`/`category` and session provenance, dedup'd against existing facts.
- The `SessionIngestor` / `CommitIngestor` diff is essentially just the prompt — confirming the
  "one extractor, two triggers" framing rather than two divergent pipelines.
- Once the retrieval gate is green, a fresh session retrieves a previously-distilled session fact via
  `praxis_get_context` at the right moment.

## Validation

Split the feature in two — the halves have very different validation cost.

**Write side (extraction) — cheap, checkable now, no agent run.** There is a gold pairing entirely in
local data: session `fd866322` (a real solved-problem arc — catching that the 002 plan gated on
falsified footguns and revising it) *and* the `/ce-compound` doc it produced
([`gate-eval-experiment-plans-on-validated-footguns.md`](../solutions/conventions/gate-eval-experiment-plans-on-validated-footguns.md)),
which is a **human-vetted reference output**. The test: render the session narrative, run the
`SessionIngestor` distill prompt over it, and diff the `Insight[]` against the doc. No fresh coding
tasks, no win-hunt, no multi-arm run — you already did the work once.

**Delivery side (retrieval) — expensive, and deferred.** The markdown-vs-Praxis-vs-cold A/B is the
multi-arm agent run, and the dogfood suite already attributes the NO-GO to semantic-only retrieval, so
running it today mostly re-confirms the known gap. Gate it on the retrieval gate going clean-green, and
reuse the dogfood apparatus (validated footguns, cost-to-correct) rather than fresh constructs — per
[`gate-eval-experiment-plans-on-validated-footguns.md`](../solutions/conventions/gate-eval-experiment-plans-on-validated-footguns.md)
itself.

### What the write-side smoke test found (Claude-as-distiller proxy, n=1)

Running the prompt over the `fd866322` narrative recovered the doc's content cleanly — the headline
convention, the "expensive apparatus-null" rationale, and the three empirical footgun facts
(phoenix invalid / umap variance-prone / yoyo validated) — and ignored the tool-call play-by-play.
**Extraction is not the risk.** The risks are downstream of it, and the build should account for them:

1. **Granularity: one doc → ~6 atomic facts.** Better for semantic retrieval (each surfaces
   independently) but loses the doc's narrative cohesion, and a single precision-first call **over-emits
   near-duplicates** (two phrasings of the same convention). This is acceptable — `graph.write` dedup
   merges them and bumps `observation_count` — but it confirms the distiller itself is not dedup-aware
   (by design) and must rely on the write path.
2. **Scope is the shakiest column.** The narrative is full of file paths (the 002 plan, the yoyo case)
   that are *provenance*, while the *lesson* applies `module:knowledge/evals`-wide. The distiller's
   `file:`-vs-`module:`-vs-`repo` guesses were the least reliable output — concretely confirming the
   sibling proposal's "provenance is not scope," and pointing at where prompt effort should go.
3. **Experiment-state vs durable repo knowledge.** Some recovered facts ("the dogfood suite dropped
   phoenix") describe an *in-flight experiment's current state*, not durable facts about the codebase;
   they go stale when the suite changes and are lower-value to a coding agent than a true gotcha. The
   session prompt likely needs to distinguish "knowledge about the code" from "knowledge about an
   in-flight experiment," or these will pollute retrieval.

The proxy caveat is real: this was run by the assistant, not the configured `OpenRouterLlm`, on one
session. It de-risks the approach; it is not the verdict. The real `SessionIngestor` run over the same
pairing is the confirming step.

## Scope boundaries (deferred)

- **Read-side cutover** — gated on the dogfood retrieval gate (semantic-only retrieval is the known
  weak link; two-lane scope-aware retrieval is the sibling proposal's concern).
- **Auto-supersede vs. human gate for self-captured facts** — same open question the
  [commits-and-PRs proposal](2026-06-24-ingest-commits-and-prs.md) raises; this slice keeps the human
  gate.
- **Headless / automated ce-compound runs writing without review** — out of scope; manual,
  human-confirmed runs first.

## Open questions

The distill-locus, doc-survival, and double-distill forks were resolved into Key Decisions (backend
distillation; keep the doc as provenance; dedup rather than couple the triggers). What remains:

**Resolve before planning**

- None outstanding. The blocking forks were resolved this pass — see Key Decisions.

**Deferred to planning**

1. **Scope inference for session facts.** *Approach decided:* add prompt guidance ("the files a session
   names are where the lesson was *found*, not necessarily where it *applies*") and let the human gate
   correct scope at review. The write-side smoke test flagged scope as the distiller's least reliable
   output. Deferred: the exact prompt wording, and whether the real model's scope error rate justifies a
   dedicated scope-inference pass (don't build one speculatively).
2. **Filtering in-flight experiment state.** *Approach decided:* prompt guidance to prefer durable
   code/architecture knowledge over an in-flight experiment's current state, with the human gate as
   backstop and the supersession/decay model aging out stale experiment facts. Deferred pending gate
   results: whether a distinct low-confidence category is still needed.
3. **Backend session-ingest endpoint.** Does a path to create `proposed` candidates from a rendered
   narrative already exist, or is it new backend surface (it follows from the backend-distill decision)?
   Resolve during planning research.
