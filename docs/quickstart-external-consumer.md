# Quickstart: Use Praxis as your KG from another repo

This guide shows an **external agent in a separate repo** how to use Praxis as
its knowledge-graph backend with near-zero KG plumbing. You only have to:
**(a) authenticate, (b) drop your documents in, (c) ask for context.** Everything
KG-shaped — distillation, dedup, contradiction handling, retrieval ranking —
stays inside Praxis.

## Prerequisites

1. **A running Praxis backend**, either:
   - Local: `uv run python -m knowledge.serve` (defaults to `http://localhost:8000`).
   - Prod: your deployed CloudFront / App Runner backend URL.
2. **An org** you can write to (its `org_id`).
3. **A scoped API key** for that org:
   ```bash
   python -m knowledge.serve.apikeys mint --org <org_id>
   # -> pxk_...
   ```
   For fully-local runs, start the server with `PRAXIS_AUTH_DISABLED=1` and any
   placeholder key/org will be accepted.
4. **The client** — install Praxis editable (`pip install -e /path/to/praxis`)
   or copy the `praxis_client/` directory into your repo. It is dependency-light
   (uses `httpx` if present, otherwise the stdlib).

Set the connection env vars:

```bash
export PRAXIS_BASE_URL=http://localhost:8000   # or your prod URL
export PRAXIS_API_KEY=pxk_...
export PRAXIS_ORG_ID=my-org
```

## Three steps

### 1. Authenticate

The key + org are passed once when you construct the client; they are sent as
`X-Praxis-Key` and `X-Praxis-Org` on every request.

```python
from praxis_client import PraxisClient

client = PraxisClient(
    base_url=os.environ["PRAXIS_BASE_URL"],
    api_key=os.environ["PRAXIS_API_KEY"],
    org_id=os.environ["PRAXIS_ORG_ID"],
)
```

### 2. Batch-ingest your documents

One call ingests the whole bundle; distillation runs server-side.

```python
client.ingest_batch([
    {"text": "...W-2 text...", "source": "w2.txt"},
    {"text": "...1040 rules...", "source": "1040_rules.txt"},
    {"text": "...intake Q&A...", "source": "intake.txt"},
])
```

### 3. Get context

```python
result = client.get_context("What are this taxpayer's wages and filing status?")
print(result["context"])
for hit in result["hits"]:
    print(hit["score"], hit["source"], hit["text"])
```

## Full runnable example

See [`examples/use_praxis_as_kg.py`](../examples/use_praxis_as_kg.py). It reads
the three env vars above, batch-ingests a tiny tax bundle (a fake W-2, a 1040
rule, and an intake Q&A), then retrieves and prints the grounded hits. It
doubles as an end-to-end smoke test of the HTTP contract.

```bash
python examples/use_praxis_as_kg.py
```

## Generating a client in another language

The Praxis FastAPI backend serves its OpenAPI schema at `/openapi.json` and
interactive docs at `/docs`. Point any OpenAPI generator (e.g. `openapi-generator`)
at `<PRAXIS_BASE_URL>/openapi.json` to produce a typed client in your language of
choice instead of using `praxis_client`.
