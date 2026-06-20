"""Optional OpenTelemetry tracing to an Arize Phoenix collector.

Tracing is OFF unless ``PHOENIX_COLLECTOR_ENDPOINT`` is set. With it unset,
:func:`setup_tracing` is a no-op and the span helpers below degrade to no-ops —
so unit tests and offline runs never import OpenTelemetry or touch the network.
Set the Phoenix env vars (see ``.env.example``) and install the ``observability``
extra to light it up.

Spans carry OpenInference semantic-convention attributes so Phoenix renders them
as LLM / agent / embedding calls with token counts and cost. The attribute keys
are kept inline as plain strings to avoid a hard dependency on
``openinference-semantic-conventions``.
"""

from __future__ import annotations

import atexit
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

# OpenInference / OTel semantic-convention attribute keys.
SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
LLM_MODEL_NAME = "llm.model_name"
LLM_TOKEN_PROMPT = "llm.token_count.prompt"
LLM_TOKEN_COMPLETION = "llm.token_count.completion"
LLM_TOKEN_TOTAL = "llm.token_count.total"
LLM_COST_TOTAL = "llm.cost.total"

# Set by setup_tracing(); None means tracing is disabled and every helper no-ops.
_TRACER: Any = None
_CONFIGURED = False


def setup_tracing(project_name: str | None = None) -> bool:
    """Configure global tracing to Phoenix from the environment. Idempotent.

    Returns ``True`` when tracing was enabled. A no-op returning ``False`` when
    ``PHOENIX_COLLECTOR_ENDPOINT`` is unset or the OpenTelemetry deps aren't
    installed — so the app behaves identically whether or not tracing runs.
    """
    global _TRACER, _CONFIGURED
    if _CONFIGURED:
        return _TRACER is not None
    _CONFIGURED = True

    endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
    if not endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        import warnings

        warnings.warn(
            "PHOENIX_COLLECTOR_ENDPOINT is set but OpenTelemetry isn't installed; "
            "tracing disabled. Install the 'observability' extra to enable it.",
            stacklevel=2,
        )
        return False

    traces_url = endpoint.rstrip("/")
    if not traces_url.endswith("/v1/traces"):
        traces_url += "/v1/traces"

    headers = {}
    api_key = os.getenv("PHOENIX_API_KEY")
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    project = project_name or os.getenv("PHOENIX_PROJECT_NAME", "praxis")
    resource = Resource.create({"openinference.project.name": project})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=traces_url, headers=headers)

    # Self-signed cert (the IP-based deploy) => skip TLS verification. The HTTP
    # exporter passes ``verify=self._certificate_file`` on every request, so
    # that's the switch to flip (session.verify alone is overridden). Opt-in only.
    if os.getenv("PHOENIX_TLS_VERIFY", "true").lower() in ("false", "0", "no"):
        exporter._certificate_file = False
        session = getattr(exporter, "_session", None)
        if session is not None:
            session.verify = False
        try:
            import urllib3

            urllib3.disable_warnings()  # silence InsecureRequestWarning spam
        except ImportError:
            pass

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)  # flush spans before a short-lived CLI exits

    _TRACER = trace.get_tracer("praxis")
    return True


@contextmanager
def llm_span(
    name: str,
    *,
    kind: str = "LLM",
    model: str | None = None,
    input_value: Any = None,
) -> Iterator[Any]:
    """Open a span for one model/agent call, or a no-op when tracing is off.

    Yields the span (or ``None`` when disabled). Set the result with
    :func:`record_output` after the call returns. Exceptions are recorded on the
    span and re-raised.
    """
    if _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as span:
        span.set_attribute(SPAN_KIND, kind)
        if model:
            span.set_attribute(LLM_MODEL_NAME, model)
        if input_value is not None:
            span.set_attribute(INPUT_VALUE, _to_text(input_value))
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            from opentelemetry.trace import Status, StatusCode

            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def record_output(
    span: Any,
    *,
    output: Any = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cost_usd: float | None = None,
    **attributes: Any,
) -> None:
    """Attach the result of a call to its span. No-op when ``span`` is ``None``."""
    if span is None:
        return
    if output is not None:
        span.set_attribute(OUTPUT_VALUE, _to_text(output))
    if prompt_tokens is not None:
        span.set_attribute(LLM_TOKEN_PROMPT, int(prompt_tokens))
    if completion_tokens is not None:
        span.set_attribute(LLM_TOKEN_COMPLETION, int(completion_tokens))
    if total_tokens is not None:
        span.set_attribute(LLM_TOKEN_TOTAL, int(total_tokens))
    if cost_usd is not None:
        span.set_attribute(LLM_COST_TOTAL, float(cost_usd))
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def _to_text(value: Any) -> str:
    """Render a span value as text (JSON for structured inputs)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
