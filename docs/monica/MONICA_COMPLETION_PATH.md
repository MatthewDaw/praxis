# Monica Completion Path

**Owner:** Monica Peters  
**Date:** 2026-06-22  
**Scope:** Dashboard and Human Gate pillar completion evidence only.  
**Primary sources:** [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md), [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md), [DEMO_SCRIPT.md](DEMO_SCRIPT.md), [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md), [PLAN_ALIGNMENT_GAP_CHECKLIST.md](PLAN_ALIGNMENT_GAP_CHECKLIST.md)

## Goal

Complete Monica-owned evidence for the human approval dashboard without blocking or breaking Matthew's pipeline/API work or Dominic's eval/metrics work.

Completion means:

- Offline automated gate is green and recorded.
- Act 2 React demo is timed at 2 minutes or less so Matthew and Dominic have room for their pillar demos.
- Accessibility evidence is captured manually.
- Screenshots and a short user-flow video are captured.
- Live API smoke is run only when an API is already available and safe to mutate.
- Row-level candidate refresh works after item updates without forcing the full data-source reload path.
- Any missing backend/eval dependency is documented as a cross-pillar blocker, not converted into Monica-owned work.

## Ownership Boundary

### Monica owns

- React dashboard demo readiness in `frontend-react/`.
- Python frontend contract/mock validation in `frontend/tests/`.
- Human-gate UX evidence: provenance, confidence, promote flow, contradiction resolution, reject behavior, and eval/evidence panel display.
- Act 2 script timing and portfolio recording.
- Accessibility verification for existing dashboard controls.
- Self-serve smoke against an available candidate API.

### Monica does not own

- PostgreSQL, RDS, `PRAXIS_DB_URL`, or Secrets Manager setup.
- Candidate API persistence internals in `knowledge/serve/`.
- Promote-to-knowledge-graph write behavior.
- JSONL ingestion, distillation, dedup, clustering, or confidence scoring pipeline internals.
- Dominic's cold-vs-injected paired eval runner or real metrics endpoint.
- GitHub CI setup unless the team explicitly assigns it.

## Non-Blocking Rules

1. Do not edit `knowledge/serve/`, `knowledge/evals/`, `session-capture/`, or `infra/` for this completion path unless a teammate explicitly asks for a Monica-scoped integration change.
2. Do not modify root `.env`; use `frontend-react/.env.local` for dashboard-only live API settings.
3. Do not add live API tests to CI or the offline gate.
4. Do not mutate a shared live API or database without Matthew's confirmation that the target is disposable or demo-safe.
5. If the API is unavailable, continue with mock React evidence and record live smoke as blocked by API availability.
6. If the metrics endpoint is unavailable, use the fixture/placeholder eval embed and record real metrics as blocked by Dominic's endpoint.
7. Keep all evidence under `docs/monica/` so other pillars do not need to change their workflows.

## Deliverables

| Deliverable | Output | Done when |
|---|---|---|
| Green gate evidence | Rehearsal log entry with command results | Pytest, Vitest, lint, and build pass |
| Timed demo | Rehearsal log entry | Act 2 completes in 2 minutes or less |
| Accessibility evidence | Checklist notes | Keyboard, labels, alert, and rejected-state helper text verified |
| Screenshots | Files or links under `docs/monica/screenshots/` / `REHEARSAL_LOG.md` | Required screenshots captured or explicitly marked pending |
| User-flow video | File or link recorded in rehearsal log | Video is under 3 minutes |
| Row-level refresh | Rehearsal log entry | One changed candidate refreshes without **Load data** / **Refresh data** |
| Conditional live smoke | Rehearsal log entry | Run only when API URL exists and is safe |
| Blocker notes | Standup/rehearsal note | Cross-pillar blockers are dated and owner-tagged |

## Evidence Locations

Recommended local evidence layout:

```text
docs/monica/
  REHEARSAL_LOG.md
  screenshots/
    2026-06-22-data-source-state.png
    2026-06-22-candidate-provenance.png
    2026-06-22-confidence-breakdown.png
    2026-06-22-promote-confirmation.png
    2026-06-22-contradiction-before-after.png
    2026-06-22-eval-metrics-expanded.png
  videos/
    2026-06-22-human-gate-flow.mp4
```

Do not commit large video files unless the team wants portfolio media in the repo. If the video is stored outside the repo, record the filename, location, duration, and capture date in `REHEARSAL_LOG.md`.

## Phase 1 - Green Gate

Run from the repository root:

```powershell
uv run pytest frontend/tests/ knowledge/evals/tests/test_cases.py -q
```

Then:

```powershell
cd frontend-react
npm test
npm run lint
npm run build
```

Record in `REHEARSAL_LOG.md`:

- Date and time.
- Command list.
- Pass/fail result.
- Any skipped tests.
- Any failure owner: Monica for dashboard/contract failures, Matthew for API dependency failures, Dominic for eval metrics dependency failures.

Pass criteria:

- `frontend/tests/` passes.
- `knowledge/evals/tests/test_cases.py` passes.
- `frontend-react` tests pass.
- `frontend-react` lint passes.
- `frontend-react` production build passes.

If the gate fails:

1. Fix Monica-owned frontend/contract issues.
2. Do not edit teammate-owned pipeline or eval internals to force the gate green.
3. If a failure is from a cross-pillar dependency, log it with owner and exact command output.

## Phase 2 - Timed Demo

Start the React client:

```powershell
cd frontend-react
npm run dev
```

Before timing:

- Confirm the selected data source is intentional. Current React config defaults to Local Postgres at `http://localhost:8000`; switch to mock fixtures only if that path is exposed in the current build.
- If using live/local API mode, confirm the target store is disposable or demo-safe.
- Use [DEMO_SCRIPT.md](DEMO_SCRIPT.md) as the speaking script.

Timed path:

1. Problem framing: show candidate list and provenance.
2. Credibility review: select `cand_1`, open detail, show confidence and audit trail.
3. Contradiction resolution: select `cand_9`, compare with `cand_16`, keep primary.
4. Human gate promotion: filter proposed, promote a candidate through confirmation.
5. Measurement hook: expand eval metrics embed.

Pass criteria:

- Full Act 2 completes in 2 minutes or less.
- Promotion confirmation copy is visible.
- Contradiction resolution is visible.
- Eval metrics embed is visible.
- No shared live API dependency is required for this pass.

Record in `REHEARSAL_LOG.md`:

- Duration.
- Data source used: mock fixtures, local live API, or remote live API.
- Any UI hesitation or script edit needed.
- Whether the closing line was delivered cleanly.

## Phase 3 - Accessibility Evidence

Manual verification checklist:

- Tab through global candidate selection.
- Tab through promote and reject confirmation controls.
- Tab through contradiction buttons.
- Verify Enter/Space works on table rows.
- Verify screen reader text for promote, reject, inspect, and defer controls.
- Trigger low-confidence promote warning and verify alert announcement.
- Select a rejected candidate and verify rejected-state helper text is readable.

Record evidence in `REHEARSAL_LOG.md`:

| Check | Result | Evidence |
|---|---|---|
| Keyboard tab order | Pending | Notes or screenshot |
| Table row Enter/Space | Pending | Notes |
| Button accessible names | Pending | Screen reader notes |
| Low-confidence alert | Pending | Notes or screenshot |
| Rejected-state helper text | Pending | Notes or screenshot |

If an accessibility issue is found:

- Fix it only in Monica-owned React files.
- Re-run `npm test`, `npm run lint`, and `npm run build`.
- Do not alter API contracts or eval fixtures unless the issue proves the contract is wrong and the team agrees.

## Phase 4 - Screenshots

Capture these from the React client using mock fixtures or disposable local API data:

- Data-source banner or control state.
- Candidate list with provenance column.
- Candidate detail confidence breakdown.
- Promote confirmation dialog showing state transition copy.
- Contradiction resolution before and after.
- Eval metrics embed expanded.

Recommended naming:

```text
docs/monica/screenshots/2026-06-22-01-data-source-state.png
docs/monica/screenshots/2026-06-22-02-candidate-provenance.png
docs/monica/screenshots/2026-06-22-03-confidence-breakdown.png
docs/monica/screenshots/2026-06-22-04-promote-confirmation.png
docs/monica/screenshots/2026-06-22-05-contradiction-resolution.png
docs/monica/screenshots/2026-06-22-06-eval-metrics-expanded.png
```

Record the screenshot set in `REHEARSAL_LOG.md`.

## Phase 5 - User-Flow Video

Capture one short recording from the React client:

1. Filter candidate list.
2. Open candidate detail.
3. Promote through confirmation.
4. Resolve contradiction.
5. Expand eval metrics.

Pass criteria:

- Under 3 minutes.
- Uses the React client.
- Shows provenance, confidence, promotion, contradiction resolution, and metrics.
- Does not require live API.

Record in `REHEARSAL_LOG.md`:

- File name or storage location.
- Duration.
- Capture mode: mock or live.
- Any known defects visible in the recording.

## Phase 6 - Conditional Live API Smoke

Only run this phase if Matthew's candidate API exists and the target is safe to mutate.

Use `frontend-react/.env.local`, not root `.env`:

```env
VITE_PRAXIS_API_BASE_URL=http://localhost:8000
VITE_PRAXIS_API_TOKEN=
VITE_PRAXIS_CONTRACT_VERSION=1
VITE_PRAXIS_EVAL_METRICS_URL=
VITE_PRAXIS_AUTH_DISABLED=1
```

Dashboard check:

```powershell
cd frontend-react
npm test
npm run dev
```

Live smoke path:

1. Confirm live mode banner shows the API URL.
2. Confirm `GET /candidates` loads candidate rows.
3. Promote a disposable proposed candidate.
4. Refresh only that candidate and confirm the mutation persists.
5. Try the duplicate promote/conflict path only on a disposable row.
6. Reject a disposable candidate with a reason.
7. Verify low-confidence promote warning.
8. Verify card view behavior matches table behavior.
9. Verify defer contradiction shows the non-mutating info banner.

Optional Python live smoke, only against a disposable local server:

```powershell
$env:PRAXIS_API_BASE_URL = "http://127.0.0.1:8000"
$env:PYTHONPATH = "frontend"
uv run pytest frontend/tests/test_live_api_smoke.py -v
```

Pass criteria:

- Live list loads.
- Mutations persist after refresh.
- Item refresh updates one candidate without reloading the full list.
- Conflict/error UX is recoverable.
- Reject maps to `rejected` behavior.
- Eval/evidence panels load from the current API endpoints or show a clear unavailable state.

If the API does not exist:

- Do not start building backend functionality.
- Mark live smoke as blocked in `REHEARSAL_LOG.md`.
- Continue local/mock demo, screenshots, accessibility, and video.

If the metrics endpoint does not exist:

- Keep the placeholder or fixture-backed eval embed.
- Mark real metrics as blocked by metrics endpoint availability.

## Phase 7 - Standup and Freeze Reporting

Use this status language in standup:

```text
Monica dashboard path:
- Offline gate: pass/fail with command evidence.
- Demo Act 2: duration and remaining script polish.
- A11y: verified items and any Monica-owned fixes.
- Screenshots/video: captured or scheduled.
- Live smoke: pass/fail/blocked by API availability.
- Cross-pillar blockers: Matthew API/persistence or Dominic metrics only.
```

Before feature freeze:

- Re-run the green gate after any Monica-owned frontend change.
- Confirm the demo source path still works without mutating shared data.
- Confirm live mode is isolated to `frontend-react/.env.local`.
- Confirm no cross-pillar code was changed for Monica evidence.

Before hard freeze:

- Lock Act 2 script.
- Keep one known-good mock recording.
- Keep one screenshot set.
- Record the live smoke result or blocker.
- Do not take speculative dependency work into the demo branch.

## Final Completion Checklist

- [ ] Green gate recorded in `REHEARSAL_LOG.md`.
- [ ] Demo Act 2 timed at 2 minutes or less.
- [ ] Accessibility checklist completed.
- [ ] Required screenshots captured or linked.
- [ ] User-flow video captured or linked.
- [ ] Row-level candidate refresh verified after an item change.
- [ ] Live API smoke run, or blocked with owner/date.
- [ ] Metrics smoke run, or blocked with owner/date.
- [ ] No root `.env` changes.
- [ ] No Matthew-owned API/database changes.
- [ ] No Dominic-owned eval/metrics changes.
- [ ] Monica-owned completion status posted to standup notes.
