# RDS PostgreSQL 16 + pgvector — Knowledge Graph Store

Stand up the PRAXIS knowledge-graph store of record on **AWS RDS PostgreSQL 16** with the **pgvector** extension, bootstrap the canonical schema, and let the candidate API swap from the JSON-file `CandidateStore` to `PostgresCandidateStore` automatically when a database DSN resolves.

**Owner:** Matthew Daw (backend / `knowledge/serve`) · **Dashboard impact:** Monica's UIs stay API-only — no Postgres env vars in `frontend/` or `frontend-react/`.

After the knowledge-graph CDK stack merge, **AWS CLI must be installed and credentials saved** so the API (or bootstrap script) can pull DB credentials from **AWS Secrets Manager**. Ask Matthew or post in standup if Secrets Manager access is blocked.

---

## Architecture primer

PRAXIS uses two AWS data stores:

| Store | Technology | Owner | Purpose |
|-------|------------|-------|---------|
| Raw session logs | DynamoDB (`praxis-sessions`) | Dominic / session-capture | Full Claude Code JSONL transcripts |
| Distilled knowledge | RDS PostgreSQL 16 + pgvector (`praxis_kg`) | Matthew / `knowledge/serve` | Dashboard candidates + KG facts/embeddings |

Dashboard clients (React) call the **candidate REST API** only. They never connect to Postgres directly. See [ARCHITECTURE_MONICA.md §17](ARCHITECTURE_MONICA.md) for the pillar boundary.

```text
Dashboard (PRAXIS_API_BASE_URL) → Candidate API (knowledge/serve)
                                      ↓ resolve_dsn() OK?
                              PostgresCandidateStore → RDS praxis_kg
                              CandidateStore (JSON)  → knowledge/serve/data/candidates.json
```

**Scope today:** The `candidates` table backs dashboard promote/reject/resolve persistence. The schema also defines `facts` and `fact_edges` with pgvector embeddings — those tables are bootstrapped but **not yet written by application code** (in-process `VectorGraph` still holds embeddings). This runbook covers the JSON → Postgres candidate-store swap.

---

## Prerequisites

- AWS account with CDK bootstrap complete in **`us-east-1`** (same region as [`infra/bin/app.ts`](../../infra/bin/app.ts))
- **Node.js 18+** for `infra/`
- **Python 3.12+** and project deps from repo root: `pip install .` or `uv sync`
- **AWS CLI v2** installed and configured with credentials that can deploy CDK stacks and read Secrets Manager

---

## 1. AWS CLI setup and credential verification

Install the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) if needed, then configure credentials:

```powershell
aws configure
# Or use SSO / named profile:
# $env:AWS_PROFILE = "your-profile"
```

Verify the CLI sees your account:

```powershell
aws --version
aws sts get-caller-identity
```

### Minimum IAM permissions

For day-to-day API + bootstrap work:

- `secretsmanager:GetSecretValue` on secret `praxis/knowledge-graph/db`

For initial CDK deploy (one-time or infra updates):

- CDK deploy permissions for RDS, EC2 (VPC/security groups), and Secrets Manager — or use an admin deploy role for capstone

### Preflight: read the DB secret

Run this **after** the CDK stack is deployed (see §2). It should return JSON, not an error:

```powershell
aws secretsmanager get-secret-value `
  --secret-id praxis/knowledge-graph/db `
  --region us-east-1 `
  --query SecretString `
  --output text
```

Expected secret shape (RDS-generated):

```json
{
  "username": "praxis",
  "password": "...",
  "engine": "postgres",
  "host": "praxiskginstance....rds.amazonaws.com",
  "port": 5432,
  "dbname": "praxis_kg"
}
```

[`knowledge/serve/db.py`](../../knowledge/serve/db.py) reads this secret via boto3 and builds a DSN automatically — you do **not** need to assemble `postgresql://...` by hand when AWS credentials are available.

**Local override:** Set `PRAXIS_DB_URL` to a full connection string to skip Secrets Manager (useful for Docker Postgres, Render, or copying the DSN once from the secret).

---

## 2. Stand up RDS PostgreSQL 16 + pgvector (CDK)

The stack [`infra/lib/knowledge-graph-db-stack.ts`](../../infra/lib/knowledge-graph-db-stack.ts) provisions:

- PostgreSQL **16.4** on `db.t4g.micro`
- Database name **`praxis_kg`**
- Master credentials in Secrets Manager as **`praxis/knowledge-graph/db`**
- Public subnets (no NAT gateways — capstone cost profile)
- Security group allowing Postgres (5432) from `allowedCidr`

Deploy from repo root:

```powershell
cd infra
npm install
npm run build

# First deploy (open to all IPs — tighten immediately after):
npx cdk deploy PraxisKnowledgeGraphDbStack

# Recommended: lock to your public IP
npx cdk deploy PraxisKnowledgeGraphDbStack -c allowedCidr=YOUR.IP.ADDR/32
```

Record the stack outputs:

| Output | Purpose |
|--------|---------|
| `DbEndpoint` | RDS hostname |
| `DbPort` | Usually `5432` |
| `DbName` | `praxis_kg` |
| `DbSecretArn` | Secrets Manager ARN |
| `DbSecretName` | `praxis/knowledge-graph/db` |

### Security notes

- Default `allowedCidr` is `0.0.0.0/0` when omitted — reachable for first bootstrap, but **pass your IP** (`/32`) before leaving the stack running.
- `deletionProtection` is `false` and removal policy is snapshot — appropriate for capstone; tighten for production.
- If the API runs on **Render**, the RDS security group must allow Render egress IPs (or temporarily widen `allowedCidr` for demo). Render does not have a fixed egress IP on free tier — prefer `PRAXIS_DB_URL` on the API service and test connectivity from Render logs.

---

## 3. Bootstrap schema (pgvector + tables)

From **repo root**, apply the idempotent schema in [`knowledge/serve/schema.sql`](../../knowledge/serve/schema.sql):

```powershell
# Uses Secrets Manager when PRAXIS_DB_URL is unset
$env:AWS_REGION = "us-east-1"
python -m knowledge.serve.db
```

Expected output: `bootstrap: applied schema.sql`

This creates:

- `CREATE EXTENSION vector` (pgvector)
- `candidates` — dashboard JSON in `doc jsonb`
- `facts` — KG rows with `embedding vector(1536)` and HNSW index
- `fact_edges` — contradiction/support edges between facts

Safe to re-run — all statements use `IF NOT EXISTS`.

**Alternative with explicit DSN:**

```powershell
$env:PRAXIS_DB_URL = "postgresql://praxis:PASSWORD@HOST:5432/praxis_kg"
python -m knowledge.serve.db
```

---

## 4. JSON CandidateStore → Postgres (automatic swap)

No dashboard code changes are required. [`knowledge/serve/app.py`](../../knowledge/serve/app.py) `default_store()` picks the backing store at startup:

| Condition | Store class | Persistence |
|-----------|-------------|-------------|
| `PRAXIS_DB_URL` set, **or** Secrets Manager reachable | `PostgresCandidateStore` | `candidates` table |
| Neither | `CandidateStore` | `knowledge/serve/data/candidates.json` |

### Candidate API environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PRAXIS_DB_URL` | — | Full Postgres DSN (**highest priority**) |
| `PRAXIS_DB_SECRET` | `praxis/knowledge-graph/db` | Secrets Manager secret name |
| `AWS_REGION` | `us-east-1` | Region for boto3 Secrets Manager client |
| `PRAXIS_ORG_ID` | `default` | Tenant org for multi-tenant rows |
| `PRAXIS_USER_ID` | `default` | Tenant user |
| `PRAXIS_SHARED` | `false` | When `true`, rows visible to whole org |

> **Note:** Some older docs mention `DATABASE_URL`. The implementation uses **`PRAXIS_DB_URL`** — not `DATABASE_URL`.

### Start the API locally with Postgres

**Option A — AWS creds + Secrets Manager (no `PRAXIS_DB_URL`):**

```powershell
$env:AWS_REGION = "us-east-1"
uv run python -m knowledge.serve
```

**Option B — explicit DSN (Render, CI, or local Docker):**

```powershell
$env:PRAXIS_DB_URL = "postgresql://praxis:PASSWORD@HOST:5432/praxis_kg"
uv run python -m knowledge.serve
```

API listens on `http://localhost:8000` by default.

### Render `praxis-candidate-api`

The Render blueprint in [`frontend-react/render.yaml`](../../frontend-react/render.yaml) does **not** inject DB credentials. For a persisted store on Render:

1. Build a DSN from the Secrets Manager JSON (username, password, host, port, dbname).
2. Add **`PRAXIS_DB_URL`** to the `praxis-candidate-api` service environment in the Render dashboard.
3. Ensure RDS security group allows connections from Render (see §2 security notes).

Render cannot use Secrets Manager unless you also inject `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` with `GetSecretValue` permission — copying `PRAXIS_DB_URL` once is simpler for capstone.

---

## 5. Wire the dashboard to the Postgres-backed API

Monica's pillar configures **API URL only** — never DB credentials.

| Client | Env var | Example |
|--------|---------|---------|
| React (build-time) | `VITE_PRAXIS_API_BASE_URL` | Set via `fromService` in blueprint or Render env |

Full Render steps: [RENDER_DEPLOY.md](RENDER_DEPLOY.md) — **Live integration blueprint** section.

Local wire-up: [wire-up.md](../integration/wire-up.md) §3.

---

## 6. Verification checklist

1. **Health endpoint**

   ```powershell
   curl http://localhost:8000/health
   ```

   Expect `{"status":"ok","candidates":N}` with `N > 0` after first-run seed.

2. **Persistence survives restart**

   - Promote a proposed candidate via dashboard or `POST /candidates/{id}/promote`
   - Stop and restart the API
   - Confirm state is still `suggested` (Postgres path) — JSON fallback would also persist to file, but Postgres survives across hosts

3. **Postgres store tests**

   ```powershell
   $env:AWS_REGION = "us-east-1"   # or set PRAXIS_DB_URL
   uv run pytest knowledge/serve/tests/test_postgres_store.py -v
   ```

4. **Integration smoke (dashboard)**

   Follow [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) live-API sections with `PRAXIS_API_BASE_URL` pointed at your API.

5. **JSON fallback (offline demo)**

   Unset `PRAXIS_DB_URL` and ensure AWS creds are unavailable → API uses `knowledge/serve/data/candidates.json`. Useful for mock-only rehearsals.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| API mutations don't survive across hosts | `resolve_dsn()` returned `None` — still on JSON store | Set `PRAXIS_DB_URL` or fix AWS CLI creds / secret access |
| `bootstrap` connection timeout | Security group or `allowedCidr` blocks your IP | Redeploy with `-c allowedCidr=YOUR.IP/32` or add Render egress |
| `permission denied for extension vector` | Wrong Postgres version or extension unavailable | Confirm RDS 16.4; re-run `python -m knowledge.serve.db` |
| `NoCredentialsError` from boto3 | AWS CLI not configured | Run `aws configure` or set `AWS_PROFILE` |
| Secret fetch 403 | IAM missing `secretsmanager:GetSecretValue` | Attach policy for secret ARN from stack output |
| Render API can't reach RDS | SG allows only your laptop IP | Widen CIDR temporarily or use VPN; set `PRAXIS_DB_URL` on API service |
| Dashboard shows stale list after promote | Client cache | Reload React page or use **Refresh data** toolbar |

If Secrets Manager or RDS access is blocked on your account, ask Matthew or raise it in daily standup — do not commit credentials to the repo.

---

## Out of scope (future work)

- Writing promoted `active` candidates into the `facts` table (schema ready; app code not wired)
- Replacing in-process `VectorGraph` with pgvector similarity queries
- Production hardening (private subnets, NAT, deletion protection)
- Automatic `PRAXIS_DB_URL` injection in `render.yaml` blueprints

---

## Related docs

| Doc | Purpose |
|-----|---------|
| [RENDER_DEPLOY.md](RENDER_DEPLOY.md) | Dashboard + API Render deploy |
| [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) | End-to-end smoke checklist |
| [wire-up.md](../integration/wire-up.md) | Local dashboard ↔ API wiring |
| [candidate-api-v1.md](../integration/candidate-api-v1.md) | REST contract |
| [infra/README.md](../../infra/README.md) | CDK stack overview |
| [session-capture/README.md](../../session-capture/README.md) | DynamoDB session store (separate from KG) |
