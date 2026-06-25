# Days 9–10 — Remaining human deliverables

Code polish for this phase is in place (reject reason, refresh, low-confidence promote warning, contract tests, React dashboard, expanded pytest, Vitest, mock export script). Complete these manually before presentation:

Execution plan: [MONICA_COMPLETION_PATH.md](MONICA_COMPLETION_PATH.md)

## Automated gate (run before each rehearsal)

```powershell
uv run pytest frontend/tests/ knowledge/evals/tests/test_cases.py -q
cd frontend-react
npm test
npm run lint
npm run build
```

- [x] Automated gate green (2026-06-19) — pytest + Vitest + build
- [x] Mock export script: `python scripts/export-mock-candidates.py` (18 candidates incl. `cand_18`)
- [x] React UX: full card text, created date, rejected-state messaging, promote confirm copy

## Demo rehearsal (React)

**Primary:** `cd frontend-react && npm run dev` → http://localhost:5173

- [ ] Run Act 2 from [DEMO_SCRIPT.md](DEMO_SCRIPT.md) on React with mock fixtures or disposable local API data (timed, ≤2 min)
- [ ] Run Act 2 again with `VITE_PRAXIS_API_BASE_URL` when Matthew's API is live — see [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md)
- [ ] Rehearse API cold-start mention if using Render free tier for `praxis-candidate-api` ([RENDER_DEPLOY.md](RENDER_DEPLOY.md)); React static site has no cold start

### Act 2 quick checklist (React)

1. Filter **proposed** → inspect **cand_1** provenance
2. Promote **cand_1** → confirm `proposed → active` dialog
3. Resolve **cand_9** ↔ **cand_16** → keep primary
4. Expand eval metrics embed
5. Optional: show **cand_18** (pathlib eval alignment)

## User-flow video

- [ ] Capture screen recording from **React** client: filter → detail → promote → contradiction resolve → eval embed
- [ ] Keep under 3 minutes for interview portfolio

## Accessibility pass

Code improvements shipped 2026-06-19 and later (React): `aria-label` on promote/reject/inspect/contradiction controls, `role="alert"` + `aria-live="assertive"` on low-confidence promote warning, keyboard Enter/Space on table rows, rejected-state helper text.

Manual verification still required:

- [ ] Tab through global selection, promote/reject confirmations, and contradiction buttons
- [ ] Screen reader: verify button `aria-label` text reads for promote/reject/inspect/contradiction controls
- [ ] Confirm low-confidence promote warning is announced when triggered
- [ ] Confirm rejected-state helper text is readable when a rejected candidate is selected

## Optional polish

- [ ] State-distribution chart (stretch)
- [ ] GitHub CI job for `frontend/tests/` + `frontend-react` `npm test` when repo CI is live
