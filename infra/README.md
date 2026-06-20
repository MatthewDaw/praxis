# PRAXIS AWS CDK infrastructure

AWS CDK app provisioning PRAXIS cloud resources. Deploys to the account from ambient CDK CLI credentials in **`us-east-1`** by default.

## Stacks

| Stack | Purpose | Key outputs |
|-------|---------|-------------|
| `PraxisSessionsTableStack` | DynamoDB table for raw Claude Code session logs | Table name (`praxis-sessions` by default) |
| `PraxisAuthUserPoolStack` | Cognito User Pool + public SPA client for dashboard auth | `UserPoolId`, `UserPoolClientId`, `UserPoolRegion`, `IssuerUrl` |
| `PraxisKnowledgeGraphDbStack` | RDS PostgreSQL 16 + pgvector knowledge-graph store | `DbEndpoint`, `DbSecretArn`, secret `praxis/knowledge-graph/db` |

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
  bin/app.ts                      CDK app entry — both stacks
  lib/sessions-table-stack.ts     DynamoDB session log store
  lib/knowledge-graph-db-stack.ts RDS Postgres 16 + Secrets Manager
```
