"""Offline tests for the OpenRouter backend (HTTP POST is mocked)."""

import json

import pytest

from knowledge.evals.eval_def import EvalCase, EvalContext, Rubric, RubricItem
from knowledge.evals.openrouter import (
    OpenRouterClient,
    OpenRouterJudge,
    OpenRouterRunner,
    openrouter_llm,
)
from knowledge.wiring import build_trio


def _chat_response(text):
    return json.dumps({"model": "test", "choices": [{"message": {"content": text}}]})


def _case():
    return EvalCase.model_validate(
        {
            "id": "c",
            "seed_prompt": "Write a greeting.",
            "target_commit": "abc",
            "deterministic_checks": [{"name": "x", "ref": "m:f"}],
        }
    )


def test_client_requires_api_key():
    client = OpenRouterClient(api_key="", post=lambda *a: _chat_response("hi"))
    with pytest.raises(RuntimeError):
        client.complete([{"role": "user", "content": "hi"}])


def test_runner_injects_graph_as_system_prompt():
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["payload"] = payload
        captured["headers"] = headers
        return _chat_response("hello there")

    client = OpenRouterClient(api_key="k", post=fake_post)
    graph, _, reader = build_trio()
    graph.write("Always greet warmly.")

    ctx = OpenRouterRunner(client=client).run(_case(), reader)

    assert ctx.output == "hello there"
    # Provenance captured for the transcript.
    assert ctx.output_source == "completion"
    assert "greet warmly" in ctx.injected_knowledge
    assert "hello there" in ctx.raw_response  # raw HTTP body kept verbatim
    roles = [m["role"] for m in captured["payload"]["messages"]]
    assert roles == ["system", "user"]
    assert "greet warmly" in captured["payload"]["messages"][0]["content"]
    assert captured["payload"]["temperature"] == 0.0  # greedy
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_runner_uses_case_model_override():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["model"] = payload["model"]
        return _chat_response("ok")

    client = OpenRouterClient(api_key="k", post=fake_post, model="default/model")
    _, _, reader = build_trio()
    # No override -> client default.
    OpenRouterRunner(client=client).run(_case(), reader)
    assert seen["model"] == "default/model"
    # Case override wins.
    pinned = _case().model_copy(update={"model": "openai/gpt-4o-mini"})
    OpenRouterRunner(client=client).run(pinned, reader)
    assert seen["model"] == "openai/gpt-4o-mini"


def test_serves_model_distinguishes_backends():
    assert OpenRouterRunner.serves_model("openai/gpt-4o-mini") is True
    assert OpenRouterRunner.serves_model("sonnet") is False  # a Claude alias


def test_runner_omits_system_when_graph_empty():
    def fake_post(url, payload, headers, timeout):
        assert [m["role"] for m in payload["messages"]] == ["user"]
        return _chat_response("plain")

    client = OpenRouterClient(api_key="k", post=fake_post)
    _, _, reader = build_trio()  # empty graph
    ctx = OpenRouterRunner(client=client).run(_case(), reader)
    assert ctx.output == "plain"


def test_judge_parses_overall():
    client = OpenRouterClient(
        api_key="k",
        post=lambda *a: _chat_response('{"per_item": {"q": 1.0}, "overall": 0.7}'),
    )
    rubric = Rubric(id="r", items=[RubricItem(id="q", criterion="good")])
    result = OpenRouterJudge(client=client)(rubric, EvalContext(case_id="c", output="x"))
    assert result.overall == 0.7
    assert result.per_item == {"q": 1.0}
    assert result.raw_response is not None


def test_default_post_surfaces_http_error_body(monkeypatch):
    import io
    import urllib.error

    from knowledge.evals import openrouter as orm

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"no such model: foo"}')
        )

    monkeypatch.setattr(orm.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        orm._default_post("https://x", {"a": 1}, {}, 5)
    assert "no such model: foo" in str(ei.value)  # body surfaced, not just "400"


def test_openrouter_llm_adapter_returns_text():
    client = OpenRouterClient(api_key="k", post=lambda *a: _chat_response("distilled"))
    llm = openrouter_llm(client)
    assert llm("summarize this") == "distilled"
