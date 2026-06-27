"""
PraxisClient — a small, importable HTTP client for using Praxis as a
knowledge-graph backend from an external repo.

Contract (Praxis HTTP API):
  Auth headers on EVERY request:
    X-Praxis-Key: pxk_...     (scoped API key)
    X-Praxis-Org: <org_id>
  (A Cognito Bearer token also works server-side, but this SDK targets API-key auth.)

  GET  /context?query=<q>&top_k=<n>
      -> {"context": str, "hits": [{"id","text","score","source","scope","category"}]}
  POST /ingest   {"documents": [{"text": str, "source": str|null}], "state": "active",
                  "onConflict": "auto_resolve"|"surface"}
      -> {"results": [{"id","action","surfaced"}], "count": int}  (server-side distillation)
  POST /insights {"insight": str, "scope": str|null, "category": str|null, "source": str|null,
                  "onConflict": "auto_resolve"|"surface"}
      -> {"summary","action","id","onConflict","contradictionsSurfaced"}

Dependency-light: uses ``httpx`` when available (it is a Praxis dependency), and
falls back to the stdlib ``urllib`` so the module works when copied into a repo
that does not have httpx installed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:  # prefer httpx when present (a Praxis dependency); degrade gracefully.
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - exercised only in stdlib-only envs
    httpx = None  # type: ignore[assignment]


class PraxisError(Exception):
    """Raised when the Praxis API returns a non-2xx response.

    Carries the HTTP ``status_code`` and the raw response ``body`` text so
    callers can inspect or log the failure.
    """

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PraxisClient:
    """Thin, typed client over the Praxis knowledge-graph HTTP API.

    Example:
        client = PraxisClient(
            base_url="http://localhost:8000",
            api_key="pxk_...",
            org_id="my-org",
        )
        client.ingest("My W-2 shows wages of $40,000.", source="w2.pdf")
        ctx = client.context_text("What were the wages?")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        org_id: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        if not org_id:
            raise ValueError("org_id is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._org_id = org_id
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    def get_context(
        self,
        query: str,
        top_k: int = 8,
        *,
        category: str | None = None,
        categories: list[str] | None = None,
        scope: str | None = None,
        meta: dict | str | None = None,
    ) -> dict[str, Any]:
        """Retrieve grounded context for ``query`` (similarity-ranked).

        Returns the parsed JSON: ``{"context": str, "hits": [...]}`` where each
        hit has ``id, text, score, source, scope, category``.

        Optional POSITIVE filters narrow the ranked results to a subset (still
        ranked, not exhaustive): ``category`` (single) and/or ``categories`` (list)
        keep only those categories; ``scope`` matches the top-level scope; ``meta``
        (a dict, or a pre-encoded JSON string) filters the JSONB ``meta`` column by
        scalar equality OR array-membership — e.g. ``category="check",
        meta={"scope": "planning"}``. Omitting all of them is unchanged behavior.
        """
        q: dict[str, Any] = {"query": query, "top_k": top_k}
        if category:
            q["category"] = category
        if categories:
            q["categories"] = ",".join(categories)
        if scope:
            q["scope"] = scope
        if meta is not None:
            q["meta"] = meta if isinstance(meta, str) else json.dumps(meta)
        params = urllib.parse.urlencode(q)
        return self._request("GET", f"/context?{params}")

    def context_text(self, query: str, top_k: int = 8) -> str:
        """Convenience: return just the joined context string from ``get_context``."""
        payload = self.get_context(query, top_k=top_k)
        return str(payload.get("context", ""))

    def ingest(
        self,
        text: str,
        source: str | None = None,
        state: str = "active",
        on_conflict: str = "auto_resolve",
    ) -> dict[str, Any]:
        """Ingest a single document. Distillation runs server-side.

        Returns ``{"results": [{"id","action"}], "count": int}``.
        """
        return self.ingest_batch(
            [{"text": text, "source": source}], state=state, on_conflict=on_conflict
        )

    def ingest_batch(
        self,
        documents: list[dict[str, Any]],
        state: str = "active",
        on_conflict: str = "auto_resolve",
    ) -> dict[str, Any]:
        """Ingest multiple documents in one call.

        Each document is ``{"text": str, "source": str | None}``. Distillation
        runs server-side. ``on_conflict`` is ``"auto_resolve"`` (default; loser
        rejected) or ``"surface"`` (keep both, raise a pending contradiction).
        Returns ``{"results": [...], "count": int}``.
        """
        normalized = [
            {"text": doc["text"], "source": doc.get("source")} for doc in documents
        ]
        body = {"documents": normalized, "state": state, "onConflict": on_conflict}
        return self._request("POST", "/ingest", body=body)

    def add_insight(
        self,
        insight: str,
        *,
        scope: str | None = None,
        category: str | None = None,
        source: str | None = None,
        on_conflict: str = "auto_resolve",
    ) -> dict[str, Any]:
        """Add a single explicit insight.

        ``on_conflict`` is ``"auto_resolve"`` (default; a conflicting fact is
        overwritten/rejected) or ``"surface"`` (keep both facts and raise a pending
        contradiction for human review). Returns ``{"summary","action","id",
        "onConflict","contradictionsSurfaced"}``.
        """
        body = {
            "insight": insight,
            "scope": scope,
            "category": category,
            "source": source,
            "onConflict": on_conflict,
        }
        return self._request("POST", "/insights", body=body)

    # -- transport ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "X-Praxis-Key": self._api_key,
            "X-Praxis-Org": self._org_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if httpx is not None:
            return self._request_httpx(method, url, body)
        return self._request_urllib(method, url, body)

    def _request_httpx(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                url,
                json=body,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:  # network/connection failure
            raise PraxisError(
                f"Praxis API unreachable: {exc}", status_code=0
            ) from exc
        text = response.text
        if not (200 <= response.status_code < 300):
            raise self._error(method, url, response.status_code, text)
        return self._parse(text)

    def _request_urllib(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return self._parse(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise self._error(method, url, exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise PraxisError(
                f"Praxis API unreachable: {exc.reason}", status_code=0
            ) from exc

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    @staticmethod
    def _error(method: str, url: str, status: int, body: str) -> PraxisError:
        hint = ""
        if status in (401, 403):
            hint = (
                " (check X-Praxis-Key and X-Praxis-Org; the key must be valid and "
                "scoped to this org, or run the server with PRAXIS_AUTH_DISABLED=1)"
            )
        return PraxisError(
            f"Praxis API {method} {url} failed ({status}){hint}: {body}",
            status_code=status,
            body=body,
        )
