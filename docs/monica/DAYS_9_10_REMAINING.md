# Days 9–10 — Remaining human deliverables

Code polish for this phase is in place (reject reason, refresh, low-confidence promote warning, contract tests). Complete these manually before presentation:

## Demo rehearsal

- [ ] Run Act 2 from [DEMO_SCRIPT.md](DEMO_SCRIPT.md) on mock Streamlit
- [ ] Run Act 2 on mock React (`cd frontend-react && npm run dev`)
- [ ] Run Act 2 again with `PRAXIS_API_BASE_URL` / `VITE_PRAXIS_API_BASE_URL` when Matthew's API is live
- [ ] Rehearse cold-start mention if using Render free tier ([RENDER_DEPLOY.md](RENDER_DEPLOY.md))

## User-flow video

- [ ] Capture screen recording: filter → detail → promote → contradiction resolve → eval embed
- [ ] Keep under 3 minutes for interview portfolio

## Accessibility pass

- [ ] Tab through global selection, promote/reject confirmations, and contradiction buttons
- [ ] Screen reader: verify button `help` text reads for promote/reject/inspect/defer
- [ ] Confirm low-confidence promote warning is announced when triggered

## Optional polish

- [ ] State-distribution chart (stretch)
- [ ] GitLab CI job for `frontend/tests/` when repo CI is live
