# praxis_client

A small, dependency-light Python client for using a **Praxis** backend as a
knowledge-graph from another repo. Authenticate with a scoped API key, drop your
documents in, and ask for grounded context. All the KG work — distillation,
dedup, contradiction handling, retrieval ranking — stays inside Praxis.

## Install

From an external repo, install editable against your Praxis checkout:

```bash
pip install -e /path/to/praxis        # exposes the `praxis_client` package
```

Or just **copy** `praxis_client/` into your repo. It uses `httpx` if available
and otherwise falls back to the Python stdlib, so it has no hard dependencies.

## Auth model

Every request sends two headers:

- `X-Praxis-Key: pxk_...` — a long-lived, org-scoped API key.
- `X-Praxis-Org: <org_id>` — the org the key is scoped to.

Mint a key against your Praxis backend:

```bash
python -m knowledge.serve.apikeys mint --org <org_id>
```

For fully-local runs you can start the server with `PRAXIS_AUTH_DISABLED=1` and
pass any placeholder key/org.

## Usage

```python
from praxis_client import PraxisClient

client = PraxisClient(
    base_url="http://localhost:8000",   # or your prod App Runner URL
    api_key="pxk_...",
    org_id="my-org",
)

# Batch-ingest documents (distillation runs server-side):
client.ingest_batch([
    {"text": "W-2: Box 1 wages $40,000.", "source": "w2.txt"},
    {"text": "Standard deduction TY2025 single: $15,750.", "source": "rules.txt"},
])

# Retrieve grounded context:
result = client.get_context("What were the wages?", top_k=8)
print(result["context"])
for hit in result["hits"]:
    print(hit["score"], hit["source"], hit["text"])
```

## API reference

`PraxisClient(base_url: str, api_key: str, org_id: str, *, timeout: float = 30.0)`

| Method | Description |
|---|---|
| `get_context(query, top_k=8) -> dict` | `GET /context`. Returns `{"context": str, "hits": [{"id","text","score","source","scope","category"}]}`. |
| `context_text(query, top_k=8) -> str` | Convenience: just the joined context string. |
| `ingest(text, source=None, state="active") -> dict` | `POST /ingest` with one document. Returns `{"results": [{"id","action"}], "count": int}`. |
| `ingest_batch(documents, state="active") -> dict` | `POST /ingest` with many `{"text","source"}` docs. |
| `add_insight(insight, *, scope=None, category=None, source=None) -> dict` | `POST /insights`. Returns `{"summary","action","id"}`. |

Non-2xx responses raise `PraxisError` (carries `.status_code` and `.body`).

## OpenAPI / client generation

The Praxis FastAPI backend serves its OpenAPI schema at `/openapi.json` and
interactive docs at `/docs`, so you can also generate a client in any language
instead of using this one.
