# Days 9–10 — Remaining human deliverables

Code polish for this phase is in place (reject reason, refresh, low-confidence promote warning, contract tests, React parity, expanded pytest). Complete these manually before presentation:

## Demo rehearsal

- [x] Automated gate: `uv run pytest frontend/tests/ knowledge/evals/tests/test_cases.py -q` + `npm run build` in `frontend-react/` (2026-06-19)
- [ ] Run Act 2 from [DEMO_SCRIPT.md](DEMO_SCRIPT.md) on mock Streamlit (timed, ≤3.5 min)
- [ ] Run Act 2 on mock React (`cd frontend-react && npm run dev`)
- [ ] Run Act 2 again with `PRAXIS_API_BASE_URL` / `VITE_PRAXIS_API_BASE_URL` when Matthew's API is live — see [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md)
- [ ] Rehearse cold-start mention if using Render free tier ([RENDER_DEPLOY.md](RENDER_DEPLOY.md))

## User-flow video

- [ ] Capture screen recording: filter → detail → promote → contradiction resolve → eval embed
- [ ] Keep under 3 minutes for interview portfolio

## Accessibility pass

Code improvements shipped 2026-06-19 (React): `aria-label` on promote/reject/inspect/defer, `role="alert"` + `aria-live="assertive"` on low-confidence promote warning, keyboard Enter/Space on table rows.

Manual verification still required:

- [ ] Tab through global selection, promote/reject confirmations, and contradiction buttons (Streamlit + React)
- [ ] Screen reader: verify button `help` / `aria-label` text reads for promote/reject/inspect/defer
- [ ] Confirm low-confidence promote warning is announced when triggered

## Optional polish

- [ ] State-distribution chart (stretch)
- [ ] GitLab CI job for `frontend/tests/` when repo CI is live
