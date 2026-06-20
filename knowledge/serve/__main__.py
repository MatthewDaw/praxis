"""Run the candidate API: uv run python -m knowledge.serve"""

from __future__ import annotations

import os

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    # Pick up COGNITO_* / PRAXIS_* from the repo-root .env (serve has no other
    # mechanism to load it) before resolving config below.
    load_dotenv()
    port = int(os.getenv("PORT", os.getenv("PRAXIS_API_PORT", "8000")))
    host = os.getenv("PRAXIS_API_HOST", "127.0.0.1")
    uvicorn.run("knowledge.serve.app:app", host=host, port=port, log_level="info")
