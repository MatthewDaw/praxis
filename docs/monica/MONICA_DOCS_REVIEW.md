# Monica Docs Alignment Review

Review date: 2026-06-25
Branch checked: `dev/monica-dashboard`
Remote checked: `origin https://github.com/Antonelli-Tech-Solutions/praxis.git`

## Update Status

### 1. Branch and Render remote references

The local checkout is on `dev/monica-dashboard`, with `origin` pointing to the GitHub repository `https://github.com/Antonelli-Tech-Solutions/praxis.git`.

Docs have been updated to use the current branch/repo. Remaining non-doc follow-up: the Render blueprint files outside `docs/monica/` still need their repo/branch entries updated before deploy:

- `frontend-react/render.yaml`
- `knowledge/serve/render.yaml`

Impact if skipped: Render deploys can still point at the legacy repo/branch even though the docs now call out the required correction.

### 2. Lifecycle vocabulary

Current code and tests use `proposed | active | rejected`:

- `frontend/models/candidate.py`
- `frontend-react/src/types/candidate.ts`
- `frontend-react/src/components/viz/legendConfig.ts`
- `frontend-react/src/api/mockGateWorkflow.test.ts`
- `knowledge/serve/tests/test_server.py`
- `knowledge/serve/tests/test_facts_candidates.py`

Docs have been updated to use `proposed -> active` and `rejected` in the operational instructions, architecture notes, wireframes, plan, runbooks, and templates:

- `docs/monica/ARCHITECTURE_MONICA.md`
- `docs/monica/INTEGRATION_SMOKE.md`
- `docs/monica/DAYS_9_10_REMAINING.md`
- `docs/monica/DEMO_SCRIPT.md`
- `docs/monica/Monica-Peters-Dashboard-Plan.md`
- `docs/monica/monica-wireframes.md`
- `docs/monica/REHEARSAL_LOG.md`
- `docs/monica/RDS_KG_DEPLOY.md`
- `docs/monica/PLAN_ALIGNMENT_GAP_CHECKLIST.md`

Impact: demo steps now match the current UI/API behavior.

### 3. Mock/live startup instructions

Current React config defaults to the Local Postgres live preset at `http://localhost:8000` when no deployed API URL is set:

- `frontend-react/src/config/dataSource.ts`
- `frontend-react/src/api/providerFactory.ts`
- `frontend-react/src/components/ui/DataSourceControl.tsx`

Docs now describe that behavior directly. The only surfaced presets are `Local Postgres` and `Remote Postgres`; mock still exists as a provider mode, but relying on mock-only public deploys requires an explicit mock source path.

Impact: reviewers should no longer expect unset API env alone to force mock mode.

### 4. Auth docs

The current server and React app support:

- Cognito JWT verification via `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_REGION`
- React Amplify config via `VITE_COGNITO_USER_POOL_ID`, `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_REGION`
- API-key auth through `X-Praxis-Key`
- org selection through `X-Praxis-Org`
- local dev bypass through `PRAXIS_AUTH_DISABLED=1` plus `VITE_PRAXIS_AUTH_DISABLED=1`

Docs now call out Cognito, API-key, org-header, and local-auth-bypass behavior for smoke and deploy paths.

Impact: local live smoke and Render setup now reflect the current protected API surface.

### 5. Screenshot evidence paths are declared but missing

`docs/monica/screenshots/README.md` now exists so the documented evidence path is real without committing binary screenshot artifacts.

Impact: screenshot artifacts remain to be captured or linked, but the repo no longer points at a missing directory.

### 6. API smoke endpoint names and scope

Current `knowledge/serve/app.py` exposes live routes beyond early candidate smoke, including:

- `/candidates`
- `/contradictions`
- `/graph`
- `/snapshots`
- `/org/sources`
- `/fold-in`
- `/apikeys`
- `/evals/*`
- `/insights`
- `/ingest`
- `/context`

Docs now describe current eval/evidence panels and `/ingest` rather than treating the API as unpublished.

Impact: integration smoke and demo docs better match what is implemented.

## Recommended Update Order

1. Update `frontend-react/render.yaml` and `knowledge/serve/render.yaml` repo/branch entries before deploy.
2. Decide whether the React app should restore an explicit mock preset or whether docs should continue treating `Local Postgres` as the default demo path.
3. Capture or link the screenshot/video evidence named by the completion path.

## Verification Performed

- Inspected `docs/monica/` Markdown files for branch, lifecycle, mock/live, auth, deploy, and evidence claims.
- Checked current git branch and remote.
- Checked `frontend-react/package.json` scripts.
- Checked `frontend-react/.node-version`.
- Checked current React data-source config, provider factory, auth config, and Render blueprint.
- Checked current Python candidate model and server routes.
- Checked current backend tests that assert rejected-state behavior.

No code or test execution was required for this documentation review.
