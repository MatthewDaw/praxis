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

`PraxisBackendServiceStack` (App Runner) and `PraxisFrontendSiteStack` (S3 + CloudFront) host the live PRAXIS
site on AWS. **Deploys run in CI** — [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) on every
push to `main`, in two change-gated jobs (backend first, then frontend).

The deploy honors one ordering constraint: the React bundle inlines its `VITE_*` config at **build time**, so
the pipeline:

1. Deploys `PraxisBackendServiceStack` and reads its `ServiceUrl` from the stack outputs.
2. Builds `frontend-react/` (`npm ci && npm run build`) with `VITE_PRAXIS_API_BASE_URL` /
   `VITE_PRAXIS_POSTGRES_API_BASE_URL` set to that `ServiceUrl`, plus the Cognito values
   (`VITE_COGNITO_USER_POOL_ID` / `_CLIENT_ID` / `_REGION` — the constants in [`lib/config.ts`](lib/config.ts)).
3. Deploys `PraxisFrontendSiteStack`, which uploads the freshly-built `frontend-react/dist/` and invalidates
   CloudFront. (`FrontendSiteStack` only uploads `dist/` if it exists at synth time, so the build must come first.)

**Docker is required** on the runner — `PraxisBackendServiceStack` builds the backend image (repo-root
`Dockerfile`) as a CDK `DockerImageAsset`. GitHub's `ubuntu-latest` runners ship with Docker, the AWS CLI, and
Node, so it works out of the box.

For an **infra-only** change you can deploy a single stack by hand (`npx cdk deploy <StackId>`), but a manual
frontend deploy must first rebuild `dist/` with the `VITE_*` vars above, or the SPA won't reach the backend.

> **Legacy:** the Render config (`frontend-react/render.yaml`, `knowledge/serve/render.yaml`) is kept in the
> repo for reference only. AWS (these two stacks) is now the system of record for hosting.

## Custom domains (DNS)

The domain **`praxiskg.com`** is registered at **Cloudflare**. Cloudflare Registrar won't let the apex
nameservers move to Route 53, so the apex stays on Cloudflare DNS and individual **subdomains are delegated**
to a Route 53 public hosted zone (`PraxisDnsStack`, zone id `Z05005163AR5WTJFHKYZC`). Route 53 is then
authoritative for everything under a delegated subdomain, so CDK/ACM can manage records + cert validation there.

### One-time Cloudflare delegation

For each subdomain, add **four `NS` records** in the Cloudflare DNS panel (record name = the subdomain label,
e.g. `app`), all pointing at the zone's nameservers:

```
ns-665.awsdns-19.net
ns-1601.awsdns-08.co.uk
ns-1305.awsdns-35.org
ns-186.awsdns-23.com
```

(Re-fetch anytime: `aws route53 get-hosted-zone --id Z05005163AR5WTJFHKYZC --query DelegationSet.NameServers`.)

| Subdomain | Points at | Managed by |
|-----------|-----------|------------|
| `phoenix.praxiskg.com` | Phoenix EC2 (Elastic IP) | CDK `PhoenixStack` — A record |
| `app.praxiskg.com` | Frontend CloudFront | CDK `FrontendSiteStack` — ACM cert + alias |
| `mcp.praxiskg.com` | Backend App Runner | `npm run domain:backend` (see below) |

**Delegate the subdomain before deploying** — ACM and App Runner write a cert-validation record *under* that
subdomain, and if it isn't delegated to Route 53 yet the validation can't resolve and the deploy hangs.

### Frontend — `app.praxiskg.com`

Fully CDK-managed: `FrontendSiteStack` requests a DNS-validated ACM cert against the Route 53 zone, attaches
the domain to the CloudFront distribution, and creates the A/AAAA alias records. It deploys via the
`deploy-frontend` job in CI. Override the hostname with `-c frontendDomain=...`.

### Backend — `mcp.praxiskg.com`

App Runner custom domains have **no CloudFormation/CDK support**, and the required records (cert-validation
CNAMEs + the DNS target) only exist once the domain is associated at runtime. So this is a one-shot script,
run **after** the backend is deployed and `mcp` is delegated:

```powershell
cd infra
npm run domain:backend
```

[`scripts/associate-backend-domain.mjs`](scripts/associate-backend-domain.mjs) associates the domain, waits for
App Runner to emit its validation records, then UPSERTs the target CNAME + validation CNAMEs into the Route 53
zone. It's idempotent. Override domain / zone with the `MCP_DOMAIN` / `HOSTED_ZONE_ID` env vars.

> Once `mcp.praxiskg.com` is active, point the frontend at it: in the `deploy-frontend` job of
> [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml), set `VITE_PRAXIS_API_BASE_URL` to
> `https://mcp.praxiskg.com` instead of the resolved `*.awsapprunner.com` `ServiceUrl`, and confirm the backend
> CORS allows the `app.praxiskg.com` origin (the regex covers `*.cloudfront.net`, which does **not** include the
> custom domain).

## Scripts

| Command | Action |
|---------|--------|
| `npm run build` | Compile TypeScript |
| `npm run synth` | Synthesize CloudFormation templates |
| `npm run deploy` | Deploy all stacks (`--require-approval never`) |
| `npm run domain:backend` | Attach `mcp.praxiskg.com` to App Runner + create its Route 53 records (see [Custom domains (DNS)](#custom-domains-dns)) |
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
  lib/frontend-site-stack.ts      S3 + CloudFront static site (app.praxiskg.com)
  lib/dns-stack.ts                Route 53 public hosted zone for praxiskg.com
  scripts/associate-backend-domain.mjs  App Runner mcp.praxiskg.com + Route 53 records
```
