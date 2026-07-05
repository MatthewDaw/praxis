"""Run the eval's model calls through the real `claude` CLI on the logged-in SUBSCRIPTION.

Mirrors Praxis's eval runner (../praxis knowledge/evals/claude_code.py): drive the `claude`
binary in headless `-p` mode and SCRUB ``ANTHROPIC_API_KEY`` from the subprocess env so the CLI
bills the logged-in subscription credential, not an API key. Returns a ``Complete = (prompt)
-> text`` that plugs into ``planner.produce_candidate`` / ``llm_evaluator.make_llm_evaluator``
exactly like the Anthropic backend — so the eval runs with no API key.

All tools are disallowed: the eval's prompts carry everything inline (the PRD, the candidate,
the golden), so the model needs no file/web/bash access — it's a pure text completion.

The CLI invocation is injected (``run_cli``) so tests stay offline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable

#: A CLI runner: (args, stdin_prompt, env, timeout) -> stdout string. The prompt is passed on
#: STDIN, not as a CLI arg — Windows caps a command line at ~32KB and the eval prompts embed
#: the full PRD (~45KB), so an arg-passed prompt fails with WinError 206.
CliRunner = Callable[[list[str], str, dict, int], str]

#: Everything is inline in the prompt, so the model gets no tools (pure text in/out).
_DISALLOWED_TOOLS = ["Bash", "WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob", "Grep"]


def _claude_path() -> str:
    path = shutil.which("claude")
    if not path:  # pragma: no cover - environment-dependent
        raise RuntimeError("the `claude` CLI is not on PATH; install Claude Code")
    return path


def _subscription_env() -> dict:
    """Env with ANTHROPIC_API_KEY removed so the CLI bills the subscription, not an API key."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _default_run_cli(args: list[str], stdin: str, env: dict, timeout: int) -> str:  # pragma: no cover - real CLI
    proc = subprocess.run(
        [_claude_path(), *args], input=stdin, env=env, capture_output=True, text=True,
        timeout=timeout, encoding="utf-8",
    )
    if proc.returncode != 0:
        # The CLI reports some failures (e.g. an invalid --model) on stdout, not stderr.
        detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    return proc.stdout


def _result_text(stdout: str) -> str:
    """Pull the assistant's final text out of `claude --output-format json` stdout."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    return str(data.get("result", "")).strip() if isinstance(data, dict) else stdout.strip()


def make_claude_cli_complete(
    *, model: str | None = None, timeout: int = 600, run_cli: CliRunner | None = None
) -> Callable[[str], str]:
    """A :data:`Complete` backed by the headless `claude` CLI on the subscription.

    ``model`` defaults to ``CLAUDE_CODE_MODEL`` then the CLI's own default. ``run_cli`` is
    injected for offline tests; the default shells out to the real binary.
    """
    runner = run_cli or _default_run_cli
    model = model or os.getenv("CLAUDE_CODE_MODEL")

    def complete(prompt: str) -> str:
        # The prompt goes on STDIN (see CliRunner) — `claude -p` with no positional reads it.
        args = [
            "-p",
            "--output-format", "json",
            "--disallowedTools", *_DISALLOWED_TOOLS,
            "--permission-mode", "bypassPermissions",
        ]
        if model:
            args += ["--model", model]
        return _result_text(runner(args, prompt, _subscription_env(), timeout))

    return complete
