"""Unit tests for the claude-CLI Complete backend (evals/plan_repro/claude_cli.py).

No real CLI: a fake `run_cli` is injected. Covers envelope parsing, prompt-on-stdin,
API-key scrubbing (subscription billing), the flag set, and the model override.
"""

import json

from evals.plan_repro.claude_cli import make_claude_cli_complete


def test_parses_result_field_from_json_envelope():
    def fake(args, stdin, env, timeout):
        return json.dumps({"result": '{"status":"covered"}', "usage": {}})

    complete = make_claude_cli_complete(run_cli=fake)
    assert complete("hello") == '{"status":"covered"}'


def test_non_json_stdout_falls_back_to_raw():
    complete = make_claude_cli_complete(run_cli=lambda a, s, e, t: "plain text out")
    assert complete("x") == "plain text out"


def test_prompt_is_passed_on_stdin_not_as_arg():
    seen = {}

    def fake(args, stdin, env, timeout):
        seen["args"] = args
        seen["stdin"] = stdin
        return json.dumps({"result": "ok"})

    make_claude_cli_complete(run_cli=fake)("a very long prompt with the whole PRD inline")
    # The prompt rides stdin (Windows 32KB arg limit), so it must NOT appear in argv.
    assert seen["stdin"] == "a very long prompt with the whole PRD inline"
    assert "a very long prompt with the whole PRD inline" not in seen["args"]
    assert seen["args"][0] == "-p"  # print mode, no positional prompt


def test_headless_flags_and_no_tools():
    seen = {}

    def fake(args, stdin, env, timeout):
        seen["args"] = args
        return json.dumps({"result": "ok"})

    make_claude_cli_complete(run_cli=fake)("a prompt")
    args = seen["args"]
    assert "--output-format" in args and "json" in args
    assert "--disallowedTools" in args and "Bash" in args
    assert "--permission-mode" in args and "bypassPermissions" in args


def test_api_key_is_scrubbed_for_subscription_billing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-removed")
    seen = {}

    def fake(args, stdin, env, timeout):
        seen["env"] = env
        return json.dumps({"result": "ok"})

    make_claude_cli_complete(run_cli=fake)("x")
    assert "ANTHROPIC_API_KEY" not in seen["env"]  # billed to the subscription, not the key


def test_model_override_is_passed(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MODEL", raising=False)
    seen = {}

    def fake(args, stdin, env, timeout):
        seen["args"] = args
        return json.dumps({"result": "ok"})

    make_claude_cli_complete(run_cli=fake, model="claude-sonnet-4-6")("x")
    assert "--model" in seen["args"] and "claude-sonnet-4-6" in seen["args"]


def test_no_model_flag_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_MODEL", raising=False)
    seen = {}

    def fake(args, stdin, env, timeout):
        seen["args"] = args
        return json.dumps({"result": "ok"})

    make_claude_cli_complete(run_cli=fake)("x")
    assert "--model" not in seen["args"]
