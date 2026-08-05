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
import pathlib
import subprocess
import tempfile
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


#: Config dir handed to the judge, holding the credential and NOTHING else.
_JUDGE_CONFIG_DIR = pathlib.Path(tempfile.gettempdir()) / "af-judge-claude-config"


def _isolated_config_dir() -> pathlib.Path | None:
    """A config dir with the subscription credential but no plugins, or None.

    Installed plugins break headless `claude -p`: it exits 0 having printed
    NOTHING, so the judge sees an empty completion and every graded check is
    unscorable. Reproduced in both directions on the devbox -- with the real
    ~/.claude/plugins present the probe returns empty, and with that one
    directory removed the identical command returns its answer.

    The two obvious escapes are both wrong. `--bare` does skip plugins, but it
    skips credential resolution too and the CLI reports "Not logged in - Please
    run /login" (as does CLAUDE_CODE_SIMPLE=1); that error is what made this
    look like an auth problem for a long time when it never was. Overriding HOME
    works but drags along everything else keyed off it.

    CLAUDE_CONFIG_DIR points the CLI at a directory we control. The credential is
    SYMLINKED rather than copied, so the secret is never duplicated on disk and a
    re-login is picked up automatically.

    Returns None when there is no credential to link -- the caller then leaves the
    env untouched so behaviour is unchanged wherever this is not the problem
    (a plugin-free machine, or a Bedrock/Vertex credential).
    """
    source = pathlib.Path.home() / ".claude" / ".credentials.json"
    if not source.exists():
        return None
    try:
        _JUDGE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        link = _JUDGE_CONFIG_DIR / ".credentials.json"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(source)
    except OSError:
        return None  # never let config isolation be the thing that fails a run
    return _JUDGE_CONFIG_DIR


def _subscription_env() -> dict:
    """Env for the judge: subscription credential, no API key, no plugins.

    ANTHROPIC_API_KEY is removed so the CLI bills the logged-in subscription
    rather than metered API credits.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    config_dir = _isolated_config_dir()
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
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
