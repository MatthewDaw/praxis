# PRAXIS Dashboard (Monica Peters)

React + Vite + TypeScript + Tailwind + Zustand frontend for the Human Gate review UI.

## Setup & Run (Day 1-2 deliverable)

```bash
cd frontend
npm install   # already includes react, zustand, tailwind
npm run dev
```

Open http://localhost:5173 — interactive candidate list with mock data, filters, promote/reject, confidence scores, provenance display.

## Key Files & Contracts

- `src/types/candidate.ts` — Shared data contracts (Candidate, GateState, Provenance, ConfidenceScore). Import this for pipeline integration.
- `src/store/candidateStore.ts` — State + actions for gate workflow.
- `src/components/CandidateList.tsx` + `ConfidenceScore.tsx` — Core UI per dashboard rules (states, tooltips, audit links).
- `src/App.tsx` — Shell ready for detail views / API wiring (Days 6-7).

## Team Integration Notes

- All components have JSDoc @usage / @example / @author Monica Peters <monigarr@MoniGarr.com> / @created 2026-06-17
- Follows .cursor/rules (exhaustive never checks, no inline imports, provenance preserved)
- Mock data uses realistic JSONL provenance for Matthew/Dominic to test against.
- Next: Day 3 detail view; pair on API (Matthew pipeline output).

See root docs/Monica-Peters-Dashboard-Plan.md and .cursor/rules/praxis-dashboard.mdc for full context.

## License / Status

Internal capstone — Day 2 shell complete. Ready for review.
