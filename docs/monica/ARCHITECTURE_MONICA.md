# ARCHITECTURE_MONICA.md

## Monica Peters — Dashboard & Human Gate Pillar

### MoniGarr Operating Model (M.O.M.) + M.I.L.E. — Pillar-Scoped Architecture

```text
============================================================================
PROJECT ARCHITECTURE (PILLAR)
============================================================================
Project Name:       PRAXIS — Dashboard & Human Gate
Repository:         https://labs.gauntletai.com/monicapeters/praxis
Pillar Owner:       Monica Peters (monigarr@monigarr.com)
Co-Leads:           Matthew Daw (ML Pipeline), Dominic Antonelli (Eval & Integration)
Organization:       Gauntlet AI for America
Branch:             monica/dashboard-human-gate
Version:            0.2.1 (Days 1–8 Streamlit complete; React client shipped for Matthew)
Status:             Active development — dual UI clients on mock; awaiting Matthew's live API
Classification:     Internal — capstone sprint
Created:            2026-06-18
Last Updated:       2026-06-19 (plans path fix + as-built alignment with branch)
Source of Truth:    docs/plans/PRAXIS_Project_Plan.html
License:            TBD — Gauntlet AI capstone (2026)
============================================================================

DESCRIPTION
----------------------------------------------------------------------------
Architecture definition for Monica's pillar only: the Streamlit human-gate
dashboard in frontend/. This document does NOT prescribe Matthew's pipeline,
Dominic's eval harness, or team-wide deployment topology. It defines how the
dashboard integrates via agreed contracts so each pillar can ship, deploy,
and evolve independently.

Interview claim (from project plan):
"I led the design and built the human approval dashboard that enforces quality
gates and makes knowledge promotion transparent and measurable."
============================================================================
```

---

# 1. Executive Summary

## Overview

PRAXIS turns Claude Code session JSONL logs into a verified **Knowledge Graph** with a mandatory **human approval gate** before knowledge is injected into future sessions. Monica's pillar owns the **Knowledge Graph Dashboard** — the UI where reviewers inspect distilled candidates, understand confidence and provenance, promote lessons through lifecycle states, resolve contradictions, and trigger downstream approval flows.

The dashboard is implemented as a **modular Python + Streamlit** application under `frontend/`. Monica chose Streamlit deliberately for **research-data visualization and human review workflows** — not as a repo-wide UI mandate. Full rationale, stack definition, and **React coexistence guarantees** are in [§2 Tech Stack & Presentation Architecture](#2-tech-stack--presentation-architecture-monicas-decision).

The UI consumes candidate data from Matthew's pipeline via a **contract-first API boundary** (Days 6–7) and surfaces audit-friendly actions that Dominic's eval harness can measure. It deploys anywhere a Python web process can run — Monica's target is **Render.com**; teammates retain full sovereignty over their own hosting and frontend choices.

System-wide context and end-to-end loop: [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) and [README.md](../README.md).

---

## Business Objective

| Dimension | Monica's pillar contribution |
|-----------|------------------------------|
| **Primary problem addressed** | Auto-memory and opaque model saves have no quality gate. Humans must approve 100%-credible insertions and resolve contradictions before knowledge compounds. |
| **Expected ROI** | Reviewers promote good lessons in a few clicks; every item shows evidence (`logs/<file>.jsonl:<line>`). Demo Act 2: dashboard fills with scored candidates linked to transcript lines; one click moves `suggested → active`. |
| **Strategic value** | Makes the "human in the loop" real, auditable, and interview-ready — the trust layer that backs the ≥50% correction-reduction claim. |
| **Long-term intent** | A reusable, accessible review surface any future contributor can run locally, on Render, or embed beside other deployment stacks without forking the repo. |

---

## Operational Philosophy

This pillar follows:

- **AI-First Engineering** — Streamlit + Python for rapid iteration; AI assists wireframes, components, and docs; humans approve all promotions.
- **Human-in-the-loop accountability** — No candidate reaches `active` without explicit human action in the dashboard.
- **Sovereign engineering** — Monica owns `frontend/` UX and presentation; Matthew owns distillation/scoring truth; Dominic owns measurement and integration hooks. No pillar blocks another.
- **Documentation as infrastructure** — Wireframes, data contracts, and this architecture doc are onboarding artifacts for teammates and future users.
- **Handoff-ready engineering** — Clear module boundaries, typed contracts, env-driven configuration, and mock providers so Matthew can integrate before the API exists.

---

# 2. Tech Stack & Presentation Architecture (Monica's Decision)

This section documents **Monica Peters' pillar-specific** architecture and technology choices. It is authoritative for the human-gate dashboard only. It does **not** override Matthew's pipeline stack, Dominic's eval/integration stack, or any future contributor's choice to build a **React-only** (or other) frontend.

## 2.1 Monica's Tech Stack (Human Gate Pillar)

| Layer | Choice | Version / notes |
|-------|--------|-----------------|
| **Language** | Python | 3.12+ (matches repo `pyproject.toml`) |
| **UI framework** | [Streamlit](https://streamlit.io/) | ≥1.32 — pillar presentation layer |
| **Tabular data** | pandas | DataFrames for list/filter/sort; aligns with pipeline output shapes |
| **Visualization** | Streamlit native + Altair (bundled) | Progress columns, charts, metrics for research review |
| **State** | `st.session_state` | Ephemeral UI state; backend KG is source of truth |
| **Integration** | HTTP client (planned) | REST to Matthew's API — UI framework agnostic |
| **Local deps** | `frontend/requirements.txt` | Isolated from React `node_modules` if added later |
| **Deploy target** | Render.com web service | Monica-owned; not required for teammates |

**Ownership boundary:** Everything in this table lives under `frontend/` and Monica's Render config. Teammates who never run the dashboard are unaffected.

---

## 2.2 Why Streamlit — Research Data & Human Review

PRAXIS's human gate is a **research review instrument**, not a marketing site. Reviewers work with structured evidence: scored candidates, confidence decompositions, provenance links to JSONL lines, contradiction pairs, and (via Dominic) compounding eval curves. Monica chose Streamlit because it optimizes for that workflow.

| Requirement | How Streamlit serves it |
|-------------|---------------------------|
| **Dense research tables** | `st.dataframe` with sortable columns, `ProgressColumn` for confidence, built-in formatting — ideal for scanning dozens of distilled lessons |
| **Rapid visual iteration** | Wireframe → working UI in hours; no separate build toolchain for sprint Days 1–5 |
| **Quantitative credibility UX** | `st.progress`, `st.metric`, line/bar charts (Altair) for frequency/recency/breadth and eval compounding curves |
| **Provenance-forward layout** | Captions, expanders, bordered containers for evidence chains without custom CSS frameworks |
| **Python-native pipeline fit** | Same language as Matthew's distillation code and Dominic's eval scripts — shared JSON contracts, no JS/Python impedance mismatch during integration |
| **Sprint realism** | 9–10 day capstone; custom React design system explicitly out of scope per [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md) Day 1 decision |

**What Streamlit is not chosen for:** pixel-perfect brand UI, native mobile apps, or replacing a production SaaS shell. Those are valid reasons for a future React app — and this architecture **allows** that without undoing Monica's work.

**Interview framing:** *"I chose Streamlit because the human gate is a data-review surface — scored candidates, provenance, and confidence metrics — and Streamlit let me ship credible research visuals fast while keeping integration contract-first so the backend stays UI-agnostic."*

Pillar docs live under `docs/monica/` (canonical home for Monica's documentation).

---

## 2.3 Architecture Pattern: API-First, UI-Optional

Monica's presentation architecture follows **API-first, multiple clients allowed**:

```text
                    ┌─────────────────────────────────────┐
                    │   Matthew's API (contract owner)     │
                    │   candidates · promote · reject      │
                    └──────────────┬──────────────────────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           │                       │                       │
           ▼                       ▼                       ▼
   ┌───────────────┐      ┌───────────────┐      ┌───────────────┐
   │ frontend/     │      │ frontend-react│      │ knowledge/evals/ │
   │ Streamlit     │      │ React client  │      │ Dominic       │
   │ Monica · now  │      │ Monica · now  │      │ no UI required│
   └───────────────┘      └───────────────┘      └───────────────┘
```

**Invariant:** Business logic for distillation, scoring, storage, and promotion side-effects stays in **Matthew's `knowledge/` + Dominic's hooks**. Neither Streamlit nor React embeds that logic — both are thin clients over the same HTTP contract ([§17 Integration Architecture](#17-integration-architecture--data-contracts)).

---

## 2.4 React Coexistence — No Blockers for Teammates

Monica's Streamlit choice **must not interfere** with current or future teammates who prefer **React only**. This architecture enforces that through repo layout and integration rules, not goodwill alone.

### Guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| **No React requirement** | Matthew and Dominic integrate via API and JSON contracts — they never need Streamlit installed for their pillars |
| **No Streamlit requirement for React devs** | **`frontend-react/` shipped** — Vite + React + TypeScript sibling with its own `package.json`, CI job, and deploy target |
| **No shared UI code** | `frontend/` is Streamlit-only; React components never import from `frontend/app.py` and vice versa |
| **No root dependency lock-in** | Root `pyproject.toml` lists Streamlit for Monica's pillar; React deps stay in the React subtree — adding React does not remove Streamlit |
| **Same backend, any client** | OpenAPI or JSON Schema for candidates and mutations is the **only** coupling surface ([monica-wireframes.md](monica-wireframes.md)) |
| **Parallel deploys** | Streamlit on Render, React on Vercel/Netlify/AWS CloudFront — all point at the same `PRAXIS_API_BASE_URL` |
| **Parallel development** | Monica merges to `frontend/`; a React contributor merges to `frontend-react/` — no file conflicts if boundaries are respected |

### What Monica does not do

- Does **not** add Streamlit imports to `knowledge/` or `knowledge/evals/`
- Does **not** make the human-gate API Streamlit-specific (no Streamlit session IDs in API payloads)
- Does **not** block MRs that add a React frontend in a sibling directory
- Does **not** claim `frontend/` as the only UI path for the whole repo — only as **Monica's pillar deliverable**

### Future React-only path (explicitly supported)

If a teammate or future contributor wants **React only** for a new or replacement UI:

1. **Consume the same API** — implement list/detail/promote against the published contract; do not fork pipeline code.
2. **Add `frontend-react/`** (or team-agreed name) — Vite/Next/CRA per their choice; zero changes required in `frontend/` for them to start.
3. **Deprecate Streamlit optionally** — a team decision, not a technical prerequisite; Monica's Streamlit app can remain as internal/research tooling even if React becomes the public-facing UI.
4. **Reuse Monica's UX artifacts** — wireframes, state machine (`proposed → suggested → active`), and data contract transfer directly; only the view layer changes.

```text
Coexistence timeline (all valid):

  Now:        frontend/ (Streamlit) + frontend-react/ (React) ──same API──► knowledge/
  Future B:   frontend-react/ only ──API──► knowledge/   (Streamlit archived by team choice)
```

**None of these futures require Monica to rewrite pipeline code or block Matthew's AWS/Dominic's eval work.**

---

## 2.5 Streamlit vs React — Decision Record

| Criterion | Streamlit (Monica — chosen) | React (teammate — supported alternate) |
|-----------|----------------------------|----------------------------------------|
| Primary use case | Research review, eval viz, internal human gate | Custom product UI, design-system control |
| Time to MVP in sprint | Days 1–2 shell proven | Longer — build, routing, component library |
| Data-heavy tables/charts | Native, minimal code | Requires libraries (TanStack Table, Recharts, etc.) |
| Python team alignment | Same stack as `knowledge/` / `knowledge/evals/` | Requires API boundary anyway (still fine) |
| Blocks other framework? | **No** — API-first | **No** — sibling directory |
| Monica's deliverable | ✅ Sprint MVP | Optional future track |

**Decision date:** Day 1 (2026-06-16), recorded in [monica-wireframes.md](monica-wireframes.md) and team plan HTML (Streamlit in scope; React noted as original plan language but deferred for Monica's sprint path).

**Revisit trigger:** Post-sprint, if product needs branded multi-page SaaS UX, a React app can be added **alongside or instead of** Streamlit without architectural contradiction — because the API contract was the integration spine from Day 1.

---

## 2.6 Research Visualization Roadmap (Streamlit)

Planned visuals that justify the stack choice (Days 3–8):

| Visual | Streamlit mechanism | Research purpose |
|--------|---------------------|------------------|
| Confidence breakdown | Columns + `st.metric` + tooltips | Show freq/recency/breadth rationale |
| Candidate comparison | `st.columns` + bordered containers | Contradiction resolution side-by-side |
| Compounding curve | `st.line_chart` / Altair | Dominic's eval: correction rate falling over sessions |
| Provenance audit | `st.caption`, `st.expander` | Link every lesson to JSONL line evidence |
| State distribution | Bar chart or metric row | Demo narrative: proposed → active funnel |

These ship in Monica's pillar without requiring charting decisions in Matthew's or Dominic's codebases.

---

# 3. MoniGarr Operating Model (M.O.M.) — Pillar Application

## 3.1 Human Accountability First

The dashboard exists because AI distillation is **untrusted by default**. Monica's UI makes accountability visible:

- Provenance on every candidate (source log path + line offset).
- Confidence breakdown (frequency, recency, breadth) with rationale tooltips (Days 3–5).
- Audit trail on promote, reject, and resolve actions (Days 6–7).
- State transitions require explicit human clicks — no autonomous promotion.

---

## 3.2 Ancient + Human + Artificial Intelligence Integration

| Layer | Dashboard role |
|-------|----------------|
| **Traditional intelligence** | Review heuristics, checklist UX, provenance linking, accessibility patterns |
| **Human contextual reasoning** | Final promotion, contradiction resolution, rejection of bad distillations |
| **AI acceleration** | Streamlit rapid UI, mock data generation, component scaffolding |

All three remain visible in the UI — nothing is hidden behind a black-box "approve all" control.

---

## 3.3 Enterprise from Day One (Pillar Scope)

Within `frontend/`:

- **Security** — No secrets in code; API URL via environment; read-only log paths in UI where possible.
- **Maintainability** — Modular packages (`components/`, `services/`, `models/`) as the app grows beyond Day 2 shell.
- **Observability** — User-visible toasts and error states; structured logging hook points for Dominic's eval (Days 6–7).
- **Extensibility** — `DataProvider` abstraction: swap `MockDataProvider` → `ApiDataProvider` without UI rewrites.
- **Documentation** — [monica-wireframes.md](monica-wireframes.md), [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md), this file.
- **Handoff** — Any teammate runs `streamlit run app.py` with mocks; no AWS/DynamoDB/Render lock-in required.

---

## 3.4 Documentation as Infrastructure

| Artifact | Purpose |
|----------|---------|
| `docs/monica/ARCHITECTURE_MONICA.md` | Pillar architecture (this document) |
| `docs/monica/monica-wireframes.md` | As-built screen spec and candidate contract |
| `docs/monica/Monica-Peters-Dashboard-Plan.md` | Sprint deliverables and timeline |
| `docs/monica/DEMO_SCRIPT.md` | Live demo Act 2 script + video checklist |
| `docs/monica/DAYS_9_10_REMAINING.md` | Demo rehearsal + a11y pass checklist |
| `docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md` | Scrum Master gap tracker + eval case backlog |
| `docs/monica/RENDER_DEPLOY.md` | Render.com deploy settings |
| `docs/monica/STANDUP_TEMPLATE.md` | Daily standup template |
| `docs/integration/candidate-api-v1.md` | Canonical Matthew ↔ Monica API contract |
| `.cursor/rules/praxis-dashboard.mdc` | Agent/editor guidance for dashboard patterns |

Team-wide architecture lives in the confidential project plan and Dominic's eval/integration docs — not duplicated here.

---

## 3.5 Handoff-Ready Engineering

A new contributor should be able to:

1. Clone the repo, `cd frontend`, install deps, run the dashboard with mock data in under five minutes.
2. Point `PRAXIS_API_BASE_URL` at Matthew's pipeline API when ready — no code fork required.
3. Deploy to Render (Monica), AWS (Matthew), or local (Dominic) using only pillar-specific config — see [§16 Deployment Architecture](#16-deployment-architecture).

---

# 4. System Scope

## In Scope (Monica's pillar)

| Area | Responsibility |
|------|----------------|
| **Human-gate UI** | Streamlit dashboard: candidate list, detail view, filters, search |
| **Lifecycle workflow** | `proposed → suggested → active` promotion and reject flows |
| **Provenance display** | Every item shows `logs/<file>.jsonl:<line>` (or agreed canonical form) |
| **Confidence UX** | Aggregate score now; freq/recency/breadth breakdown + tooltips (Days 3–5) |
| **Contradiction resolution** | Side-by-side comparison cards + resolution actions (Day 5) |
| **Credibility metrics viz** | Visual indicators supporting promotion decisions (Day 5) |
| **API client layer** | Thin HTTP client calling Matthew's backend; no pipeline logic in UI |
| **Mock data provider** | Local development without blocking on pipeline readiness |
| **Accessibility** | Keyboard-friendly flows, high contrast, screen-reader labels (Days 8–10 polish) |
| **Eval embed points** | Optional panels/hooks for Dominic's compounding-curve widgets (coordinate Day 8) |

---

## Out of Scope (owned by teammates — do not implement in `frontend/`)

| Area | Owner | Rationale |
|------|-------|-----------|
| JSONL ingestion, episode segmentation | Matthew | Pipeline correctness |
| Learning-moment detection, LLM distillation | Matthew | ML/KG engine |
| Embeddings, HDBSCAN, clustering, decay rules | Matthew | Scoring source of truth |
| Knowledge Graph storage (DynamoDB, vector DB, graph DB) | Matthew | Backend persistence |
| get-context tool, CLAUDE.md / skills generation | Matthew + Dominic | Injection substrate |
| Eval harness, cold vs injected runs, compounding curve computation | Dominic | Measurement spine |
| GitHub hook / PR automation on promotion | Dominic | Integration layer |
| Team-wide CI/CD, repo deployment topology | Dominic (+ shared agreement) | Each pillar deploys independently |
| **React SPA (alternate UI)** | **Monica — shipped `frontend-react/`** | Same candidate-api-v1 contract; Matthew validates server without Streamlit; deploy to Vercel/Netlify/static host |

**Non-blocking rule:** Changes under `frontend/` must not require edits to `knowledge/` or `knowledge/evals/` to run. Integration is **pull-based** (dashboard calls API) or **env-configured**, never hard-coded to Matthew's AWS or Dominic's server. A React frontend, if added, follows the same rule — API-only coupling.

---

# 5. STRATA-X Scale Classification

| Level | Description |
|-------|-------------|
| X0 | Micro modifications |
| X1 | Local feature |
| **X2** | **Component architecture** ← current |
| X3 | Cross-system architecture |
| X4 | Institutional systems |
| X5 | Sovereign / generational systems |

## Current Classification: **X2 — Component Architecture**

**Rationale:** The dashboard is a self-contained UI component within the larger PRAXIS system. It has clear boundaries, a defined data contract, and independent deployability. Full X3 cross-system classification applies to the integrated PRAXIS loop (project plan Figure 1), not to this pillar document alone.

---

# 6. Architecture Goals

## Functional Goals

1. **Transparent review** — Reviewers see title, content, state, confidence, provenance, and timestamps for every candidate.
2. **Effortless promotion** — Two-step gate (`proposed → suggested → active`) with clear visual state badges and one-click actions.
3. **Contradiction clarity** — Side-by-side cards with resolution actions that update state and notify backend (Day 5+).
4. **Contract-stable integration** — UI reads/writes through typed DTOs matching Matthew's API schema; mocks conform to the same shape.
5. **Demo-ready narrative** — Act 2 of the live demo: dashboard fills with evidence-linked candidates; human promotes in view of audience.

---

## Non-Functional Goals

| Category | Target |
|----------|--------|
| **Security** | No credentials in repo; API keys via env; HTTPS in production (Render default) |
| **Privacy** | Session log content displayed only in review UI; no logging of full transcripts to third parties |
| **Stability** | Graceful degradation when API unavailable — show error banner, preserve session state |
| **Reliability** | Idempotent promote/reject actions; optimistic UI with server reconciliation (Days 6–7) |
| **Performance** | Responsive list/detail for hundreds of candidates (pagination if needed Day 8) |
| **Accessibility** | WCAG AA-oriented contrast; keyboard navigation; meaningful labels (Days 8–10) |
| **Observability** | Toast feedback on actions; hook points for structured action logs (Dominic eval) |
| **Maintainability** | Small modules; one component per file; exhaustive handling of lifecycle states |
| **Scalability** | Stateless Streamlit + external API — horizontal scale via Render/reverse proxy |
| **Portability** | Runs on Windows dev laptop, Render web service, or any Python 3.12+ host |
| **Disaster recovery** | UI state is ephemeral; source of truth is backend KG — redeploy dashboard without data loss |

---

# 7. High-Level System Architecture

## Architectural Style

**Modular presentation layer** with **adapter-based data access**:

- **UI layer** — Streamlit pages, tabs, components (`frontend/app.py` → `frontend/components/`)
- **Application layer** — State transitions, filtering, session management
- **Service layer** — `DataProvider` interface; `MockDataProvider` (now), `ApiDataProvider` (Days 6–7)
- **Integration boundary** — REST (preferred) or GraphQL per team agreement — dashboard is a **client only**

Monica does **not** embed pipeline, eval, or storage logic. Dominic's GitHub hooks and Matthew's DynamoDB remain behind the API.

---

## Pillar Boundary Diagram

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         PRAXIS (team system)                             │
├──────────────────────┬──────────────────────────┬───────────────────────┤
│  Matthew — knowledge/ │  Monica — frontend/       │  Dominic — knowledge/evals/      │
│  ingest·distill·score│  Streamlit human gate     │  harness·hooks·metrics│
│  KG storage          │  review·promote·resolve   │  compounding proof    │
│  deploy: AWS (etc.)  │  deploy: Render (etc.)    │  deploy: TBD / local  │
└──────────┬───────────┴────────────┬─────────────┴───────────┬───────────┘
           │                        │                         │
           │    REST/GraphQL API     │                         │
           │◄───────────────────────┤                         │
           │   candidates, mutations │                         │
           │                        │   eval metrics (embed)  │
           │                        │◄────────────────────────┤
           └────────────────────────┴─────────────────────────┘
                    Agreed data contracts only — no shared code ownership
```

---

## Dashboard Internal Diagram (current → target)

```text
[ Browser ]
     ↓
[ Streamlit app.py ] ──→ [ sidebar: Refresh data · st.session_state ]
     ↓
[ Components: list | detail | contradiction | metrics ]
     ↓
[ DataProvider interface ]
     ├── MockDataProvider  ← mock_data.py (local dev — implemented)
     └── ApiDataProvider   ← PRAXIS_API_BASE_URL (contract v1 client — implemented; awaits Matthew's server)
     ↓
[ Matthew's backend API ] → Knowledge Graph
     ↓
[ Dominic's hooks ] ← promotion events (server-side, not UI-owned)
```

---

## Current Implementation (branch `monica/dashboard-human-gate`)

Delivered as of as-built alignment (2026-06-19):

| File | Status | Description |
|------|--------|-------------|
| `frontend/app.py` | ✅ | Entry — sidebar refresh, filters, API error banner, global selection |
| `frontend/models/candidate.py` | ✅ | Typed contract models + forward-compatible `from_mapping` |
| `frontend/services/data_provider.py` | ✅ | `DataProvider` protocol + env-based factory |
| `frontend/services/contract_v1.py` | ✅ | Canonical v1 payload builders + contract headers |
| `frontend/services/mock_provider.py` | ✅ | In-memory fixtures; audit trail append on mutations |
| `frontend/services/api_client.py` | ✅ | HTTP client — contract v1 (`docs/integration/candidate-api-v1.md`) |
| `frontend/components/candidate_list.py` | ✅ | Table + card views; promote/reject confirmations; low-confidence warning |
| `frontend/components/candidate_detail.py` | ✅ | Detail expander; confidence breakdown; audit trail |
| `frontend/components/confidence_badge.py` | ✅ | State badges + breakdown metrics |
| `frontend/components/contradiction_panel.py` | ✅ | Side-by-side layout + keep-A / keep-B / defer |
| `frontend/components/eval_metrics_embed.py` | ✅ | Compounding curve embed (`PRAXIS_EVAL_METRICS_URL`) |
| `frontend/tests/` | ✅ | Contract fixtures + mock gate workflow tests |
| `frontend/mock_data.py` | ✅ | 17 contract-shaped fixtures (breakdown, contradictions, auditTrail) |
| `frontend/render.yaml` | ✅ | Render.com blueprint |
| `frontend-react/` | ✅ | React client — same contract for Matthew API validation |
| `docs/monica/` | ✅ | Pillar architecture, as-built wireframes, demo/deploy docs |

**Lifecycle logic (mock + API client):**

```python
proposed  --Promote (confirm)-->  suggested  --Promote (confirm)-->  active
   |                                  |                              |
   +-------- Reject (reason?) -------+-------- decayed (no promote) -+
```

Mock `reject` removes from queue and appends audit entry; live mode calls `POST /candidates/{id}/reject`. Contradiction resolve calls `POST /contradictions/{id}/resolve` via `contract_v1.py`.

---

# 8. AI-Native Engineering Model

## AI-First Philosophy (dashboard pillar)

AI assists:

- Wireframe → Streamlit translation
- Component scaffolding and accessibility copy
- Mock candidate generation from JSONL patterns
- Architecture and integration doc drafts

Humans retain authority over:

- Promotion and rejection decisions in the UI
- Data contract approval with Matthew
- UX acceptance criteria and demo script
- Deployment config on Render

---

## Human-in-the-Loop Controls

Aligned with project plan Figure 1 ("Human Approval Gate" nested under Consolidate/Score):

| Control | UI manifestation |
|---------|-------------------|
| Approve credible ideas | Promote buttons with state-aware transitions |
| Resolve contradictions | Side-by-side cards + keep-A / keep-B / defer (mock + API client) |
| Reject bad distillations | Reject action with confirmation + optional reason |
| Audit | Provenance links, timestamps, `auditTrail` panel in detail view |
| Override | Low-confidence promote warning (threshold in `candidate_list.py`) |

---

# 9. Agent Council Review (ACR) — Pillar Touchpoints

| Agent | Dashboard concern |
|-------|-------------------|
| Architect Agent | Module boundaries, contract stability, non-blocking integration |
| Security Agent | Env-based secrets, no log exfiltration, CSRF/API auth as needed |
| Audit Agent | Provenance visible on every row; action logging |
| Verification Agent | Mock contract tests match API schema before integration |
| Documentation Agent | Wireframes, this doc, README run instructions |
| Adversarial Agent | Empty states, API down, duplicate promote, race on rerun |
| Performance Agent | Large candidate lists, Streamlit rerun cost |

**Cursor agent teams:** ACR roles map to the six-role dream team blueprint in [templates_temp/DREAM_AGENT_TEAM.md](templates_temp/DREAM_AGENT_TEAM.md) (`DT-001` baseline for daily `frontend/` work).

**Governance:** No autonomous promotion in production. All API mutations require authenticated human session (implementation TBD with Dominic Days 6–7).

---

# 10. Security Architecture

## Security Philosophy

The dashboard displays potentially sensitive session-derived content. Security is layered and **does not depend on a specific cloud vendor**.

## Requirements (pillar)

| Requirement | Approach |
|-------------|----------|
| Authentication | Defer to deployment wrapper (Render auth, reverse proxy, or SSO) — coordinate with Dominic if unified |
| Authorization | Role-based promote/reject — backend enforces; UI hides actions if unauthorized |
| Audit logging | Log `{action, candidate_id, user, timestamp, old_state, new_state}` to backend |
| Secrets | `PRAXIS_API_BASE_URL`, `PRAXIS_API_TOKEN` via environment only |
| Dependency scanning | Root `pyproject.toml` + GitLab CI when live |
| Prompt injection | Display-only — dashboard does not send user free-text to LLM |
| Data isolation | UI never writes directly to KG; all mutations via API |

## Threat Model (dashboard-specific)

| Threat | Mitigation |
|--------|------------|
| Unauthorized promotion | Backend auth on mutation endpoints |
| Leaked session logs in UI | Deploy over HTTPS; restrict Render URL if needed |
| Mock data mistaken for production | Clear "mock mode" banner when `PRAXIS_API_BASE_URL` unset |
| XSS via candidate content | Streamlit escaping defaults; sanitize if rendering raw HTML |

---

# 11. Privacy & Data Governance

## Data Classification

| Class | Dashboard handling |
|-------|-------------------|
| **Internal** | Candidate titles, distilled lessons, provenance paths |
| **Confidential** | Full session excerpts in detail view — restrict deploy access |
| **Public** | None — dashboard is not public-facing for MVP |

Session JSONL paths reference local or team-agreed storage; the dashboard displays paths, not necessarily hosting the raw files.

---

# 12. Observability Architecture

## Dashboard Observability

| Signal | Mechanism |
|--------|-----------|
| User actions | `st.toast` (now); structured POST to audit endpoint (Days 6–7) |
| API errors | `st.error` with provenance context preserved |
| Load time | Streamlit built-in; optional timing logs |
| Eval correlation | Dominic consumes promotion events + eval run IDs |

Monica does not own Langfuse/OpenTelemetry for the full pipeline — only UI-level hooks and clear error surfaces.

---

# 13. Verification & Evaluation

## Verification Philosophy

Dashboard outputs (promotion events) are **inputs** to Dominic's eval harness — not the measurer itself.

## Evaluation Categories (pillar)

| Category | Method |
|----------|--------|
| Functional correctness | Manual demo script; peer review on MRs |
| Contract compliance | Mock data matches shared JSON schema with Matthew |
| Accessibility | Keyboard walkthrough before Days 9–10 demo |
| Regression | Snapshot tests for filter/promote logic (optional Day 8) |
| Integration | End-to-end promote → KG → eval replay (Days 6–7, with team) |

---

# 14. Repository Governance

## Monica's Ownership Boundary

```text
praxis/
├── frontend/              ← Monica primary ownership (Streamlit human gate)
│   ├── app.py
│   ├── mock_data.py
│   ├── requirements.txt
│   ├── components/        ← implemented
│   ├── services/          ← implemented
│   └── models/            ← implemented
├── frontend-react/        ← React human gate (Monica — Matthew API client; same contract)
├── knowledge/             ← Matthew — distillation, KG, candidate API (planned)
│   └── evals/             ← Dominic — harness, cases, metrics
├── session-capture/       ← Dominic — Go wrapper, DynamoDB capture
├── infra/                 ← Dominic — AWS CDK
├── docs/
│   └── monica/            ← Monica pillar docs (this file, plan, as-built wireframes)
│       ├── ARCHITECTURE_MONICA.md
│       ├── monica-wireframes.md
│       └── Monica-Peters-Dashboard-Plan.md
└── pyproject.toml         ← shared deps — coordinate changes affecting all pillars
```

## Contribution Rules (pillar)

- MRs touching `frontend/` require dashboard-focused review (Monica + one peer).
- MRs touching shared contracts require Matthew's acknowledgment on candidate schema.
- Promotion side-effects (webhooks, PR creation) are **backend/Dominic** — UI emits mutations only.
- Conventional commits: `feat(dashboard):`, `fix(dashboard):`, `docs(dashboard):` with `#<issue>`.

---

# 15. Echelon Engineering File Standards

## File Header Pattern (Python modules in `frontend/`)

New production modules should use the project header template from `docs/monica/templates_temp/CODE_COMMENT_HEADER_TEMPLATE.md`. Example for API client:

```python
"""
===============================================================================
FILE: services/api_client.py
AUTHOR: Monica Peters
CREATED: 2026-06-XX
LICENSE: TBD

PURPOSE:
HTTP client for PRAXIS candidate list and approval mutations.

USAGE:
    client = ApiClient(base_url=os.environ["PRAXIS_API_BASE_URL"])
    candidates = client.list_candidates(state="proposed")

SECURITY:
- Token via PRAXIS_API_TOKEN environment variable only
- No session log content in client logs

OPERATIONAL:
- Swap MockDataProvider for ApiClient in app wiring (Days 6-7)
===============================================================================
"""
```

---

# 16. Deployment Architecture

## Design Principle: Pillar-Sovereign Deployments

Each teammate deploys **their pillar** independently. Monica's dashboard is a **stateless Streamlit process** that calls a configurable API base URL. It does **not** assume Matthew's AWS resources or Dominic's server layout.

| Person | Target (example) | Pillar artifact | Monica dependency |
|--------|-------------------|-----------------|-------------------|
| **Monica** | [Render.com](https://render.com) web service | `frontend/` | `PRAXIS_API_BASE_URL` → Matthew's API when integrated |
| **Matthew** | AWS (DynamoDB, Lambda, etc.) | `knowledge/` + API | Exposes candidate REST endpoints |
| **Dominic** | Local / TBD | `knowledge/evals/` + hooks | Consumes promotion events; optional metrics iframe/embed |

---

## Environments

| Environment | Purpose | Monica config |
|-------------|---------|---------------|
| **Local** | Development | No API URL → mock mode |
| **Dev** | Shared integration | `PRAXIS_API_BASE_URL=https://dev-api...` |
| **Staging** | Pre-demo | Render preview + staging API |
| **Production** | Live demo | Render production service |

---

## Render.com Deployment (Monica)

Minimal `render.yaml` or dashboard settings (Monica-owned, not blocking others):

| Setting | Value |
|---------|-------|
| **Root directory** | `frontend` |
| **Build command** | `pip install -r requirements.txt` |
| **Start command** | `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0` |
| **Env vars** | `PRAXIS_API_BASE_URL`, optional `PRAXIS_API_TOKEN` |
| **Health** | Streamlit HTTP root |

Teammates who do not use Render **ignore this section** — run the same start command on their host.

---

## Local Development

```powershell
cd frontend
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
# Or from repo root: pip install -e .
.\venv\Scripts\streamlit run app.py
```

Mock mode activates automatically when no API URL is configured.

---

## CI/CD Philosophy (pillar)

- Dashboard lint/typecheck via shared GitLab CI when added — must pass before MR merge.
- Monica's Render deploy is **decoupled** from Matthew's AWS pipeline and Dominic's eval runner.
- No single shared deploy gate that blocks one pillar waiting on another.

---

# 17. Integration Architecture & Data Contracts

## Contract-First Integration (Matthew ↔ Monica)

The dashboard and pipeline integrate **only** through agreed JSON shapes. **Canonical contract:** [candidate-api-v1.md](../integration/candidate-api-v1.md). Monica's `Candidate.from_mapping` preserves unknown fields in `extra`.

### Candidate (read model)

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `id` | string | ✅ | Stable identifier |
| `title` | string | ✅ | Distilled lesson title |
| `content` | string | ✅ | Full lesson body |
| `state` | enum | ✅ | `proposed` \| `suggested` \| `active` \| `decayed` |
| `confidence` | float | ✅ | 0–1 aggregate |
| `confidenceBreakdown` | object | ✅ mock | `{ frequency, recency, breadth }` + optional rationale strings |
| `provenance` | string | ✅ | `logs/<file>.jsonl:<line>` |
| `createdAt` | ISO 8601 | ✅ | Creation timestamp |
| `contradictions` | array | ✅ mock | IDs or embedded rival candidates |
| `auditTrail` | array | ✅ mock | `{ action, timestamp, provenance, actor, note? }` |
| *extension keys* | any | optional | Preserved in `Candidate.extra`; shown in detail view |

**Forward compatibility:** `from_mapping` accepts camelCase and snake_case aliases for provenance and timestamps, tolerates unknown `state` values (displayed as-is), and never drops undeclared API fields.

### Approval mutations (write model)

| Action | Payload | Owner |
|--------|---------|-------|
| `POST /candidates/{id}/promote` | `{ "targetState": "suggested" \| "active" }` | Matthew API; Dominic webhook side-effect |
| `POST /candidates/{id}/reject` | `{ "reason": string? }` | Matthew API |
| `POST /contradictions/{id}/resolve` | `{ "resolution": "keep_a" \| "keep_b", "keepId": string }` | Matthew API; `merge` stretch |

**Contradiction id:** `{primaryId}__{rivalId}`. Dashboard maps UI labels via `frontend/services/contract_v1.py`.

**Promote fallback:** Client retries with `{}` if server returns 400/422 on explicit `targetState`.

**Versioning:** Prefix contract version in API path or header (`X-Praxis-Contract: 1`) when schema evolves — Monica's client checks version at startup.

---

## DataProvider Abstraction (implemented)

```python
# frontend/services/data_provider.py

class DataProvider(Protocol):
    def list_candidates(self, state: CandidateState | None = None) -> list[Candidate]: ...
    def get_candidate(self, candidate_id: str) -> Candidate | None: ...
    def promote(self, candidate_id: str) -> Candidate: ...
    def reject(self, candidate_id: str, reason: str | None = None) -> None: ...
```

- `MockDataProvider` — `frontend/services/mock_provider.py` (default when `PRAXIS_API_BASE_URL` unset)
- `ApiDataProvider` — `frontend/services/api_client.py` (contract v1; tolerant read + explicit mutations)

UI components depend on `DataProvider`, not on pandas or HTTP directly. **`frontend-react/`** calls the same HTTP endpoints — it never imports Streamlit modules.

---

## Non-Blocking Integration Checklist

Before merging integration MRs:

- [ ] Mock data still works with zero backend (Monica local dev unblocked)
- [ ] API URL is optional env var, not hard-coded host
- [ ] No imports from `knowledge/` or `knowledge/evals/` inside `frontend/`
- [ ] Matthew implements server to [candidate-api-v1.md](../integration/candidate-api-v1.md) + fixtures
- [ ] Dominic confirms promotion events reach hooks without UI knowing GitHub details
- [ ] Failed API calls do not corrupt local session state irreversibly

---

# 18. Modular Structure (implemented)

Extracted from the Day 2 monolith per **models → services → components → slim app.py**. Each module has a single owner concern and **zero imports from `knowledge/` or `knowledge/evals/`**.

```text
frontend/
├── app.py                      # Entry: page config, provider wiring, layout only
├── components/
│   ├── candidate_list.py       # ✅ Table + card views, filter, promote/reject actions
│   ├── candidate_detail.py     # ✅ Detail expander (confidence + audit trail)
│   ├── contradiction_panel.py  # ✅ Side-by-side resolve actions
│   ├── confidence_badge.py     # ✅ State badges + confidence breakdown metrics
│   └── eval_metrics_embed.py   # ✅ Compounding curve embed (eval-metrics-v1)
├── models/
│   └── candidate.py            # ✅ Candidate, CandidateState, ConfidenceBreakdown
├── services/
│   ├── data_provider.py        # ✅ Protocol + get_data_provider() factory
│   ├── mock_provider.py        # ✅ In-memory fixtures for local dev
│   ├── contract_v1.py          # ✅ Canonical v1 payload builders
│   └── api_client.py           # ✅ ApiDataProvider — contract v1 HTTP client
├── tests/
│   └── test_contract_fixtures.py  # ✅ Fixture contract tests
├── mock_data.py                # ✅ Static fixtures as contract-shaped dicts
├── requirements.txt
└── .streamlit/
    └── config.toml             # ✅ Theme / a11y-oriented defaults
```

### Module boundary map (non-blocking guarantees)

| Module | Owns | Must never |
|--------|------|------------|
| `models/` | Typed API contract (`Candidate`, states, promotion helpers) | Import Streamlit, HTTP, `knowledge/`, `knowledge/evals/` |
| `services/` | Data access (`DataProvider`, mock + API clients) | Render UI or embed pipeline logic |
| `components/` | Streamlit presentation only | Call DynamoDB, GitHub, or eval scripts directly |
| `app.py` | Provider singleton in `st.session_state`, page shell | Business logic beyond wiring |
| `mock_data.py` | Dev/demo fixtures | Be required in production when API URL is set |

**Teammate impact:** Matthew implements the server behind `ApiDataProvider` endpoints in `knowledge/` on his AWS stack. Dominic reads promotion events from that API / hooks in `knowledge/evals/` — not from Streamlit session state. A React developer adds `frontend-react/` and calls the same endpoints without modifying any file above.

**Extraction status:** Complete (2026-06-19). Streamlit + React clients share contract v1; live E2E awaits Matthew's API server.

---

# 19. Sprint Alignment (Monica deliverables)

From [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) and [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md):

| Day | Deliverable | Status |
|-----|-------------|--------|
| 1 | Wireframes, Streamlit stack decision | ✅ Done |
| 2 | Dashboard shell + candidate list | ✅ Done |
| 3 | Candidate detail + confidence UI | ✅ Done — breakdown metrics, audit trail, global selection |
| 4 | Human gate workflow UI polish | ✅ Done — confirmations, transition feedback, empty/error states |
| 5 | Contradiction resolution + credibility viz | ✅ Done — mock resolve + breakdown tooltips |
| 6 | API integration + approval actions | ✅ Client ready — `ApiDataProvider` wired; awaits Matthew's server |
| 7 | Full approval flow + provenance in UI | ✅ Done on mock; live audit from API when available |
| 8 | Edge-case polish + eval embed support | ✅ Done — `PRAXIS_EVAL_METRICS_URL`, 409 handling, API error banner |
| 9–10 | Demo-ready + user flow video | 🔲 Rehearsal + video capture |

---

# 20. Failure Modes & Recovery

| Failure | Expected behavior |
|---------|-------------------|
| API unreachable | Banner: "Backend unavailable — showing last loaded data" or mock fallback in dev |
| Promote conflict (409) | Toast error; refresh candidate from server |
| Streamlit rerun mid-action | `st.session_state` preserves list; idempotent API calls |
| Empty candidate list | Friendly empty state — not an error |
| Stale confidence after Matthew retune | Refresh button; show `updatedAt` when available |
| Render cold start | Acceptable for demo; document startup time |

---

# 21. Future Expansion (pillar)

| Item | Notes |
|------|-------|
| Pagination / virtual scroll | If candidate count exceeds ~200 |
| Bulk promote/reject | Stretch — with strong audit warnings |
| Dark mode / theme tokens | Streamlit config |
| Read-only public demo mode | Mock-only deploy for portfolio |
| **`frontend-react/` sibling app** | **Shipped** — parallel React UI; same API contract ([§2.4](#24-react-coexistence--no-blockers-for-teammates)) |
| Cross-project candidate filtering | When Matthew's graph supports scope metadata |

---

# 22. Final Engineering Position

Monica's dashboard pillar is designed according to:

- **MoniGarr Operating Model (M.O.M.)** — human accountability, documentation as infrastructure, handoff-ready modules
- **M.I.L.E.** — intelligence-led UX that makes confidence and provenance legible
- **Echelon standards** — file headers, contract-first integration, enterprise-appropriate security posture

**This pillar prioritizes:**

- Human-in-the-loop quality gates the project plan requires
- **Streamlit for research-data review visuals** — Monica's deliberate pillar choice, not a repo-wide UI mandate
- **API-first integration** so React-only teammates (now or later) share the same backend without forking pipeline code
- Modular Python under `frontend/` that any contributor can run, extend, or leave untouched
- Strict ownership boundaries so Matthew's AWS, Dominic's eval spine, and Monica's Render deploy coexist without mutual blockers
- Interview-ready transparency: every promotion is visible, evidenced, and measurable

AI accelerates the UI.

Humans approve the knowledge.

Teammates own their deploys.

The dashboard connects them through contracts — not through shared mutable internals.

---

## Related Documents

| Document | Relationship |
|----------|--------------|
| [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) | HTML mirror + architecture diagram |
| [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md) | Sprint plan |
| [monica-wireframes.md](monica-wireframes.md) | As-built UX spec |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Live demo + user-flow video beats |
| [PLAN_ALIGNMENT_GAP_CHECKLIST.md](PLAN_ALIGNMENT_GAP_CHECKLIST.md) | Scrum Master gap tracker |
| [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) | Demo rehearsal checklist |
| [STANDUP_TEMPLATE.md](STANDUP_TEMPLATE.md) | Daily 10 AM standup with Tom Tarpey |
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Render.com deploy notes + cold-start expectations |
| [Matthew-Daw-ML-Pipeline-PlanDRAFT.md](../plans/Matthew-Daw-ML-Pipeline-PlanDRAFT.md) | Upstream data producer |
| [Dominic-Antonelli-Architecture-Eval-PlanDRAFT.md](../plans/Dominic-Antonelli-Architecture-Eval-PlanDRAFT.md) | Downstream measurement |
| [README.md](../README.md) | Repo overview and run instructions |
| [.cursor/rules/praxis-dashboard.mdc](../../.cursor/rules/praxis-dashboard.mdc) | Editor/agent patterns |
| [templates_temp/DREAM_AGENT_TEAM.md](templates_temp/DREAM_AGENT_TEAM.md) | ACR → Cursor agent team blueprint (`DT-001`–`DT-003`) |
