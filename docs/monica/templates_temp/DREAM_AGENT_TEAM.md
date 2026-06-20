# PRAXIS Dashboard & Human Gate — Dream Agent Team

**Pillar owner:** Monica Peters · **Branch:** `monica/dashboard-human-gate` · **Scope:** `frontend-react/` React human-gate dashboard + `frontend/` Python contract/mock layer

**Aligned sources:**

| Document | Role |
|----------|------|
| [ARCHITECTURE_MONICA.md](../ARCHITECTURE_MONICA.md) | Pillar architecture, ACR touchpoints, contracts, M.O.M. |
| [monica-wireframes.md](../monica-wireframes.md) | As-built screens, candidate data contract |
| [Monica-Peters-Dashboard-Plan.md](../Monica-Peters-Dashboard-Plan.md) | Sprint deliverables Days 1–10 |
| [PRAXIS_Project_Plan.html](../../plans/PRAXIS_Project_Plan.html) | Team schedule, interview claim, system diagram |
| [PRD.pdf](../../PRD.pdf) | Capstone framing: technically ambitious, demo what shipped |
| [proposal-praxis.md](../../plans/proposal-praxis.md) | Human gate lifecycle, provenance, ≥50% correction-reduction narrative |

**Interview claim (project plan):** *"I led the design and built the human approval dashboard that enforces quality gates and makes knowledge promotion transparent and measurable."*

**Governance invariant:** No autonomous promotion. All `proposed → suggested → active` transitions require explicit human action in the UI; agents assist build and review only.

---

## Team Blueprint (Copy/Paste)

### Team: `DT-XXX — Team Name`

**Purpose:**  
Cursor agent team optimized for Monica Peters' **Dashboard & Human Gate** pillar — React review UI, lifecycle workflow, provenance display, contradiction resolution, Python contract layer, and API-first integration with Matthew's pipeline and Dominic's eval embed points.

**Hypothesis:**  
A six-role council (mapped from [Agent Council Review (ACR)](../ARCHITECTURE_MONICA.md#9-agent-council-review-acr--pillar-touchpoints)) produces contract-stable, demo-ready dashboard increments without blocking teammates or embedding pipeline logic in `frontend/`.

**When To Use:**  
- Building or refactoring `frontend-react/` (components, api, hooks) or the Python contract layer in `frontend/`
- Updating pillar docs (`docs/monica/`, wireframes, architecture)
- Preparing Days 6–7 integration MRs against Matthew's candidate API
- Day 8–10 polish: accessibility, edge cases, eval metrics embed, demo Act 2 script

**Out of scope for this team:** `knowledge/` distillation, `knowledge/evals/` harness computation, PostgreSQL/KG storage (Matthew), GitHub hook implementation (Dominic).

#### Agent Roles

| Role | Agent Name | Model | Function | Trust Level |
|------|------------|-------|----------|-------------|
| Orchestrator | | | Sprint routing: Day deliverables from [Monica-Peters-Dashboard-Plan.md](../Monica-Peters-Dashboard-Plan.md); integration stop conditions (mock still works, no `knowledge/` imports); demo Act 2 checklist | Medium |
| Architect | | | Module boundaries (`frontend/` models/services + `frontend-react/` components/api); `DataProvider` contract stability ([§2](../ARCHITECTURE_MONICA.md#2-tech-stack--presentation-architecture-monicas-decision)) | Medium-high |
| Adversarial UX | | | Edge cases per ACR: empty candidate list, API unreachable, duplicate promote, mid-action navigation, mock-vs-live confusion, stale confidence | Low |
| Verification | | | `Candidate.from_mapping()` forward compatibility; mock fixtures match API schema; exhaustive `CandidateState` handling; promote/reject idempotency expectations for Days 6–7 | Medium |
| Documentation | | | Wireframes, architecture deltas, provenance/confidence UX rationale; MR descriptions with `#<issue>`; handoff run instructions (`npm run dev` in `frontend-react/`) | Medium |
| Security & Performance | | | `PRAXIS_API_*` / `VITE_PRAXIS_*` env-only secrets; HTTPS deploy posture; no log exfiltration; list/detail perf for ~200 candidates; bundle size awareness | High |

#### Run Notes + Scorecard

| Date | Sprint Day / Target | Scope | Contract OK | A11y Spot-check | Edge Cases Covered | Integration Risk | Demo Narrative (1–10) | Keep? |
|------|---------------------|-------|:-----------:|:---------------:|:------------------:|:----------------:|:---------------------:|-------|
| YYYY-MM-DD | e.g. Day 5 — contradiction UI | `frontend/components/...` | Yes / No | Yes / No | count | Low / Med / High | 1–10 | Yes / No |

**Observations:**  
- What worked:
- What failed:
- What to change next:

---

## Current Baseline Team

### Team: `DT-001 — Human Gate Core Six`

**Purpose:**  
Default Cursor agent council for daily dashboard development on `monica/dashboard-human-gate` — balanced judgment on contracts, UX edge cases, and sprint alignment without scope creep into pipeline or eval codebases.

**Hypothesis:**  
Routing sprint work through ACR-mapped roles yields reviewable MRs that preserve mock-mode local dev, provenance on every row, and API-first boundaries before Days 6–7 wire-up.

**When To Use:**  
- Default for feature work in `frontend-react/` and contract layer in `frontend/`
- Doc updates under `docs/monica/`
- Pre-MR self-review (peer review still required per team rules)
- First-pass checks before pairing with Matthew (API) or Dominic (eval embed)

#### Agent Roles

| Role | Agent Name | Model | Function | Trust Level |
|------|------------|-------|----------|-------------|
| Orchestrator | GateCoordinator | Composer 2.5 Fast or Gemini 3 Flash | Maps tasks to sprint days; enforces pillar scope; blocks `knowledge/`/`knowledge/evals/` edits; tracks mock + API dual-path | Medium |
| Architect | BoundarySmith | Claude 4.6 Sonnet or GPT-5.4 | `DataProvider` protocol, module map, non-blocking integration checklist ([§17](../ARCHITECTURE_MONICA.md#17-integration-architecture--data-contracts)) | Medium-high |
| Adversarial UX | EdgeCaseForge | Gemini 3 Flash | Empty states, API-down banner, 409 promote conflict, reject audit gap, unknown `state` values, contradiction stub without mutations | Low |
| Verification | ContractGuard | Claude 4.6 Sonnet or GPT-5.4 | `Candidate` aliases (`provenance`/`source_log`, `confidenceBreakdown`); `X-Praxis-Contract: 1`; lifecycle `proposed → suggested → active` | Medium |
| Documentation | PillarScribe | GPT-5.4 or Claude 4.6 Sonnet | Syncs [monica-wireframes.md](../monica-wireframes.md) with as-built UI; conventional commits `feat(dashboard):` + `#<issue>` | Medium |
| Security & Performance | TrustSentinel | Deterministic checklist + light model | Env secrets only; mock-mode banner when `PRAXIS_API_BASE_URL` unset; pagination trigger ~200 rows; no LLM calls from UI | High |

#### Pillar Deliverable Map (scorecard anchors)

| Day | Monica deliverable (project plan) | Primary agents | Done when |
|-----|-----------------------------------|----------------|-----------|
| 1 | Wireframes, React stack decision | Orchestrator, Documentation | ✅ |
| 2 | Dashboard shell + candidate list | Architect, Verification | ✅ |
| 3 | Candidate detail + confidence UI | Verification, Documentation | ✅ |
| 4 | Human gate workflow polish | Adversarial UX, Orchestrator | ✅ |
| 5 | Contradiction resolution + credibility viz | Architect, Adversarial UX | ✅ |
| 6 | API integration + approval actions | BoundarySmith, ContractGuard | ✅ client; awaits Matthew server |
| 7 | Full approval flow + provenance in UI | ContractGuard, TrustSentinel | ✅ mock |
| 8 | Edge-case polish + eval embed | EdgeCaseForge, PillarScribe | ✅ |
| 9–10 | Demo-ready + user flow video | GateCoordinator, PillarScribe | 🔲 |

#### Run Notes + Scorecard

| Date | Sprint Day / Target | Scope | Contract OK | A11y Spot-check | Edge Cases Covered | Integration Risk | Demo Narrative (1–10) | Keep? |
|------|---------------------|-------|:-----------:|:---------------:|:------------------:|:----------------:|:---------------------:|-------|
| 2026-06-19 | Day 4–8 — workflow + API client | React UI + contract v1 | Yes | Partial | 8+ (confirm, reject reason, 409, defer) | Low (mock) | 8 | Yes |

**Observations:**  
- What worked: Modular layout; `ApiDataProvider` + `contract_v1.py`; mock + React parity; confirmation UX and contradiction resolve on mock.  
- What failed: Live E2E still blocked on Matthew's API server.  
- What to change next: Days 9–10 demo rehearsal + user-flow video; ContractGuard against Matthew's OpenAPI when published.

---

## Planned Variant Teams

### Team: `DT-002 — Integration Sprint Six` (Days 6–7)

**Purpose:** Wire `ApiDataProvider` to Matthew's REST endpoints without breaking mock mode.  
**Hypothesis:** Security & Performance + Verification roles front-loaded prevent session-state corruption and auth leaks.  
**Trigger:** Matthew publishes candidate list + promote/reject/resolve server (client already implemented).

| Role | Focus |
|------|-------|
| Orchestrator | Integration checklist only; pair with Matthew on schema drift |
| Architect | `api_client.py` implementation behind existing `DataProvider` |
| Adversarial UX | 409 conflicts, network timeout, partial list load |
| Verification | Live + mock parity tests; audit trail fields from backend |
| Documentation | Update wireframes + architecture §17 with final payloads |
| Security & Performance | `PRAXIS_API_TOKEN`, HTTPS, mutation auth expectations |

### Team: `DT-003 — Demo Act Two Six` (Days 8–10)

**Purpose:** Interview-ready demo — dashboard fills with scored candidates; one-click `suggested → active`; provenance visible; optional Dominic compounding curve embed.  
**Hypothesis:** Documentation + Adversarial UX drive the live narrative more than new features.  
**Trigger:** End-to-end promote path works on staging API.

| Role | Focus |
|------|-------|
| Orchestrator | Demo script timing; Act 2 beat from [proposal-praxis.md](../../plans/proposal-praxis.md) |
| Architect | Minimal diff; no last-minute architecture churn |
| Adversarial UX | Render cold start, stale data, keyboard-only walkthrough |
| Verification | Staging run with real candidates; provenance links valid |
| Documentation | User-flow video shot list; presentation talking points |
| Security & Performance | Staging URL access; no secrets in demo recording |

---

## Team Comparison Board

| Team ID | Contract Stability (1–10) | UX / Edge Coverage (1–10) | Sprint Alignment (1–10) | Demo Readiness (1–10) | Overall (avg) | Verdict |
|---------|:-------------------------:|:---------------------------:|:-----------------------:|:---------------------:|:-------------:|---------|
| DT-001 | | | | | | Baseline — daily dev |
| DT-002 | | | | | | Use Days 6–7 |
| DT-003 | | | | | | Use Days 8–10 |

**Best Team Right Now:** `DT-001`  
**Reason:** Day 2 shell shipped; Days 3–5 UI work benefits from balanced council before integration team swap.

---

## ACR Quick Reference (from architecture)

| ACR agent | Dashboard concern | Maps to DT role |
|-----------|-------------------|-----------------|
| Architect Agent | Module boundaries, contract stability, non-blocking integration | Architect |
| Security Agent | Env secrets, no log exfiltration, API auth | Security & Performance |
| Audit Agent | Provenance on every row; action logging | Verification + Documentation |
| Verification Agent | Mock contract tests match API schema | Verification |
| Documentation Agent | Wireframes, architecture, README | Documentation |
| Adversarial Agent | Empty states, API down, duplicate promote, rerun races | Adversarial UX |
| Performance Agent | Large lists, React render performance | Security & Performance |

**Cross-pillar coordination (not agent-owned):**

| Teammate | Monica pairs on | Contract surface |
|----------|-----------------|------------------|
| Matthew Daw | Days 6–7 candidate API | `GET` candidates, `POST` promote/reject/resolve |
| Dominic Antonelli | Day 8 eval embed | Compounding curve JSON; promotion event hooks (server-side) |

---

*AI accelerates the UI. Humans approve the knowledge. This team keeps the dashboard contract-first, pillar-sovereign, and demo-measurable.*
