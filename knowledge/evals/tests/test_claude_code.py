"""Offline tests for the real-Claude-Code runner/judge wiring.

The CLI invocation is faked, so these verify the sealed-box flags, subscription
auth (no API key), system-prompt knowledge injection, and output handling
without launching the binary.
"""

import json
from pathlib import Path

import pytest

from knowledge.evals.claude_code import ClaudeCodeJudge, ClaudeCodeRunner
from knowledge.evals.eval_def import EvalCase, EvalContext, Rubric, RubricItem
from knowledge.wiring import build_trio


def _case():
    return EvalCase.model_validate(
        {
            "id": "iambic_poem",
            "seed_prompt": "Write a poem to poem.txt",
            "target_commit": "abc",
            "deterministic_checks": [{"name": "x", "ref": "m:f"}],
        }
    )


def test_runner_injects_knowledge_boxes_and_scrubs_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    captured = {}

    def fake_cli(args, cwd, env, timeout):
        captured["args"] = args
        captured["cwd"] = Path(cwd)
        captured["env"] = env
        # Simulate the agent writing the artifact into the box.
        (Path(cwd) / "poem.txt").write_text("a boxed poem", encoding="utf-8")
        return json.dumps({"result": "done"})

    graph, _, reader = build_trio()  # fresh in-memory graph
    graph.write("Always write in iambic pentameter.")

    ctx = ClaudeCodeRunner(run_cli=fake_cli).run(_case(), reader)

    assert ctx.output == "a boxed poem"
    # Provenance captured for the transcript.
    assert ctx.raw_response == json.dumps({"result": "done"})
    assert ctx.output_source == "named_file"
    assert "iambic pentameter" in ctx.injected_knowledge
    assert "ANTHROPIC_API_KEY" not in captured["env"]  # subscription auth
    # Knowledge injected via system prompt — no file on disk.
    assert "--append-system-prompt" in captured["args"]
    idx = captured["args"].index("--append-system-prompt")
    assert "iambic pentameter" in captured["args"][idx + 1]
    # Box restrictions present; cwd is a throwaway dir the runner created.
    assert "WebSearch" in captured["args"] and "Bash" in captured["args"]
    assert "bypassPermissions" in captured["args"]
    assert isinstance(captured["cwd"], Path)


def test_runner_omits_injection_when_graph_empty():
    def fake_cli(args, cwd, env, timeout):
        assert "--append-system-prompt" not in args  # nothing to inject
        return json.dumps({"result": "inline poem text"})

    graph, _, reader = build_trio()  # empty graph
    ctx = ClaudeCodeRunner(run_cli=fake_cli).run(_case(), reader)
    assert ctx.output == "inline poem text"  # falls back to result text


def test_runner_copies_fixture_into_box(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "calculator.py").write_text("def sub(a, b):\n    return a - b\n", encoding="utf-8")

    seen = {}

    def fake_cli(args, cwd, env, timeout):
        # The fixture is present in the box when the agent starts; simulate an edit.
        calc = Path(cwd) / "calculator.py"
        seen["start_state"] = calc.read_text(encoding="utf-8")
        calc.write_text(seen["start_state"] + "\ndef add(a, b):\n    return a + b\n", encoding="utf-8")
        return json.dumps({"result": "done"})

    case = _case().model_copy(update={"fixture_path": str(fixture)})
    _, _, reader = build_trio()
    ctx = ClaudeCodeRunner(run_cli=fake_cli).run(case, reader)

    assert "def sub" in seen["start_state"]  # fixture was seeded before the run
    assert "def sub" in ctx.output and "def add" in ctx.output  # graded on the edited file


def test_runner_passes_model_flag_when_pinned():
    seen = {}

    def fake_cli(args, cwd, env, timeout):
        seen["args"] = args
        (Path(cwd) / "poem.txt").write_text("p", encoding="utf-8")
        return json.dumps({"result": "done"})

    case = _case().model_copy(update={"model": "sonnet"})
    _, _, reader = build_trio()
    ClaudeCodeRunner(run_cli=fake_cli).run(case, reader)
    assert "--model" in seen["args"]
    assert seen["args"][seen["args"].index("--model") + 1] == "sonnet"


def test_runner_omits_model_flag_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MODEL", raising=False)

    def fake_cli(args, cwd, env, timeout):
        assert "--model" not in args
        (Path(cwd) / "poem.txt").write_text("p", encoding="utf-8")
        return json.dumps({"result": "done"})

    _, _, reader = build_trio()
    ClaudeCodeRunner(run_cli=fake_cli).run(_case(), reader)


def test_runner_uses_env_model_when_case_unset(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "haiku")
    seen = {}

    def fake_cli(args, cwd, env, timeout):
        seen["args"] = args
        (Path(cwd) / "poem.txt").write_text("p", encoding="utf-8")
        return json.dumps({"result": "done"})

    _, _, reader = build_trio()
    ClaudeCodeRunner(run_cli=fake_cli).run(_case(), reader)  # constructed after setenv
    assert seen["args"][seen["args"].index("--model") + 1] == "haiku"


def test_case_model_overrides_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "haiku")
    seen = {}

    def fake_cli(args, cwd, env, timeout):
        seen["args"] = args
        (Path(cwd) / "poem.txt").write_text("p", encoding="utf-8")
        return json.dumps({"result": "done"})

    case = _case().model_copy(update={"model": "opus"})
    _, _, reader = build_trio()
    ClaudeCodeRunner(run_cli=fake_cli).run(case, reader)
    assert seen["args"][seen["args"].index("--model") + 1] == "opus"  # case pin wins


def test_serves_model_rejects_provider_prefixed():
    assert ClaudeCodeRunner.serves_model("sonnet") is True
    assert ClaudeCodeRunner.serves_model("openai/gpt-4o-mini") is False


def test_default_run_cli_surfaces_stdout_on_failure(monkeypatch):
    from types import SimpleNamespace

    from knowledge.evals import claude_code as cc

    monkeypatch.setattr(cc, "_claude_path", lambda: "claude")
    # CLI failure with the reason on stdout and nothing on stderr.
    monkeypatch.setattr(
        cc.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="model may not exist", stderr=""),
    )
    with pytest.raises(RuntimeError) as ei:
        cc._default_run_cli(["-p", "hi"], Path("."), {}, 5)
    assert "model may not exist" in str(ei.value)  # stdout surfaced, not swallowed


def test_judge_parses_overall_score():
    raw = json.dumps({"result": '{"per_item": {"on_topic": 1.0}, "overall": 0.83}'})

    def fake_cli(args, cwd, env, timeout):
        return raw

    rubric = Rubric(id="r", items=[RubricItem(id="on_topic", criterion="about the sea")])
    judge = ClaudeCodeJudge(run_cli=fake_cli)
    result = judge(rubric, EvalContext(case_id="c", output="some poem"))
    assert result.overall == 0.83
    assert result.per_item == {"on_topic": 1.0}
    assert result.raw_response == raw  # raw response captured for the transcript
