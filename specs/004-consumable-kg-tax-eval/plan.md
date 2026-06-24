# Plan — Praxis as a trivially-consumable KG, proven by a tax-return eval

Status: **draft / awaiting review**
Date: 2026-06-24
Ideation: [docs/ideation/2026-06-24-praxis-consumable-kg-ideation.md](../../docs/ideation/2026-06-24-praxis-consumable-kg-ideation.md)

## Goal

Extend Praxis so that an **external agent running in a separate repo** (specifically: a
hackathon tax-filing assistant — chat → fills a 2025 Form 1040 → downloadable) can use Praxis
as its knowledge-graph backend with near-zero KG plumbing of its own. The external harness should
only have to: **(a) authenticate, (b) drop its documents in, (c) ask for context.** Everything
KG-shaped — distillation, dedup, contradiction handling, retrieval ranking — stays inside Praxis.

We **prove** this contract with a tax-return eval set under `knowledge/evals/cases/matt/tax_return`
that exercises the full ingest → distill → retrieve → fill pipeline on real TY2025 tax material.

### Locked decisions (from ideation)
- **Scope:** full set, **eval-driven** — the eval defines what the surface must deliver; build the
  eval first, let it pull the surface work behind it.
- **Auth:** add a **scoped API key / service token** path (not Cognito-only) for automated agents.

### Out of scope (belongs to the separate harness repo)
The tax *agent's* internal architecture — chat loop, the 1040 tax engine, guardrails, the
downloadable-PDF step, deployment. Praxis is only the **knowledge layer** the harness calls.

## Non-negotiable facts the build must honor
- TY2025 standard deduction: **$15,750** single/MFS, **$31,500** MFJ, **$23,625** HOH.
- TY2025 brackets (single): 10% ≤ $11,925; 12% ≤ $48,475; 22% ≤ $103,350; 24% ≤ $197,300.
- Hackathon taxpayer profile: **single, one W-2, ~$40k/yr** → the eval's flagship case mirrors this.
- Praxis tenancy is `(org_id, user_id)`; existing surface = MCP server (`knowledge/mcp/server.py`),
  FastAPI backend (`knowledge/serve/app.py`), `build_trio` (`knowledge/wiring.py`).

---

## Workstream 1 — Tax-return eval set (the proving artifact)  [Task #1, in progress]

**Where:** `knowledge/evals/cases/matt/tax_return/`

**What:** Refactor the current single hand-built case into a table-driven generator that mirrors the
`matt/applications/_generate.py` convention (one source-of-truth script + shared sources + generated
per-scenario `case.yaml`; generated files carry the "edit the script, not this file" header).

**Shared source (ingested raw via `seeded_insight.via_ingestor`):**
- `sources/form_1040_instructions.txt` — line-by-line rules + TY2025 std deductions + single/MFJ/HOH
  brackets (already rewritten with real figures).

**Per-scenario sources (W-2(s) + intake Q&A), generated into `sources/<scenario>/`:**

| Scenario (folder) | Filing status | W-2 wages / withholding | Expected result |
|---|---|---|---|
| `single_w2` | Single | $40,000 / $3,200 | taxable $24,250 → tax $2,672 → **refund $528** |
| `mfj_two_w2` | MFJ (two W-2s) | $40,000/$3,200 + $35,000/$2,600 | taxable $43,500 → tax $4,743 → **refund $1,057** |
| `single_owes` | Single | $90,000 / $9,000 | taxable $74,250 → tax $11,249 → **owes $2,249** |

(Canonical figures live once per scenario in the generator so the deterministic-check regexes and the
rubric's arithmetic criterion can't drift. All math recomputed and pinned in the script.)

**Grading per scenario:**
- `deterministic_checks`: `output_nonempty` + comma-optional regexes for total income, std deduction,
  taxable income, computed tax, withholding, and the refund/owe bottom line (`regex_matches`); a
  `regex_absent` guard against fabricated credits/income lines.
- `rubric`: grounded / correct_arithmetic / complete / no_questions_back (weighted).

**Plus one retrieval case (`component: graph_reader`):** `retrieval_recall_no_leak`
- Co-ingest the single-filer's docs **and** a second taxpayer's W-2, query with a fill prompt, then
  assert (a) the filer's own figures + the rules surface in the reader output (`regex_matches`), and
  (b) the *other* taxpayer's wage number does **not** leak (`forbids_substring`). This grades the
  retrieval set itself, per the intent_gating lesson ("can't tune retrieval on answer quality alone").

**Knobs (copied from applications, with rationale):** `substrate: vector`, `embedder: cached`,
`ingest_model: openai/gpt-4o-mini`, `ingest_state: active`, `reader: retrieving`, `reader_top_k: 0`,
`needs: [file_io]`, `target_commit: 0*40`.

**Done when:** `uv run python .../tax_return/_generate.py` regenerates all cases and every case loads
via `knowledge.evals.run.load_case`; a `README.md` documents the set and how to run it.

---

## Workstream 2 — Structured-JSON consumption contract + provenance  [Task #2]

**Where:** `knowledge/mcp/server.py`, `knowledge/serve/app.py` (`/context`, `/insights`).

**What:**
- `/context` returns (already has) `hits` with `id/text/score`; add per-hit **provenance**
  (`source`, `scope`/`category` where available) so a consumer can cite which ingested doc grounds a value.
- MCP `praxis_get_context` / `praxis_add_insight` return **structured JSON** (or a JSON block) rather
  than only a human display string, so the external agent consumes results without regex-parsing.
- Keep backward-compatible display text for existing CLI/UI consumers (return both, or a
  `format=json` switch) — decide during implementation, default to additive.

**Done when:** an external caller can get machine-readable hits with provenance from both the HTTP API
and the MCP tool; existing consumers still work.

---

## Workstream 3 — Batch ingest endpoint  [Task #3]

**Where:** `knowledge/serve/app.py` (+ MCP convenience wrapper).

**What:** A batch ingest route accepting multiple documents/insights in one request (the tax agent's
onboarding is exactly a small bundle: W-2 + 1040 instructions + Q&A). Returns per-item results
(action/id) and is tenant-scoped like the single-item path. Reuses the existing ingest/write policy.

**Done when:** ingesting the 3-document tax bundle is one call; covered by a small test.

---

## Workstream 4 — Scoped API-key auth for external agents  [Task #4]

**Where:** `knowledge/serve/auth.py`, `knowledge/serve/app.py`, `knowledge/serve/schema.sql`.

**What:** A long-lived, **org-scoped API key / service token** path so an automated agent
authenticates without the Cognito SRP + per-request token mint.
- New `api_keys` table (hashed key, `org_id`, optional `user_id`/label, created/last-used, revoked).
- Auth dependency accepts either a Cognito Bearer JWT (existing) **or** an `Authorization: Bearer
  pxk_…` API key / `X-Praxis-Key` header, resolving the same `Principal` + org membership.
- A minimal way to mint/revoke a key (CLI command or an authenticated endpoint).
- Security: store only a hash, scope to one org, support revocation; keep `PRAXIS_AUTH_DISABLED=1`
  local seam documented for fully-local runs.

**Done when:** an agent with only an API key + org id can ingest and retrieve; keys are hashed,
revocable, and tenant-scoped; tests cover accept/reject/revoke.

---

## Workstream 5 — `praxis_client` SDK + OpenAPI + quickstart  [Task #5]

**Where:** new `praxis_client/` (promote `frontend/services/api_client.py`), `examples/`, docs.

**What:**
- A small importable client: `PraxisClient(base_url, api_key, org_id)` with `ingest()` /
  `ingest_batch()` / `get_context()` / `add_insight()` (thin over the HTTP API; stdlib or httpx).
- Ensure FastAPI's **OpenAPI** schema is exposed/served so consumers can generate clients.
- A standalone **quickstart** doc ("Use Praxis as your KG from another repo") + a **~20-line example**
  under `examples/` showing: authenticate (API key) → batch-ingest the tax bundle → `get_context`.

**Done when:** a fresh external script can, in ~20 lines, point at Praxis (prod or local), ingest the
tax docs, and retrieve context — no vendoring of internal HTTP code.

---

## Sequencing & verification

1. **WS1 eval set** first (in progress) — establishes the target behavior and a regression net.
2. **WS2 + WS3** (JSON/provenance, batch ingest) — the surface the eval shows a consumer needs.
3. **WS4 API key** — onboarding without Cognito.
4. **WS5 SDK + quickstart + example** — ties it together; the example is itself a mini acceptance test.
5. Re-run the tax eval set after surface changes to confirm nothing regressed.

**Verification per workstream:** unit/integration tests where backend logic changes (WS2–WS4);
`load_case` + a real eval run for WS1; the `examples/` script as a smoke test for WS5.

## Open questions for review
- WS2: return JSON-only from MCP tools, or keep a dual display+JSON output? (default: additive/dual)
- WS4: mint keys via a CLI command vs. an authenticated HTTP endpoint? (default: CLI first)
- WS5: publish `praxis_client` to PyPI, or keep it in-repo + installable via path/git? (default: in-repo)
- Eval set: is 3 answer-graded scenarios + 1 retrieval case the right coverage, or add HOH / a dependent+CTC case?
