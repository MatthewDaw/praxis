"""Tracing-seam tests: no-op when disabled, OpenInference spans when enabled."""

import json

from knowledge.llm import openrouter_http
from knowledge.observability import tracing


def _in_memory_tracer():
    """A real tracer wired to an in-memory exporter (no network)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


def test_setup_tracing_is_disabled_without_endpoint(monkeypatch):
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_CONFIGURED", False)
    monkeypatch.setattr(tracing, "_TRACER", None)

    assert tracing.setup_tracing() is False
    # Helpers must no-op (and not crash) with tracing off.
    with tracing.llm_span("x", model="m", input_value="hi") as span:
        assert span is None
    tracing.record_output(None, output="y", total_tokens=3)  # no crash


def test_llm_span_records_openinference_attributes(monkeypatch):
    exporter, tracer = _in_memory_tracer()
    monkeypatch.setattr(tracing, "_TRACER", tracer)

    with tracing.llm_span(
        "openrouter.chat",
        model="gpt-4o-mini",
        input_value=[{"role": "user", "content": "hi"}],
    ) as span:
        tracing.record_output(
            span,
            output="ok",
            prompt_tokens=3,
            completion_tokens=1,
            total_tokens=4,
            cost_usd=0.0001,
        )

    (recorded,) = exporter.get_finished_spans()
    attrs = dict(recorded.attributes)
    assert attrs[tracing.SPAN_KIND] == "LLM"
    assert attrs[tracing.LLM_MODEL_NAME] == "gpt-4o-mini"
    assert attrs[tracing.OUTPUT_VALUE] == "ok"
    assert attrs[tracing.LLM_TOKEN_TOTAL] == 4
    assert attrs[tracing.LLM_COST_TOTAL] == 0.0001
    assert "user" in attrs[tracing.INPUT_VALUE]  # messages serialized as JSON


def test_llm_span_records_exception(monkeypatch):
    exporter, tracer = _in_memory_tracer()
    monkeypatch.setattr(tracing, "_TRACER", tracer)

    try:
        with tracing.llm_span("boom"):
            raise RuntimeError("nope")
    except RuntimeError:
        pass

    (recorded,) = exporter.get_finished_spans()
    assert recorded.status.status_code.name == "ERROR"


def test_openrouter_chat_complete_emits_span(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    exporter, tracer = _in_memory_tracer()
    monkeypatch.setattr(tracing, "_TRACER", tracer)

    def fake_post(url, payload, headers, timeout):
        return json.dumps(
            {
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )

    out = openrouter_http.chat_complete(
        [{"role": "user", "content": "hi"}], post=fake_post
    )
    assert out == "hello"

    (recorded,) = exporter.get_finished_spans()
    attrs = dict(recorded.attributes)
    assert attrs[tracing.OUTPUT_VALUE] == "hello"
    assert attrs[tracing.LLM_TOKEN_TOTAL] == 7
