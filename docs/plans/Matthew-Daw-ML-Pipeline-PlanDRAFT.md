# Matthew Daw — ML & Knowledge Pipeline Individual Plan

> **Individual draft.** Team operating calendar, gap checklist, and canonical paths: [docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md](monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). Code lives under `knowledge/`.

**Role:** ML & Knowledge Pipeline Lead  
**Focus:** Ingestion, learning moment detection (ML classifier), LLM distillation, consolidation/dedup/scoring, knowledge graph, provenance.  
**Interview Claim:** "I led the ML-powered distillation engine that turns raw JSONL logs into scored, deduplicated, human-approved knowledge with full provenance."

**Sprint Schedule Note:** 10 full work days (skipping Thursday June 18). Day 1 = Wednesday June 16. **Internal** Days 9–10 (June 26–27): freeze and practice. **Public** Gauntlet showcase: **Monday, June 29**. See [PLAN_ALIGNMENT_GAP_CHECKLIST.md](monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md). All times EOD unless noted.

## Overall Success Criteria (Shared)
- MVP delivered with full pipeline.
- Primary metric: ≥50% reduction in user corrections on benchmark vs. cold runs.
- Matthew owns data/ML correctness and knowledge quality.

## High-Level Phases & Personal Deliverables

### Days 1–2: Project Plan, Foundation & Design (Due: End of Day 2 — Saturday, June 20)
- **Day 1 Deliverables:**
  - Explore sample JSONL logs.
  - Define episode segmentation heuristics.
- **Day 2 Deliverables:**
  - Prototype learning moment detector (heuristics + LLM).
  - Participate in design review and data contracts definition.
- **Key Output:** Solid foundation for episode boundaries and initial detection logic; data contracts agreed with team.

### Days 3–5: Parallel Core Build (Due: End of Day 5 — Tuesday, June 23)
- **Day 3 Deliverables:**
  - Full distillation pipeline implemented.
  - Structured candidate output with provenance.
- **Day 4 Deliverables:**
  - Embeddings + HDBSCAN dedup/cluster logic.
  - Contradiction detection logic.
- **Day 5 Deliverables:**
  - Confidence scoring (frequency/recency/breadth) + decay rules.
- **Key Output:** Core ML pipeline producing scored, deduplicated candidates ready for human gate.

### Days 6–7: Integration & Human Gate (Due: End of Day 7 — Thursday, June 25)
- **Day 6 Deliverables:**
  - End-to-end pipeline wiring.
  - Knowledge graph stub.
- **Day 7 Deliverables:**
  - Knowledge store (graph + CLAUDE.md generator).
  - Injection logic complete.
- **Key Output:** Pipeline fully integrated with dashboard/backend; knowledge can be promoted and injected.

### Day 8: Eval Harness & Measurement (Due: End of Day 8 — Friday, June 26)
- Run batch evals.
- Analyze failure modes.
- Tune thresholds.
- **Key Output:** Validated knowledge quality; metrics feeding into compounding proof.

### Day 9–10: Demo Polish, Documentation & Final Runs (Due: End of Day 10 — Sunday, June 28)
- Final pipeline tuning.
- Knowledge quality report.
- Practice Presentation contribution (ML pillar).
- **Key Output:** Production-ready distillation engine; clear demo narrative for technical interviews.

## Daily Commitments
- Daily 15-min sync at start of each day.
- All code reviewed by at least one other member.
- Own data/ML correctness end-to-end.

## Cross-Team Notes
- Pair with Monica on dashboard integration (Days 6–7) for candidate display and approval actions.
- Support Dominic on eval harness data needs and failure mode analysis.
- Ensure provenance is captured at every step for dashboard display and auditability.

*Stay focused: Your pillar is the engine that makes everything else possible. Prioritize clean, testable distillation and consolidation logic first.*