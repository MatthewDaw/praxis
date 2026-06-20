# Monica Peters — Dashboard & Human Gate Individual Plan

Pillar documentation: [ARCHITECTURE_MONICA.md](ARCHITECTURE_MONICA.md) · [monica-wireframes.md](monica-wireframes.md) (as-built spec).

**Role:** Dashboard & Human Gate Lead · **Daily Scrum Master** (see [PLAN_ALIGNMENT_GAP_CHECKLIST.md](PLAN_ALIGNMENT_GAP_CHECKLIST.md))  
**Focus:** React human-gate dashboard (`frontend-react/`) with Python contract/mock layer (`frontend/`) — human approval workflow (proposed→suggested→active), contradiction resolution UI, credibility metrics viz, injection controls. Matthew validates the candidate API against typed Python fixtures and pytest without running the React dev server unless desired.  
**Interview Claim:** "I designed and built the human approval dashboard that enforces quality gates and makes knowledge promotion transparent and measurable."

**Sprint Schedule Note:** 10 full work days (skipping Thursday June 18). Day 1 = Wednesday June 16. **Internal** Days 9–10 (June 26–27): project completion, hard freeze, and presentation practice. **Public** Gauntlet showcase: **Monday, June 29**. Team operating calendar: [PLAN_ALIGNMENT_GAP_CHECKLIST.md](PLAN_ALIGNMENT_GAP_CHECKLIST.md). All times EOD unless noted.

**Completion status:** [ARCHITECTURE_MONICA.md §19](ARCHITECTURE_MONICA.md#19-sprint-alignment-monica-deliverables)

## Overall Success Criteria (Shared)
- MVP delivered with full pipeline.
- Primary metric: ≥50% reduction in user corrections on benchmark vs. cold runs.
- Monica owns UX clarity and human-gate usability.

## High-Level Phases & Personal Deliverables

### Days 1–2: Project Plan Drafts, Foundation & Design (Due: End of Day 2 — Friday, June 19)
- **Day 1 Deliverables:**
  - Repo setup, documentation drafts, and project plan drafts.
  - Dashboard wireframes & team tech stack decision (**React** in `frontend-react/`; Python contract layer in `frontend/`).
- **Day 2 Deliverables:**
  - Build review dashboard shell + candidate list view.
- **Key Output:** Approved wireframes; interactive shell ready for data population; tech stack finalized with team.

### Days 3–5: Parallel Core Build (Due: End of Day 5 — Monday, June 22)
- **Day 3 Deliverables:**
  - Candidate detail view + confidence score UI components.
- **Day 4 Deliverables:**
  - Human gate workflow UI (proposed → suggested → active states).
- **Day 5 Deliverables:**
  - Contradiction resolution interface + credibility metrics visualization.
- **Key Output:** Polished dashboard UI components for reviewing, scoring, and gating knowledge candidates.

### Days 6–7: Integration & Human Gate (Due: End of Day 7 — Wednesday, June 24)
- **Day 6 Deliverables:**
  - Dashboard ↔ backend API integration + approval actions wired (React UI + Python contract tests).
- **Day 7 Deliverables:**
  - Full human approval flow complete on React dashboard.
  - Provenance display in UI.
- **Key Output:** End-to-end human gate functional: users can review, resolve contradictions, promote knowledge with full audit trail visible. Matthew validates server against `frontend/tests/` and `frontend-react/` API client.

### Day 8: Eval Harness & Measurement (Due: End of Day 8 — Thursday, June 25)
- Dashboard polish + edge-case handling in review flow.
- Support eval metrics visualization if needed.
- **Key Output:** Robust, demo-ready dashboard handling real data and edge cases.

### Day 9–10: Demo Polish, Documentation & Final Runs (Due: End of Day 10 — Saturday, June 27)
- Dashboard demo-ready.
- User flow video capture.
- Practice Presentation contribution (Dashboard/Human Gate pillar).
- **Key Output:** Production-quality dashboard with compelling live demo story; clear UX for quality gates.

## Daily Commitments
- Daily 15-min sync at start of each day.
- All code reviewed by at least one other member.
- Own UX clarity and human-gate usability end-to-end.

## Cross-Team Notes
- Pair with Matthew on integration (Days 6–7) for seamless candidate data flow and approval actions.
- Matthew owns **PostgreSQL** setup for the candidate API and Knowledge Graph persistence (Days 6–7). Monica's UIs integrate via [candidate-api-v1.md](../integration/candidate-api-v1.md) only — no database coupling in `frontend/` or `frontend-react/`.
- Coordinate with Dominic on eval harness metrics display and demo data prep.
- Ensure every promoted item shows clear provenance and confidence rationale for transparency and interview storytelling.

*Stay focused: Your pillar makes the "human in the loop" real and credible. Polish the review experience so promotion feels effortless and trustworthy.*
