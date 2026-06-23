"""Read-only FastAPI proxy in front of the Arize Phoenix REST API.

The React dashboard calls this proxy (never Phoenix directly) so the Phoenix
Bearer key stays server-side and out of the static bundle. Only safe, read-only
trace lookups are exposed; the normalized response never echoes the API key.

Run locally::

    uvicorn frontend.phoenix_proxy.app:app --host 0.0.0.0 --port 8800

Environment:
    PHOENIX_BASE_URL   Phoenix origin (default ``https://phoenix.praxiskg.com``).
    PHOENIX_API_KEY    Bearer token for Phoenix (read-only key preferred).
    PHOENIX_PROJECT    Default project identifier (name or id) to query.
    PHOENIX_PROJECT_UI_ID  Phoenix UI project node id (e.g. UHJvamVjdDoz).
    PHOENIX_CORS_ORIGINS      Comma-separated exact origins to allow (optional).
    PHOENIX_CORS_ORIGIN_REGEX Override the default localhost/Render CORS regex.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DEFAULT_PHOENIX_BASE_URL = "https://phoenix.praxiskg.com"
# Mirrors knowledge/serve CORS: localhost (any port) + Render/CloudFront/App Runner.
_DEFAULT_CORS_REGEX = (
    r"(http://(localhost|127\.0\.0\.1):\d+|https://[\w-]+\.onrender\.com"
    r"|https://[\w-]+\.cloudfront\.net|https://[\w-]+\.awsapprunner\.com)"
)


@dataclass(frozen=True)
class PhoenixSettings:
    """Resolved Phoenix connection settings (loaded from the environment)."""

    base_url: str
    api_key: str | None
    project: str | None
    project_ui_id: str | None = None

    @classmethod
    def from_env(cls) -> "PhoenixSettings":
        base = os.getenv("PHOENIX_BASE_URL", "").strip() or DEFAULT_PHOENIX_BASE_URL
        return cls(
            base_url=base.rstrip("/"),
            api_key=os.getenv("PHOENIX_API_KEY", "").strip() or None,
            project=os.getenv("PHOENIX_PROJECT", "").strip() or None,
            project_ui_id=os.getenv("PHOENIX_PROJECT_UI_ID", "").strip() or None,
        )


def _cors_origin_regex() -> str:
    custom = os.getenv("PHOENIX_CORS_ORIGIN_REGEX", "").strip()
    return custom or _DEFAULT_CORS_REGEX


def _as_float(value: Any) -> float | None:
    """Best-effort float coercion that tolerates strings and ``None``."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-null value among ``keys``."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _path_value(mapping: dict[str, Any], path: str) -> Any:
    cursor: Any = mapping
    for part in path.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    return cursor


def _nested_first(mapping: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _path_value(mapping, path) if "." in path else mapping.get(path)
        if value is not None:
            return value
    return None


def _global_id(kind: str, value: Any) -> str | None:
    """Return a Phoenix GraphQL node id for numeric or already-global ids."""
    text = _clean_str(value)
    if not text:
        return None
    prefix = f"{kind}:"
    if text.startswith(prefix):
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    padded = text + ("=" * (-len(text) % 4))
    try:
        decoded = base64.b64decode(padded, validate=False).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        decoded = ""
    if decoded.startswith(prefix):
        return text

    if text.isdigit():
        return base64.b64encode(f"{kind}:{text}".encode("utf-8")).decode("ascii")
    return None


def _span_attribute(span: dict[str, Any], *paths: str) -> Any:
    """Read an OpenInference attribute by dotted path or flattened key.

    Phoenix spans expose semantic attributes either as a nested ``attributes``
    dict (``llm.token_count.prompt``) or, depending on serialization, as flat
    keys. Check both so token/model extraction is resilient.
    """
    attributes = span.get("attributes")
    for path in paths:
        if isinstance(attributes, dict):
            if path in attributes and attributes[path] is not None:
                return attributes[path]
            cursor: Any = attributes
            ok = True
            for part in path.split("."):
                if isinstance(cursor, dict) and part in cursor:
                    cursor = cursor[part]
                else:
                    ok = False
                    break
            if ok and cursor is not None:
                return cursor
        if path in span and span[path] is not None:
            return span[path]
    return None


def normalize_span(span: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Phoenix span to the fields the dashboard renders."""
    latency = _as_float(_first(span, "latency_ms", "latencyMs"))
    if latency is None:
        start = _first(span, "start_time", "startTime")
        end = _first(span, "end_time", "endTime")
        start_f, end_f = _as_float(start), _as_float(end)
        if start_f is not None and end_f is not None:
            latency = (end_f - start_f) * 1000.0
    status = _first(span, "status_code", "statusCode")
    return {
        "name": str(_first(span, "name", "span_name") or "span"),
        "kind": str(_first(span, "span_kind", "spanKind", "kind") or "UNKNOWN"),
        "latencyMs": round(latency, 2) if latency is not None else None,
        "statusCode": str(status) if status is not None else None,
    }


def _aggregate_tokens(spans: list[dict[str, Any]]) -> dict[str, int | None]:
    """Sum prompt/completion/total token counts across a trace's spans."""
    prompt = completion = total = 0
    saw_any = False
    for span in spans:
        p = _as_float(
            _span_attribute(span, "llm.token_count.prompt", "llm_token_count_prompt")
        )
        c = _as_float(
            _span_attribute(
                span, "llm.token_count.completion", "llm_token_count_completion"
            )
        )
        t = _as_float(
            _span_attribute(span, "llm.token_count.total", "llm_token_count_total")
        )
        if p is not None:
            prompt += int(p)
            saw_any = True
        if c is not None:
            completion += int(c)
            saw_any = True
        if t is not None:
            total += int(t)
            saw_any = True
    if not saw_any:
        return {"prompt": None, "completion": None, "total": None}
    if total == 0 and (prompt or completion):
        total = prompt + completion
    return {"prompt": prompt or None, "completion": completion or None, "total": total or None}


def _model_name(spans: list[dict[str, Any]]) -> str | None:
    for span in spans:
        model = _span_attribute(span, "llm.model_name", "llm_model_name", "model_name")
        if model:
            return str(model)
    return None


def _trace_id(trace: dict[str, Any]) -> str:
    return str(_first(trace, "trace_id", "traceId", "context.trace_id", "id") or "")


def _project_ui_id(trace: dict[str, Any], fallback: str | None) -> str | None:
    return _global_id("Project", fallback) or _global_id(
        "Project",
        _nested_first(
            trace,
            "project_node_id",
            "projectNodeId",
            "project_gid",
            "projectGid",
            "project.id",
            "project.node_id",
            "project.nodeId",
            "project.global_id",
            "project.globalId",
            "project_id",
            "projectId",
        ),
    )


def _span_route_id(span: dict[str, Any]) -> str | None:
    return _clean_str(
        _nested_first(
            span,
            "span_id",
            "spanId",
            "context.span_id",
            "context.spanId",
            "otel_span_id",
            "otelSpanId",
        )
    )


def _span_node_id(span: dict[str, Any]) -> str | None:
    return _global_id(
        "Span",
        _nested_first(
            span,
            "span_node_id",
            "spanNodeId",
            "node_id",
            "nodeId",
            "global_id",
            "globalId",
            "id",
        ),
    )


def _deep_link_span(spans: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    fallback: tuple[str | None, str | None] = (None, None)
    for span in spans:
        span_id = _span_route_id(span)
        if not span_id:
            continue
        node_id = _span_node_id(span)
        if node_id:
            return span_id, node_id
        if fallback == (None, None):
            fallback = (span_id, None)
    return fallback


def phoenix_spans_url(
    *,
    base_url: str,
    project_ui_id: str | None,
    span_id: str | None,
    span_node_id: str | None = None,
) -> str | None:
    """Build a Phoenix UI deep link for a concrete span."""
    if not project_ui_id or not span_id:
        return None
    query = {"timeRangeKey": "7d"}
    if span_node_id:
        query["selectedSpanNodeId"] = span_node_id
    return (
        f"{base_url}/projects/{quote(project_ui_id, safe='')}"
        f"/spans/{quote(span_id, safe='')}?{urlencode(query)}"
    )


def normalize_trace(
    trace: dict[str, Any],
    *,
    base_url: str,
    project: str | None,
    project_ui_id: str | None = None,
) -> dict[str, Any]:
    """Reduce a Phoenix trace (optionally with spans) to a dashboard shape.

    The Phoenix API key is never part of this output.
    """
    raw_spans = trace.get("spans")
    spans = [normalize_span(s) for s in raw_spans] if isinstance(raw_spans, list) else []
    span_dicts = raw_spans if isinstance(raw_spans, list) else []

    latency = _as_float(_first(trace, "latency_ms", "latencyMs"))
    if latency is None:
        start = _first(trace, "start_time", "startTime")
        end = _first(trace, "end_time", "endTime")
        start_f, end_f = _as_float(start), _as_float(end)
        if start_f is not None and end_f is not None:
            latency = (end_f - start_f) * 1000.0

    status = _first(trace, "status_code", "statusCode")
    trace_id = _trace_id(trace)
    span_id, span_node_id = _deep_link_span(span_dicts)
    phoenix_url = phoenix_spans_url(
        base_url=base_url,
        project_ui_id=_project_ui_id(trace, project_ui_id or project),
        span_id=span_id,
        span_node_id=span_node_id,
    )

    return {
        "traceId": trace_id,
        "startTime": _first(trace, "start_time", "startTime"),
        "latencyMs": round(latency, 2) if latency is not None else None,
        "statusCode": str(status) if status is not None else None,
        "spanCount": len(spans) if spans else _as_float(_first(trace, "span_count", "spanCount")),
        "tokens": _aggregate_tokens(span_dicts),
        "model": _model_name(span_dicts),
        "spans": spans,
        "phoenixUrl": phoenix_url,
    }


def _extract_trace_list(payload: Any) -> list[dict[str, Any]]:
    """Pull the trace array out of a Phoenix list response (``{data: [...]}``)."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    return []


def create_app(settings: PhoenixSettings | None = None, *, transport: Any = None) -> FastAPI:
    """Build the proxy app. ``transport`` lets tests inject an httpx transport."""
    app = FastAPI(title="Praxis Phoenix Proxy", version="1")

    explicit_origins = [
        origin.strip()
        for origin in os.getenv("PHOENIX_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    cors_kwargs: dict[str, Any] = {"allow_methods": ["GET"], "allow_headers": ["*"]}
    if explicit_origins:
        cors_kwargs["allow_origins"] = explicit_origins
    else:
        cors_kwargs["allow_origin_regex"] = _cors_origin_regex()
    app.add_middleware(CORSMiddleware, **cors_kwargs)

    def resolved_settings() -> PhoenixSettings:
        return settings if settings is not None else PhoenixSettings.from_env()

    @app.get("/health")
    def health() -> dict[str, Any]:
        cfg = resolved_settings()
        return {
            "status": "ok",
            "phoenixConfigured": bool(cfg.api_key and cfg.project),
            "project": cfg.project,
        }

    @app.get("/phoenix/traces")
    async def phoenix_traces(
        trace_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        project: str | None = Query(default=None),
        limit: int = Query(default=25, gt=0, le=100),
    ) -> dict[str, Any]:
        """Return normalized Phoenix traces for a candidate.

        ``trace_id`` filters to a single trace; ``session_id`` maps to Phoenix's
        ``session_identifier``. Spans are included so the card can show
        latency/token/model context in one request.
        """
        cfg = resolved_settings()
        active_project = (project or cfg.project or "").strip()
        if not cfg.api_key or not active_project:
            raise HTTPException(
                status_code=503,
                detail="Phoenix proxy is not configured (set PHOENIX_API_KEY and PHOENIX_PROJECT).",
            )

        params: dict[str, Any] = {"include_spans": "true", "limit": limit}
        if session_id:
            params["session_identifier"] = session_id

        url = f"{cfg.base_url}/v1/projects/{active_project}/traces"
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
                response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Phoenix request failed: {exc}")

        if response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"Phoenix project {active_project!r} not found.",
            )
        if response.status_code >= 400:
            # Never surface the upstream body verbatim — it could echo the key.
            raise HTTPException(
                status_code=502,
                detail=f"Phoenix returned {response.status_code} listing traces.",
            )

        traces = _extract_trace_list(response.json())
        normalized = [
            normalize_trace(
                t,
                base_url=cfg.base_url,
                project=active_project,
                project_ui_id=cfg.project_ui_id,
            )
            for t in traces
        ]
        if trace_id:
            normalized = [t for t in normalized if t["traceId"] == trace_id]

        return {
            "project": active_project,
            "phoenixBaseUrl": cfg.base_url,
            "traces": normalized,
        }

    return app


app = create_app()
