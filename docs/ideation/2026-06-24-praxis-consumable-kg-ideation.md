---
date: 2026-06-24
topic: praxis-consumable-kg
focus: extend Praxis so an external agent can trivially use it as its KG backend; prove via the tax-return eval
mode: repo-grounded
---

# Ideation: Make Praxis a Trivially-Consumable KG (and prove it with the tax-return eval)

## Grounding Context (Codebase)

Praxis is a multi-tenant KG retrieval system (RDS Postgres + pgvector, tenancy by `(org_id, user_id)`).
External-consumption surface today:

- **MCP server** (`knowledge/mcp/server.py`) — 9 tools (`praxis_get_context`, `praxis_add_insight`,
  `praxis_insert_fact`, `praxis_edit_fact`, `praxis_list_graph`, contradictions, login/org). Thin
  httpx client over the HTTP backend. Tools return **plain display strings, not JSON**.
- **HTTP backend** (`knowledge/serve/app.py`, FastAPI) — `/context` (GET query+top_k → hits),
  `/insights` (POST approved fact), `/candidates*`, `/contradictions*`, `/orgs*`. Auth = Cognito
  Bearer JWT + `X-Praxis-Org` header; `active_org` checks membership. Dev seam `PRAXIS_AUTH_DISABLED=1`.
- **Core lib** (`knowledge/wiring.py` `build_trio(substrate=in_memory|vector|postgres, reader, embedder, …)`)
  → `(graph, ingestor, reader)`. `ingestor.ingest()`, `reader.read()`, `graph.search()`. Fact/Claim/SearchHit shapes.
- **Client** (`frontend/services/api_client.py` `ApiDataProvider`) — reference only, **not published**.
- **Eval framework** (`knowledge/evals`) — full-pipeline cases: raw sources `via_ingestor` → KG →
  generic `seed_prompt` → agent writes `answer.md` → `deterministic_checks` + `rubric`. A tax case
  `matt_tax_return_single_w2` already exists.

Friction for an external consumer: Cognito-only auth + org-password (no API key/service account);
MCP tools return strings not JSON; no batch ingest; no published client / OpenAPI; MCP coupled to repo
`.env`/cwd; no standalone "consume Praxis from another repo" quickstart.

Prior art (web): TurboTax **calculation isolation** (LLM never owns arithmetic), OpenTax 1040 DAG,
AAAI 2026 neuro-symbolic (separate extraction from arithmetic), SARA/FinQA (extract-then-compute).
IRS TY2025: std deduction $15,750 single / $31,500 MFJ / $23,625 HOH; single brackets 10/12/22/24%.

## Topic Axes

A. Consumption surface (MCP/HTTP/SDK ergonomics for an external agent)
B. Auth & onboarding (service-account/API-key, quickstart, sandbox tenancy)
C. Ingest + retrieve contract (structured JSON, batch, the shape the tax agent needs)
D. Observability + eval (the tax eval proving the KG does the work end-to-end)
E. Packaging & extensibility (published client, OpenAPI, thin shell so the harness is trivial)

## Ranked Ideas

### 1. Tax-return eval set proving the KG does the work end-to-end
**Description:** Expand `knowledge/evals/cases/matt/tax_return` from one case into a small generated set
(single W-2 / MFJ / different filing statuses) that ingests the 1040 instructions + W-2 + intake Q&A and
retrieves enough to fill the return, graded by `deterministic_checks` + rubric. Add a `component: graph_reader`
recall/no-leak case so it grades the *retrieval set*, not just the answer.
**Axis:** D
**Basis:** `direct:` user — "build the original eval I requested … so the separate harness doesn't have to do that much"; existing `matt_tax_return_single_w2`; intent_gating README ("can't tune retrieval against answer-quality alone").
**Rationale:** This is the proof artifact: it demonstrates Praxis already does ingest→distill→retrieve for the tax domain, so the harness offloads KG work to Praxis.
**Downsides:** Eval-only; needs cassettes/cached embeddings to replay offline.
**Confidence:** 90%
**Complexity:** Low-Medium
**Status:** Explored (case 1 already built)

### 2. Structured-JSON consumption contract for MCP tools
**Description:** Have `praxis_get_context` / `praxis_add_insight` (and friends) return structured JSON
(hits with `id`, `text`, `score`, `source`/provenance; ingest results with `action`, `id`) instead of
display strings, so an external agent consumes context programmatically without regex-parsing.
**Axis:** C
**Basis:** `direct:` friction map — "MCP tools return plain text (not structured JSON); readers parse display strings."
**Rationale:** The tax harness needs machine-readable facts to ground 1040 line values; strings force brittle parsing.
**Downsides:** Changes the tool output contract; current UI/CLI string consumers may need updates.
**Confidence:** 85%
**Complexity:** Low
**Status:** Unexplored

### 3. Service-account / API-key auth + documented local dev seam
**Description:** Add a long-lived API-key / service-token path (scoped to an org) for non-interactive
agents, and document the `PRAXIS_AUTH_DISABLED=1` + `X-Praxis-Org` local path. Removes the Cognito-SRP
+ org-password dance for an external automated consumer.
**Axis:** B
**Basis:** `direct:` gap — "Auth is Cognito-only; no API key / service account support … external batch agents can't use long-lived API keys."
**Rationale:** The single biggest onboarding tax; an external hackathon agent can't easily mint Cognito tokens per request.
**Downsides:** Security surface — needs scoping, rotation, storage; or accept dev-seam-only for the prototype.
**Confidence:** 70%
**Complexity:** Medium
**Status:** Unexplored

### 4. Importable praxis-client SDK + OpenAPI spec
**Description:** Promote `ApiDataProvider` into a small installable client (`praxis_client`) with
`ingest()`, `get_context()`, `add_insight()`, and let FastAPI emit its OpenAPI spec so consumers can
generate clients. The harness imports rather than reverse-engineers.
**Axis:** E
**Basis:** `direct:` gaps — "No PyPI package … No OpenAPI/Swagger spec."
**Rationale:** Directly realizes "trivial harness extension" — a few-line import instead of vendoring HTTP code.
**Downsides:** Packaging/versioning overhead; for a hackathon, an in-repo `examples/` client may suffice.
**Confidence:** 75%
**Complexity:** Medium
**Status:** Unexplored

### 5. Batch ingest endpoint
**Description:** A batch `POST /ingest` (or batched `/insights`) that accepts multiple documents/insights
in one call, so ingesting the W-2 + 1040 instructions + Q&A is one request, not N.
**Axis:** C
**Basis:** `direct:` gap — "No bulk POST /candidates or batch retrieval endpoint; adding 100 facts requires 100 POST calls."
**Rationale:** The tax agent's onboarding is exactly a small bundle of docs; batch makes the consumer's ingest trivial and atomic.
**Confidence:** 70%
**Complexity:** Low-Medium
**Status:** Unexplored

### 6. Retrieval observability / provenance on hits
**Description:** Return per-hit provenance (source doc, score, fact id, optionally why-retrieved) on
`/context`, so the consuming agent and the eval can trace which ingested fact grounds each retrieved value.
**Axis:** D
**Basis:** `reasoned:` calculation-isolation theme (ground numbers in retrieved facts) + intent_gating retrieval-precision warning.
**Rationale:** Lets the harness cite sources and the eval assert recall/no-leak; supports trustable grounding.
**Confidence:** 65%
**Complexity:** Low-Medium
**Status:** Unexplored

### 7. Quickstart + minimal external-consumer example
**Description:** A standalone "Use Praxis as your KG from another repo" doc + a ~20-line example
(authenticate → batch-ingest docs → get_context) under `examples/`, decoupled from repo `.env`/cwd.
**Axis:** A/E
**Basis:** `direct:` gap — "No standalone consume-Praxis tutorial for external repos; MCP registration tied to repo directory."
**Rationale:** The fastest path to "trivial" is a copyable worked example the harness author follows.
**Confidence:** 80%
**Complexity:** Low
**Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | DAG-as-agent-graph / node-per-1040-line | subject-replacement — architects the separate harness repo |
| 2 | Blackboard slots + referee orchestration | subject-replacement — harness-internal control flow |
| 3 | Event-sourced TaxState / log-as-state | subject-replacement — harness state model (Praxis already has facts/cached_facts) |
| 4 | Elm/Redux pure reducer agent loop | subject-replacement — harness runtime |
| 5 | Compiler typed-IR lowering passes | subject-replacement — harness build pipeline |
| 6 | Frontier / info-gain question planner | subject-replacement — harness conversation design (5-question budget) |
| 7 | Speculative pre-fill ("compute then confirm") | subject-replacement — harness UX |
| 8 | Middleware guardrail ring / types-as-guardrails | subject-replacement — harness guardrails (pillar 3) |
| 9 | No-LLM-compute-path / spreadsheet recalc engine | subject-replacement — harness tax engine |
| 10 | All-client/no-server portable graph | subject-replacement — harness deployment |
