"""Offline tests for the OpenRouter backend (HTTP POST is mocked)."""

import json

import pytest

from knowledge.evals.eval_def import EvalCase, EvalContext, Rubric, RubricItem
from knowledge.evals.openrouter import (
    OpenRouterClient,
    OpenRouterJudge,
    OpenRouterRunner,
    StructuredOpenRouterRunner,
    openrouter_llm,
)
from knowledge.wiring import build_trio


def _chat_response(text):
    return json.dumps({"model": "test", "choices": [{"message": {"content": text}}]})


def _files_response(files: dict, notes=""):
    """A mocked structured reply whose content is a file_changes JSON object."""
    payload = {"file_changes": [{"path": p, "contents": c} for p, c in files.items()], "notes": notes}
    return _chat_response(json.dumps(payload))


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


def test_judge_computes_overall_ignoring_any_llm_overall():
    # The model's own "overall" (0.7) is ignored; the harness computes it from
    # per_item — here one item scored 1.0 -> 1.0.
    client = OpenRouterClient(
        api_key="k",
        post=lambda *a: _chat_response('{"per_item": {"q": 1.0}, "overall": 0.7}'),
    )
    rubric = Rubric(id="r", items=[RubricItem(id="q", criterion="good")])
    result = OpenRouterJudge(client=client)(rubric, EvalContext(case_id="c", output="x"))
    assert result.overall == 1.0
    assert result.per_item == {"q": 1.0}
    assert result.raw_response is not None


def test_judge_overall_honors_declared_weights():
    # scope (weight 3) fails, fn (weight 1) passes -> 0.25, NOT the unweighted 0.5.
    client = OpenRouterClient(
        api_key="k",
        post=lambda *a: _chat_response('{"per_item": {"scope": 0.0, "fn": 1.0}}'),
    )
    rubric = Rubric(
        id="r",
        items=[
            RubricItem(id="scope", criterion="scope respected", weight=3.0),
            RubricItem(id="fn", criterion="function correct", weight=1.0),
        ],
    )
    result = OpenRouterJudge(client=client)(rubric, EvalContext(case_id="c", output="x"))
    assert result.overall == 0.25


def test_judge_sends_per_rubric_json_schema():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["payload"] = payload
        return _chat_response('{"per_item": {"a": 1.0, "b": 0.0}}')

    client = OpenRouterClient(api_key="k", post=fake_post)
    rubric = Rubric(
        id="r",
        items=[RubricItem(id="a", criterion="x"), RubricItem(id="b", criterion="y", weight=2.0)],
    )
    result = OpenRouterJudge(client=client)(rubric, EvalContext(case_id="c", output="z"))
    pi = seen["payload"]["response_format"]["json_schema"]["schema"]["properties"]["per_item"]
    assert set(pi["properties"]) == {"a", "b"}  # exactly the rubric ids
    assert set(pi["required"]) == {"a", "b"}
    assert pi["additionalProperties"] is False
    assert result.overall == pytest.approx(1 / 3)  # a=1(w1), b=0(w2) -> 1/3


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


# --- StructuredOpenRouterRunner (structured file output -> artifacts) ---


def test_structured_runner_parses_single_file_into_artifact():
    client = OpenRouterClient(api_key="k", post=lambda *a: _files_response({"answer.py": "print('hi')\n"}))
    _, _, reader = build_trio()
    ctx = StructuredOpenRouterRunner(client=client).run(_case(), reader)
    assert ctx.output == "print('hi')\n"  # single file -> raw contents (no header)
    assert ctx.output_source == "single_file"
    assert [(a.path, a.status) for a in ctx.artifacts] == [("answer.py", "created")]


def test_structured_runner_honors_output_file():
    resp = _files_response({"answer.py": "ANSWER", "scratch.py": "SCRATCH"})
    client = OpenRouterClient(api_key="k", post=lambda *a: resp)
    case = _case().model_copy(update={"output_file": "answer.py"})
    _, _, reader = build_trio()
    ctx = StructuredOpenRouterRunner(client=client).run(case, reader)
    assert ctx.output == "ANSWER"  # graded the named file, not SCRATCH
    assert ctx.output_source == "named_file"
    assert {a.path for a in ctx.artifacts} == {"answer.py", "scratch.py"}


def test_structured_runner_injects_graph_and_requests_json_schema():
    seen = {}

    def fake_post(url, payload, headers, timeout):
        seen["payload"] = payload
        return _files_response({"answer.py": "x"})

    client = OpenRouterClient(api_key="k", post=fake_post)
    graph, _, reader = build_trio()
    graph.write("Always greet warmly.")
    StructuredOpenRouterRunner(client=client).run(_case(), reader)
    rf = seen["payload"]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    roles = [m["role"] for m in seen["payload"]["messages"]]
    assert roles == ["system", "user"]  # graph injected as system prompt
    assert "greet warmly" in seen["payload"]["messages"][0]["content"]


def test_structured_runner_raises_on_malformed_output():
    client = OpenRouterClient(api_key="k", post=lambda *a: _chat_response("not json at all"))
    _, _, reader = build_trio()
    with pytest.raises(RuntimeError) as ei:
        StructuredOpenRouterRunner(client=client).run(_case(), reader)
    assert "file_changes" in str(ei.value)  # loud, specific capability error


def test_structured_runner_capability_and_model():
    assert StructuredOpenRouterRunner.provides == frozenset({"file_io"})
    assert StructuredOpenRouterRunner.serves_model("openai/gpt-4o-mini") is True
    assert StructuredOpenRouterRunner.serves_model("sonnet") is False  # a Claude alias


def test_judge_model_prefers_judge_env_then_runner_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "runner/model")
    monkeypatch.setenv("OPENROUTER_JUDGE_MODEL", "judge/model")
    assert OpenRouterJudge().client.model == "judge/model"  # judge env wins
    monkeypatch.delenv("OPENROUTER_JUDGE_MODEL")
    assert OpenRouterJudge().client.model == "runner/model"  # falls back to runner model
