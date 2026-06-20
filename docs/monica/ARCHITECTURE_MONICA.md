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
Version:            0.3.0 (Days 1–8 React dashboard complete; Python contract layer in frontend/)
Status:             Active development — React UI on mock; awaiting Matthew's live API
Classification:     Internal — capstone sprint
Created:            2026-06-18
Last Updated:       2026-06-19 (React-only dashboard posture; frontend/ = contract/mock layer)
Source of Truth:    docs/plans/PRAXIS_Project_Plan.html
License:            TBD — Gauntlet AI capstone (2026)
============================================================================

DESCRIPTION
----------------------------------------------------------------------------
Architecture definition for Monica's pillar only: the React human-gate
dashboard in frontend-react/ and the Python contract/mock layer in frontend/.
This document does NOT prescribe Matthew's pipeline,
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

The dashboard UI is a **Vite + React + TypeScript** application under `frontend-react/`. Shared **Python contract models, mock fixtures, and API client** live in `frontend/` (models, services, `mock_data.py`, tests) — not a presentation layer. Stack definition and repo layout are in [§2 Tech Stack & Presentation Architecture](#2-tech-stack--presentation-architecture-monicas-decision).

The UI consumes candidate data from Matthew's pipeline via a **contract-first API boundary** (Days 6–7) and surfaces audit-friendly actions that Dominic's eval harness can measure. Monica's deploy target is **Render.com** (React static site + Matthew's API service); teammates retain full sovereignty over their own hosting choices.

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

- **AI-First Engineering** — React + TypeScript for the review UI; Python contract layer for mocks and API parity; AI assists wireframes, components, and docs; humans approve all promotions.
- **Human-in-the-loop accountability** — No candidate reaches `active` without explicit human action in the dashboard.
- **Sovereign engineering** — Monica owns `frontend-react/` UX and `frontend/` contract layer; Matthew owns distillation/scoring truth; Dominic owns measurement and integration hooks. No pillar blocks another.
- **Documentation as infrastructure** — Wireframes, data contracts, and this architecture doc are onboarding artifacts for teammates and future users.
- **Handoff-ready engineering** — Clear module boundaries, typed contracts, env-driven configuration, and mock providers so Matthew can integrate before the API exists.

---

# 2. Tech Stack & Presentation Architecture (Monica's Decision)

This section documents **Monica Peters' pillar-specific** architecture and technology choices. It is authoritative for the human-gate dashboard only. It does **not** override Matthew's pipeline stack or Dominic's eval/integration stack.

## 2.1 Monica's Tech Stack (Human Gate Pillar)

| Layer | Choice | Version / notes |
|-------|--------|-----------------|
| **UI framework** | [React](https://react.dev/) + [Vite](https://vitejs.dev/) + TypeScript | Human-gate dashboard in `frontend-react/` |
| **Contract / mocks** | Python 3.12+ | `frontend/models/`, `frontend/services/`, `frontend/mock_data.py`, `frontend/tests/` |
| **Visualization** | React components (Recharts, custom graph views) | Confidence breakdown, eval compounding curve, KG explorer |
| **State** | React hooks + context | Ephemeral UI state; backend KG is source of truth |
| **Integration** | HTTP client | REST to Matthew's API — shared contract v1 |
| **Local deps** | `frontend-react/package.json` + root `pyproject.toml` | React UI deps isolated in `frontend-react/` |
| **Deploy target** | Render.com static site + API service | Monica-owned blueprint in `frontend-react/render.yaml` |

**Ownership boundary:** Presentation lives in `frontend-react/`. Python under `frontend/` is the **contract and mock-data package** — typed DTOs, `DataProvider` protocol, `ApiClient`, fixtures, and pytest — not a UI runtime.

---

## 2.2 Architecture Pattern: API-First, Thin Clients

Monica's presentation architecture follows **API-first, contract-stable clients**:

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
   │ Python        │      │ React UI      │      │ Dominic       │
   │ contract+mock │      │ Monica · now  │      │ no UI required│
   └───────────────┘      └───────────────┘      └───────────────┘
```

**Invariant:** Business logic for distillation, scoring, storage, and promotion side-effects stays in **Matthew's `knowledge/` + Dominic's hooks**. Neither the React app nor the Python contract layer embeds that logic — both are thin clients over the same HTTP contract ([§17 Integration Architecture](#17-integration-architecture--data-contracts)).

---

## 2.3 Repo Layout — UI vs Contract Layer

| Directory | Role | Run command |
|-----------|------|-------------|
| `frontend-react/` | **Dashboard UI** — list, detail, promote/reject, contradictions, eval embed | `npm run dev` |
| `frontend/` | **Contract + mock layer** — `Candidate` models, `DataProvider`, `api_client.py`, `mock_data.py`, pytest | `uv run pytest frontend/tests/ -q` |

Matthew and Dominic integrate via API and JSON contracts — they never need the React dev server for their pillars. Mock fixtures in `frontend/mock_data.py` export to `frontend-react/public/mock-candidates.json` via `scripts/export-mock-candidates.py`.

**Interview framing:** *"I built a React human-gate dashboard with a contract-first Python layer so Matthew could validate the API against typed fixtures and pytest before the UI ever needed a live server."*

Pillar docs live under `docs/monica/` (canonical home for Monica's documentation).

---

## 2.4 Research Visualization Roadmap (React)

Planned visuals in the dashboard (Days 3–8):

| Visual | React mechanism | Research purpose |
|--------|-----------------|------------------|
| Confidence breakdown | `ConfidenceBreakdown.tsx` + metrics grid | Show freq/recency/breadth rationale |
| Candidate comparison | `ContradictionPanel.tsx` side-by-side | Contradiction resolution |
| Compounding curve | `EvalMetricsEmbed.tsx` | Dominic's eval: correction rate falling over sessions |
| Provenance audit | Detail panel + metadata grid | Link every lesson to JSONL line evidence |
| State distribution | `StateFunnel.tsx` / graph views | Demo narrative: proposed → active funnel |
| Knowledge graph | `KnowledgeGraphView.tsx` | Scope and relationship exploration |

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
| **AI acceleration** | React component scaffolding, mock data generation, contract tests |

All three remain visible in the UI — nothing is hidden behind a black-box "approve all" control.

---

## 3.3 Enterprise from Day One (Pillar Scope)

Within `frontend/` (contract layer) and `frontend-react/` (UI):

- **Security** — No secrets in code; API URL via environment; read-only log paths in UI where possible.
- **Maintainability** — Modular packages: Python `models/` + `services/`; React `components/` + `api/`.
- **Observability** — User-visible toasts and error states; structured logging hook points for Dominic's eval (Days 6–7).
- **Extensibility** — `DataProvider` abstraction in Python; React `providerFactory.ts` — swap mock → API without UI rewrites.
- **Documentation** — [monica-wireframes.md](monica-wireframes.md), [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md), this file.
- **Handoff** — Any teammate runs `npm run dev` in `frontend-react/` with mocks; pytest in `frontend/tests/` validates contract parity; no PostgreSQL lock-in required for Monica's pillar (Matthew owns DB setup).

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

1. Clone the repo, `cd frontend-react`, `npm install`, run the dashboard with mock data in under five minutes.
2. Point `VITE_PRAXIS_API_BASE_URL` at Matthew's pipeline API when ready — no code fork required.
3. Run `uv run pytest frontend/tests/ -q` to validate contract fixtures against Matthew's schema.
4. Deploy to Render (Monica), Matthew's PostgreSQL-backed API host, or local (Dominic) using only pillar-specific config — see [§16 Deployment Architecture](#16-deployment-architecture).

---

# 4. System Scope

## In Scope (Monica's pillar)

| Area | Responsibility |
|------|----------------|
| **Human-gate UI** | React dashboard (`frontend-react/`): candidate list, detail view, filters, search, KG explorer |
| **Lifecycle workflow** | `proposed → suggested → active` promotion and reject flows |
| **Provenance display** | Every item shows `logs/<file>.jsonl:<line>` (or agreed canonical form) |
| **Confidence UX** | Aggregate score now; freq/recency/breadth breakdown + tooltips (Days 3–5) |
| **Contradiction resolution** | Side-by-side comparison cards + resolution actions (Day 5) |
| **Credibility metrics viz** | Visual indicators supporting promotion decisions (Day 5) |
| **API client layer** | Thin HTTP client calling Matthew's backend; no pipeline logic in UI |
| **Mock data provider** | Local development without blocking on pipeline readiness (`frontend/mock_data.py` → exported JSON) |
| **Accessibility** | Keyboard-friendly flows, high contrast, screen-reader labels (Days 8–10 polish) |
| **Eval embed points** | Optional panels/hooks for Dominic's compounding-curve widgets (coordinate Day 8) |

---

## Out of Scope (owned by teammates — do not implement in `frontend/`)

| Area | Owner | Rationale |
|------|-------|-----------|
| JSONL ingestion, episode segmentation | Matthew | Pipeline correctness |
| Learning-moment detection, LLM distillation | Matthew | ML/KG engine |
| Embeddings, HDBSCAN, clustering, decay rules | Matthew | Scoring source of truth |
| Knowledge Graph storage (PostgreSQL — Matthew sets up schema/migrations) | Matthew | Backend persistence |
| get-context tool, CLAUDE.md / skills generation | Matthew + Dominic | Injection substrate |
| Eval harness, cold vs injected runs, compounding curve computation | Dominic | Measurement spine |
| GitHub hook / PR automation on promotion | Dominic | Integration layer |
| Team-wide CI/CD, repo deployment topology | Dominic (+ shared agreement) | Each pillar deploys independently |

**Non-blocking rule:** Changes under `frontend-react/` and the Python contract layer in `frontend/` must not require edits to `knowledge/` or `knowledge/evals/` to run. Integration is **pull-based** (dashboard calls API) or **env-configured**, never hard-coded to Matthew's PostgreSQL host or Dominic's server.

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
| **Scalability** | Stateless React static site + external API — CDN-friendly deploy |
| **Portability** | Runs in any modern browser; dev server via Vite |
| **Disaster recovery** | UI state is ephemeral; source of truth is backend KG — redeploy dashboard without data loss |

---

# 7. High-Level System Architecture

## Architectural Style

**Modular presentation layer** with **adapter-based data access**:

- **UI layer** — React pages and components (`frontend-react/src/App.tsx` → `frontend-react/src/components/`)
- **Application layer** — State transitions, filtering, selection hooks
- **Service layer** — `apiClient.ts` + `mockProvider.ts` (React); `DataProvider` protocol + `ApiClient` (Python contract layer)
- **Integration boundary** — REST per team agreement — dashboard is a **client only**

Monica does **not** embed pipeline, eval, or storage logic. Dominic's GitHub hooks and Matthew's PostgreSQL-backed API remain behind the API boundary.

---

## Pillar Boundary Diagram

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                         PRAXIS (team system)                             │
├──────────────────────┬──────────────────────────┬───────────────────────┤
│  Matthew — knowledge/ │  Monica — frontend-react/   │  Dominic — knowledge/evals/      │
│  ingest·distill·score│  React human gate           │  harness·hooks·metrics│
│  KG storage (PG)     │  + frontend/ contract layer │  compounding proof    │
│  deploy: TBD         │  deploy: Render (etc.)    │  deploy: TBD / local  │
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
[ React App (frontend-react/src/App.tsx) ] ──→ [ sidebar: Refresh · filters · data source badge ]
     ↓
[ Components: CandidateTable | CandidateDetail | ContradictionPanel | EvalMetricsEmbed | KnowledgeGraphView ]
     ↓
[ apiClient.ts / mockProvider.ts ]
     ├── mock-candidates.json  ← exported from frontend/mock_data.py (local dev)
     └── HTTP client           ← VITE_PRAXIS_API_BASE_URL (contract v1 — Matthew's server)
     ↓
[ Matthew's backend API ] → Knowledge Graph
     ↓
[ Dominic's hooks ] ← promotion events (server-side, not UI-owned)
```

**Python contract layer (parallel, no UI):**

```text
frontend/models/candidate.py  →  frontend/services/data_provider.py
                                    ├── mock_provider.py  (pytest + export source)
                                    └── api_client.py     (live API smoke tests)
```

---

## Current Implementation (branch `monica/dashboard-human-gate`)

Delivered as of as-built alignment (2026-06-19):

| File | Status | Description |
|------|--------|-------------|
| `frontend-react/src/App.tsx` | ✅ | Entry — layout, filters, API error banner, global selection |
| `frontend-react/src/api/apiClient.ts` | ✅ | HTTP client — contract v1 |
| `frontend-react/src/api/mockProvider.ts` | ✅ | In-memory fixtures from exported JSON |
| `frontend-react/src/components/*` | ✅ | Table, cards, detail, contradictions, eval embed, graph views |
| `frontend/models/candidate.py` | ✅ | Typed contract models + forward-compatible `from_mapping` |
| `frontend/services/data_provider.py` | ✅ | `DataProvider` protocol + env-based factory |
| `frontend/services/contract_v1.py` | ✅ | Canonical v1 payload builders + contract headers |
| `frontend/services/mock_provider.py` | ✅ | In-memory fixtures; audit trail append on mutations |
| `frontend/services/api_client.py` | ✅ | Python HTTP client — contract v1 (pytest / live smoke) |
| `frontend/tests/` | ✅ | Contract fixtures + mock gate workflow + live API smoke |
| `frontend/mock_data.py` | ✅ | 17+ contract-shaped fixtures (source for mock JSON export) |
| `frontend-react/render.yaml` | ✅ | Render.com blueprint (API + static site) |
| `scripts/export-mock-candidates.py` | ✅ | Syncs `mock_data.py` → `frontend-react/public/` |
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

- Wireframe → React component translation
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
| Override | Low-confidence promote warning (threshold in React list/card components) |

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
| Performance Agent | Large candidate lists, bundle size, render performance |

**Cursor agent teams:** ACR roles map to the six-role dream team blueprint in [templates_temp/DREAM_AGENT_TEAM.md](templates_temp/DREAM_AGENT_TEAM.md) (`DT-001` baseline for daily `frontend-react/` + contract layer work).

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
| XSS via candidate content | React default escaping; sanitize if rendering raw HTML |

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
| User actions | Toast/banner feedback (React); structured POST to audit endpoint (Days 6–7) |
| API errors | Error banner with provenance context preserved |
| Load time | Browser devtools; optional timing logs |
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
├── frontend/              ← Monica: Python contract + mock layer (models, services, tests)
│   ├── mock_data.py
│   ├── models/
│   ├── services/
│   └── tests/
├── frontend-react/        ← Monica: React human-gate UI (primary deliverable)
│   ├── src/
│   ├── public/mock-candidates.json
│   └── render.yaml
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

- MRs touching `frontend-react/` or the Python contract layer in `frontend/` require dashboard-focused review (Monica + one peer).
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

Each teammate deploys **their pillar** independently. Monica's dashboard is a **static React site** (or Vite dev server locally) that calls a configurable API base URL. It does **not** assume Matthew's PostgreSQL resources or Dominic's server layout.

| Person | Target (example) | Pillar artifact | Monica dependency |
|--------|-------------------|-----------------|-------------------|
| **Monica** | [Render.com](https://render.com) static site + API service | `frontend-react/` | `VITE_PRAXIS_API_BASE_URL` → Matthew's API when integrated |
| **Matthew** | PostgreSQL + API server (`knowledge/`) | `knowledge/` + API | Exposes candidate REST endpoints |
| **Dominic** | Local / TBD | `knowledge/evals/` + hooks | Consumes promotion events; optional metrics iframe/embed |

---

## Environments

| Environment | Purpose | Monica config |
|-------------|---------|---------------|
| **Local** | Development | No API URL → mock mode (exported JSON) |
| **Dev** | Shared integration | `VITE_PRAXIS_API_BASE_URL=https://dev-api...` |
| **Staging** | Pre-demo | Render preview + staging API |
| **Production** | Live demo | Render production static site + API |

---

## Render.com Deployment (Monica)

Blueprint: `frontend-react/render.yaml` — `praxis-candidate-api` + `praxis-react-human-gate`. Full settings in [RENDER_DEPLOY.md](RENDER_DEPLOY.md).

| Setting | Value |
|---------|-------|
| **Static site root** | `frontend-react` |
| **Build command** | `npm install && npm run build` |
| **Publish directory** | `dist` |
| **Env vars (build-time)** | `VITE_PRAXIS_API_BASE_URL`, optional `VITE_PRAXIS_API_TOKEN`, `VITE_PRAXIS_EVAL_METRICS_URL` |
| **Health** | Static asset root |

Teammates who do not use Render **ignore this section** — run `npm run dev` locally.

---

## Local Development

**Dashboard UI:**

```powershell
cd frontend-react
npm install
npm run dev
```

Open http://localhost:5173 — mock mode when `VITE_PRAXIS_API_BASE_URL` is unset.

**Contract layer (pytest):**

```powershell
uv run pytest frontend/tests/ -q
```

Sync mock JSON after editing fixtures:

```powershell
python scripts/export-mock-candidates.py
```

---

## CI/CD Philosophy (pillar)

- Dashboard lint/typecheck via shared GitLab CI when added — must pass before MR merge.
- Monica's Render deploy is **decoupled** from Matthew's PostgreSQL-backed pipeline and Dominic's eval runner.

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

UI components depend on the API client / mock provider, not on HTTP details scattered in views. **`frontend/`** Python layer mirrors the same contract for pytest and Matthew's server validation.

---

## Backend persistence (Matthew — PostgreSQL)

Matthew owns PostgreSQL setup for the repo's Knowledge Graph and candidate API persistence:

- **`PRAXIS_DB_URL`** (or **`PRAXIS_DB_SECRET`** → AWS Secrets Manager), schema bootstrap (`python -m knowledge.serve.db`), and promote/reject/resolve persistence live in `knowledge/` (Matthew's pillar). Full runbook: [RDS_KG_DEPLOY.md](RDS_KG_DEPLOY.md).
- Monica's `ApiDataProvider` and the React client consume REST only — **no DB env vars in `frontend/` or `frontend-react/`**.
- Vector or embedding stores may use separate backends later; relational KG/candidate state lives in PostgreSQL (RDS 16 + pgvector).

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

**React UI** (`frontend-react/`) and **Python contract layer** (`frontend/`) share the candidate-api-v1 contract. Neither imports from `knowledge/` or `knowledge/evals/`.

```text
frontend-react/
├── src/
│   ├── App.tsx                     # Entry: layout, filters, provider wiring
│   ├── api/
│   │   ├── apiClient.ts            # HTTP client — contract v1
│   │   ├── mockProvider.ts         # Mock fixtures from exported JSON
│   │   └── providerFactory.ts      # Mock vs live factory
│   └── components/
│       ├── CandidateTable.tsx      # Table + promote/reject actions
│       ├── CandidateDetail.tsx     # Detail + confidence + audit trail
│       ├── ContradictionPanel.tsx  # Side-by-side resolve actions
│       ├── EvalMetricsEmbed.tsx    # Compounding curve embed
│       └── graph/                  # KG explorer views
└── public/
    └── mock-candidates.json        # Exported from frontend/mock_data.py

frontend/                           # Contract + mock layer (no UI)
├── models/
│   └── candidate.py                # Candidate, CandidateState, ConfidenceBreakdown
├── services/
│   ├── data_provider.py            # Protocol + get_data_provider() factory
│   ├── mock_provider.py            # In-memory fixtures for pytest
│   ├── contract_v1.py              # Canonical v1 payload builders
│   └── api_client.py               # ApiDataProvider — contract v1 HTTP client
├── tests/
│   └── test_*.py                   # Contract fixtures + gate workflow + live smoke
└── mock_data.py                    # Static fixtures — source of truth for mock JSON
```

### Module boundary map (non-blocking guarantees)

| Module | Owns | Must never |
|--------|------|------------|
| `frontend/models/` | Typed API contract (`Candidate`, states, promotion helpers) | Import UI frameworks, `knowledge/`, `knowledge/evals/` |
| `frontend/services/` | Data access (`DataProvider`, mock + API clients) | Render UI or embed pipeline logic |
| `frontend-react/src/components/` | React presentation only | Call PostgreSQL, GitHub, or eval scripts directly |
| `frontend-react/src/api/` | HTTP + mock providers | Embed pipeline or distillation logic |
| `frontend/mock_data.py` | Dev/demo fixtures + export source | Be required in production when API URL is set |

**Teammate impact:** Matthew implements the server behind the shared endpoints in `knowledge/` on his PostgreSQL-backed stack. Dominic reads promotion events from that API / hooks in `knowledge/evals/`. The React UI and Python contract layer call the same endpoints without modifying pipeline code.

**Extraction status:** Complete (2026-06-19). React UI + Python contract layer share contract v1; live E2E awaits Matthew's API server on staging.

---

# 19. Sprint Alignment (Monica deliverables)

From [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) and [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md):

| Day | Deliverable | Status |
|-----|-------------|--------|
| 1 | Wireframes, React stack decision | ✅ Done |
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
| Promote conflict (409) | Toast/banner error; refresh candidate from server |
| Mid-action navigation | React state preserves selection where possible; idempotent API calls |
| Empty candidate list | Friendly empty state — not an error |
| Stale confidence after Matthew retune | Refresh button; show `updatedAt` when available |
| Render cold start | API service only — React static site has no cold start |

---

# 21. Future Expansion (pillar)

| Item | Notes |
|------|-------|
| Pagination / virtual scroll | If candidate count exceeds ~200 |
| Bulk promote/reject | Stretch — with strong audit warnings |
| Dark mode / theme tokens | CSS variables in `frontend-react/` |
| Read-only public demo mode | Mock-only deploy for portfolio |
| Cross-project candidate filtering | When Matthew's graph supports scope metadata |

---

# 22. Final Engineering Position

Monica's dashboard pillar is designed according to:

- **MoniGarr Operating Model (M.O.M.)** — human accountability, documentation as infrastructure, handoff-ready modules
- **M.I.L.E.** — intelligence-led UX that makes confidence and provenance legible
- **Echelon standards** — file headers, contract-first integration, enterprise-appropriate security posture

**This pillar prioritizes:**

- Human-in-the-loop quality gates the project plan requires
- **React dashboard** for research-data review visuals — Monica's pillar deliverable
- **Python contract layer** in `frontend/` for typed mocks, pytest, and API parity
- **API-first integration** so Matthew and Dominic share the same backend without UI coupling
- Strict ownership boundaries so Matthew's PostgreSQL + API, Dominic's eval spine, and Monica's Render deploy coexist without mutual blockers
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
