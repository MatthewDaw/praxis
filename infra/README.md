# PRAXIS AWS CDK infrastructure

AWS CDK app provisioning PRAXIS cloud resources. Deploys to the account from ambient CDK CLI credentials in **`us-east-1`** by default.

## Stacks

| Stack | Purpose | Key outputs |
|-------|---------|-------------|
| `PraxisSessionsTableStack` | DynamoDB table for raw Claude Code session logs | Table name (`praxis-sessions` by default) |
| `PraxisAuthUserPoolStack` | Cognito User Pool + public SPA client for dashboard auth | `UserPoolId`, `UserPoolClientId`, `UserPoolRegion`, `IssuerUrl` |
| `PraxisKnowledgeGraphDbStack` | RDS PostgreSQL 16 + pgvector knowledge-graph store | `DbEndpoint`, `DbSecretArn`, secret `praxis/knowledge-graph/db` |
| `PraxisBackendServiceStack` | AWS App Runner service running the FastAPI backend (built from the repo-root `Dockerfile`); reaches the public RDS + Secrets Manager + Cognito over the internet | `ServiceUrl` |
| `PraxisFrontendSiteStack` | Private S3 bucket + CloudFront (OAC) serving the React SPA; 403/404 rewritten to `index.html` for client-side routing | `DistributionUrl` |

**Two-store model:** DynamoDB holds raw JSONL transcripts (session-capture). RDS holds distilled knowledge — dashboard candidates and KG facts/embeddings.

## Quick start

```powershell
cd infra
npm install
npm run build
npm run deploy                    # deploy all stacks
# Or deploy individually:
npx cdk deploy PraxisSessionsTableStack
npx cdk deploy PraxisKnowledgeGraphDbStack -c allowedCidr=YOUR.IP.ADDR/32
```

Session capture wrapper setup: [session-capture/README.md](../session-capture/README.md).

Knowledge-graph RDS setup (AWS CLI, Secrets Manager, schema bootstrap, Postgres-backed candidate API): [docs/monica/RDS_KG_DEPLOY.md](../docs/monica/RDS_KG_DEPLOY.md).

## Deploy the website

`PraxisBackendServiceStack` (App Runner) and `PraxisFrontendSiteStack` (S3 + CloudFront) host the live
PRAXIS site on AWS. Because the React bundle must be built with the backend URL baked in (Vite inlines
`VITE_*` env vars at build time), the deploy is ordered: **backend → build frontend → frontend**. The
orchestrator handles that ordering for you:

```powershell
cd infra
npm run deploy:web
```

This runs [`scripts/deploy-web.mjs`](scripts/deploy-web.mjs), which:

1. Deploys `PraxisBackendServiceStack` (`--outputs-file cdk-web-out.json`) and reads back its `ServiceUrl`.
2. In `frontend-react/`, runs `npm ci` then `npm run build` with `VITE_PRAXIS_API_BASE_URL` /
   `VITE_PRAXIS_POSTGRES_API_BASE_URL` set to that `ServiceUrl`, plus the known Cognito values
   (`VITE_COGNITO_USER_POOL_ID` = `us-east-1_4nAMe6bPK`, `VITE_COGNITO_CLIENT_ID` = `3ij653bq912pi4f17l5hn9iqqn`,
   `VITE_COGNITO_REGION` = `us-east-1`).
3. Deploys `PraxisFrontendSiteStack`, uploading the fresh `frontend-react/dist/` and invalidating CloudFront.
4. Prints the `DistributionUrl` (the live site) at the end.

**Docker is required** on the machine running this: `PraxisBackendServiceStack` builds the backend container
image (repo-root `Dockerfile`) as a CDK `DockerImageAsset`, which needs a running Docker daemon. The backend
CORS regex already allows `*.cloudfront.net`, so there is no circular dependency on the frontend URL.

The mock fixtures in `frontend-react/public/*.json` are committed, so the Python export scripts are optional
for the build. The two stacks can also be deployed individually with `npx cdk deploy <StackId>`, but use
`npm run deploy:web` to keep the build ordering correct.

> **Legacy:** the Render config (`frontend-react/render.yaml`, `knowledge/serve/render.yaml`) is kept in the
> repo for reference only. AWS (these two stacks) is now the system of record for hosting.

## Scripts

| Command | Action |
|---------|--------|
| `npm run build` | Compile TypeScript |
| `npm run synth` | Synthesize CloudFormation templates |
| `npm run deploy` | Deploy all stacks (`--require-approval never`) |
| `npm run destroy` | Tear down stacks |

## Context flags

Pass to `cdk deploy` with `-c key=value`:

| Flag | Stack | Default | Purpose |
|------|-------|---------|---------|
| `tableName` | Sessions | `praxis-sessions` | DynamoDB table name |
| `authUserPoolName` | Auth | `praxis-users` | Cognito User Pool name |
| `databaseName` | Knowledge graph | `praxis_kg` | Postgres database name |
| `allowedCidr` | Knowledge graph | `0.0.0.0/0` | CIDR allowed to reach RDS port 5432 — **lock to your IP** for capstone |

## Source layout

```
infra/
  bin/app.ts                      CDK app entry — all stacks
  lib/sessions-table-stack.ts     DynamoDB session log store
  lib/knowledge-graph-db-stack.ts RDS Postgres 16 + Secrets Manager
  lib/backend-service-stack.ts    App Runner backend service
  lib/frontend-site-stack.ts      S3 + CloudFront static site
  scripts/deploy-web.mjs          Ordered backend→build→frontend deploy
```
