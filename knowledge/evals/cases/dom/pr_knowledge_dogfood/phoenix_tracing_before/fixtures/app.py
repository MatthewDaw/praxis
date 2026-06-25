"""Praxis serve entrypoint (FastAPI)."""

from __future__ import annotations

from fastapi import FastAPI

api = FastAPI(title="Praxis")


@api.get("/health")
def health() -> dict:
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:api", host="0.0.0.0", port=8000)
