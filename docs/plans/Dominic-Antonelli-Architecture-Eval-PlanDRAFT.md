# Dominic Antonelli — Architecture, Eval & Integration Individual Plan

> **Individual draft.** Team operating calendar, gap checklist, and canonical paths: [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). Eval harness: `knowledge/evals/`; session capture: `session-capture/`.

**Role:** Architecture, Eval & Integration Lead  
**Focus:** System design, eval harness (fixed tasks + metrics), GitHub hook/PR automation, Python tooling, deployment, live demo & compounding curve proof.  
**Interview Claim:** "I architected the eval harness and integration layer that rigorously proves PRAXIS delivers ≥50% fewer corrections with compounding gains."

**Sprint Schedule Note:** 10 full work days (skipping Thursday June 18). Day 1 = Wednesday June 16. **Internal** Days 9–10 (June 26–27): freeze and practice. **Public** Gauntlet showcase: **Monday, June 29**. See [PLAN_ALIGNMENT_GAP_CHECKLIST.md](monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). All times EOD unless noted.

## Overall Success Criteria (Shared)
- MVP delivered with full pipeline.
- Primary metric: ≥50% reduction in user corrections on benchmark vs. cold runs, plus visible compounding curve.
- Dominic owns measurement credibility and demo readiness.

## High-Level Phases & Personal Deliverables

### Days 1–2: Project Plan, Foundation & Design (Due: End of Day 2 — Saturday, June 20)
- **Day 1 Deliverables:**
  - Eval harness skeleton + fixed quirky repo tasks defined.
  - GitHub hook design.
- **Day 2 Deliverables:**
  - Define eval metrics & cold-run baseline script.
- **Key Output:** Eval framework structure in place; baseline measurement capability; architecture decisions locked with team.

### Days 3–5: Parallel Core Build (Due: End of Day 5 — Tuesday, June 23)
- **Day 3 Deliverables:**
  - Python tooling (reader, distiller wrapper) + basic injection.
- **Day 4 Deliverables:**
  - GitHub PR/ticket automation for promoted knowledge.
- **Day 5 Deliverables:**
  - Eval harness expansion: token/time tracking + basic dashboard integration points.
- **Key Output:** Tooling and automation foundations; eval harness capable of running cold baselines and capturing key metrics.

### Days 6–7: Integration & Human Gate (Due: End of Day 7 — Thursday, June 25)
- **Day 6 Deliverables:**
  - Full eval harness + cold vs injected comparison runner.
- **Day 7 Deliverables:**
  - GitHub hook live + PR creation on promotion.
  - Demo data prep.
- **Key Output:** Integrated system with automated promotion-to-PR flow; harness ready for before/after measurement.

### Day 8: Eval Harness & Measurement (Due: End of Day 8 — Friday, June 26)
- Full compounding curve measurement.
- Identify demo quirks.
- **Key Output:** Quantified proof of improvement (≥50% correction reduction + compounding curve data); demo scenarios validated.

### Day 9–10: Demo Polish, Documentation & Final Runs (Due: End of Day 10 — Sunday, June 28)
- Live demo script + side-by-side before/after.
- Final docs & repo handoff.
- Practice Presentation (overall demo leadership + Eval pillar).
- **Key Output:** Compelling, repeatable live demo with irrefutable measurement evidence; polished repo and documentation.

## Daily Commitments
- Daily 15-min sync at start of each day.
- All code reviewed by at least one other member.
- Own measurement credibility and demo readiness end-to-end.

## Cross-Team Notes
- Pair with Matthew and Monica on integration days (6–7) for full pipeline and dashboard wiring.
- Ensure eval harness consumes real outputs from ML pipeline and dashboard actions.
- Own the "proof" narrative: every demo should clearly show cold vs. injected results and the compounding trend.

*Stay focused: Your pillar turns claims into credible, measured reality. Prioritize the harness early so every integration can be validated immediately.*