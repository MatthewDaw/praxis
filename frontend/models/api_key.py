"""
===============================================================================
FILE: models/api_key.py

PURPOSE:
Typed API-key models aligned with the apikeys backend contract.
Python field names use snake_case; JSON/API uses camelCase at the HTTP boundary.

CONTRACT:
- POST /apikeys           {"label": str|null}
      -> {"id","key","label","createdAt"}   (raw key shown ONCE)
- GET  /apikeys           -> [{"id","label","userId","createdAt","lastUsedAt","revoked"}]
- POST /apikeys/{id}/revoke -> {"id","revoked": true}

SECURITY:
- Display-only types; the raw ``key`` (``pxk_...``) is only ever present on the
  create response (``CreatedApiKey``) and is never returned by the list endpoint.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ApiKey:
    """An API key as returned by ``GET /apikeys`` (never includes the raw key)."""

    id: str
    label: str | None
    user_id: str
    created_at: str
    last_used_at: str | None
    revoked: bool

    @property
    def status(self) -> str:
        """UI label for the key's lifecycle state."""
        return "revoked" if self.revoked else "active"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ApiKey":
        return cls(
            id=str(data.get("id", "")),
            label=_opt_str(data, "label"),
            user_id=_first_str(data, "userId", "user_id", default=""),
            created_at=_first_str(data, "createdAt", "created_at", default=""),
            last_used_at=_opt_str(data, "lastUsedAt", "last_used_at"),
            revoked=bool(data.get("revoked", False)),
        )


@dataclass(frozen=True)
class CreatedApiKey:
    """Response of ``POST /apikeys`` — the only place the raw key is exposed."""

    id: str
    key: str
    label: str | None
    created_at: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CreatedApiKey":
        return cls(
            id=str(data.get("id", "")),
            key=str(data.get("key", "")),
            label=_opt_str(data, "label"),
            created_at=_first_str(data, "createdAt", "created_at", default=""),
        )


def _opt_str(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None:
            text = str(value)
            return text if text else None
    return None


def _first_str(data: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return default
