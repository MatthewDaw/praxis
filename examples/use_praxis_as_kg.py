"""Use Praxis as a knowledge-graph backend — runnable smoke test.

Reads PRAXIS_BASE_URL / PRAXIS_API_KEY / PRAXIS_ORG_ID from the environment,
batch-ingests a tiny tax bundle, then retrieves grounded context.

Run:
    export PRAXIS_BASE_URL=http://localhost:8000
    export PRAXIS_API_KEY=pxk_...
    export PRAXIS_ORG_ID=my-org
    python examples/use_praxis_as_kg.py
"""

import os

from praxis_client import PraxisClient

client = PraxisClient(
    base_url=os.environ["PRAXIS_BASE_URL"],
    api_key=os.environ["PRAXIS_API_KEY"],
    org_id=os.environ["PRAXIS_ORG_ID"],
)

bundle = [
    {"text": "Form W-2: Employee Jane Doe, Box 1 wages $40,000, Box 2 federal income tax withheld $3,200.", "source": "w2.txt"},
    {"text": "TY2025 Form 1040: the standard deduction is $15,750 for single filers; subtract it from total income to get taxable income.", "source": "1040_rules.txt"},
    {"text": "Intake Q&A: The taxpayer is single, has one job, and no dependents.", "source": "intake.txt"},
]

ingest_result = client.ingest_batch(bundle, state="active")
print(f"Ingested {ingest_result.get('count')} documents.")

result = client.get_context("What are this taxpayer's wages and filing status?")
print(f"\nContext:\n{result.get('context', '')}\n")
print("Hits:")
for hit in result.get("hits", []):
    score = hit.get("score") or 0.0
    print(f"  [{score:.3f}] ({hit.get('source')}) {hit.get('text')}")
