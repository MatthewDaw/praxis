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

Tenancy model (org -> space -> snapshot + per-user working memory):
  Working-memory ops (``get_context``, ``ingest``, ``add_insight``) are scoped to the
  authenticated principal and never send a space header. The org-shared ``(space,
  snapshot)`` graphs are addressed by EXPLICIT params only, on the snapshot/mount
  methods (``save_snapshot``/``load_snapshot``/``list_snapshots``/``delete_snapshot``/
  ``mount_snapshot``/``unmount_snapshot``/``list_mounts``) and, optionally, on
  ``get_context`` when BOTH ``space`` and ``snapshot`` are passed together.

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


def _require(value: str | None, name: str) -> str:
    """Return a non-empty ``value`` or raise (fail-closed on a missing scope)."""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _as_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Extract a list from a response envelope.

    Tolerates the ``{"<key>": [...]}`` envelope the server emits and a bare array
    (which ``_parse`` wraps under ``"data"``).
    """
    value = payload.get(key)
    if value is None:
        value = payload.get("data", [])
    return value if isinstance(value, list) else []


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
        space: str | None = None,
        snapshot: str | None = None,
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

        By default the read is scoped to the caller's working memory. Pass BOTH
        ``space`` and ``snapshot`` to read an org-shared snapshot instead; passing
        only one of them is a mis-scoped read and raises ``ValueError`` (fail-closed
        — never silently fall back to working memory).
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
        if space is not None or snapshot is not None:
            q["space"] = _require(space, "space")
            q["snapshot"] = _require(snapshot, "snapshot")
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

    # -- snapshots (org-shared, explicit space+snapshot) --------------------

    def save_snapshot(self, space: str, snapshot: str) -> dict[str, Any]:
        """Dump the caller's working memory into ``snapshots(org, space, snapshot)``.

        ``POST /snapshots {space, snapshot}``. Returns the server ack (e.g.
        ``{"space","snapshot","count"}``).
        """
        return self._request(
            "POST",
            "/snapshots",
            body={
                "space": _require(space, "space"),
                "snapshot": _require(snapshot, "snapshot"),
            },
        )

    def load_snapshot(
        self, space: str, snapshot: str, mode: str = "replace"
    ) -> dict[str, Any]:
        """Copy an org-shared snapshot into the caller's working memory.

        ``POST /snapshots/load {space, snapshot, mode}``. ``mode="replace"``
        (default) truncates working memory then inserts the snapshot; ``mode="add"``
        additively merges. Returns ``{"loaded": int, "mode": str}``.
        """
        return self._request(
            "POST",
            "/snapshots/load",
            body={
                "space": _require(space, "space"),
                "snapshot": _require(snapshot, "snapshot"),
                "mode": mode,
            },
        )

    def list_snapshots(self, space: str) -> list[dict[str, Any]]:
        """List the snapshots in ``space``.

        ``GET /snapshots?space=<space>``. Returns a list of
        ``{"snapshot","count","createdAt"}``.
        """
        params = urllib.parse.urlencode({"space": _require(space, "space")})
        return _as_list(self._request("GET", f"/snapshots?{params}"), "snapshots")

    def delete_snapshot(self, space: str, snapshot: str) -> dict[str, Any]:
        """Delete the snapshot keyed ``(org, space, snapshot)``.

        ``DELETE /snapshots {space, snapshot}``. Also unmounts any overlay that
        referenced it. Returns the server ack.
        """
        return self._request(
            "DELETE",
            "/snapshots",
            body={
                "space": _require(space, "space"),
                "snapshot": _require(snapshot, "snapshot"),
            },
        )

    # -- mounts (read-only retrieval overlays) ------------------------------

    def mount_snapshot(self, space: str, snapshot: str) -> dict[str, Any]:
        """Mount an org-shared snapshot as a read-only overlay on working memory.

        ``POST /mounts {space, snapshot}``. Returns the server ack.
        """
        return self._request(
            "POST",
            "/mounts",
            body={
                "space": _require(space, "space"),
                "snapshot": _require(snapshot, "snapshot"),
            },
        )

    def unmount_snapshot(self, space: str, snapshot: str) -> dict[str, Any]:
        """Remove a mounted overlay (no-op if it was not mounted).

        ``DELETE /mounts {space, snapshot}``. Returns the server ack.
        """
        return self._request(
            "DELETE",
            "/mounts",
            body={
                "space": _require(space, "space"),
                "snapshot": _require(snapshot, "snapshot"),
            },
        )

    def list_mounts(self) -> list[dict[str, Any]]:
        """List the caller's mounted overlays.

        ``GET /mounts``. Returns a list of ``{"space","snapshot","count"}``.
        """
        return _as_list(self._request("GET", "/mounts"), "mounts")

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
