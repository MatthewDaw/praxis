"""Real Claude Code integration: runner + rubric judge.

Both drive the actual ``claude`` CLI (the same binary an interactive user runs)
via headless ``-p`` mode — this is the real engine, not a mock. Neither path
sends an API key: ``ANTHROPIC_API_KEY`` is scrubbed from the subprocess env so
the CLI uses the logged-in subscription credential.

The runner executes inside a **sealed box**: a throwaway temp dir it creates,
seeds with the (empty) start state, and runs in as ``cwd``. The knowledge graph
is injected into the session via ``--append-system-prompt`` (read from the graph
reader) rather than any file — the graph is a data object, never written to
disk. The toolset is restricted to file edits with Bash / web tools forbidden,
so the agent can't reach outside the box to find the answer.

The CLI invocation is injected (``run_cli``) so harness tests stay offline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from knowledge.evals.eval_def import EvalContext, Rubric

# Tools the boxed agent may use. Bash / WebSearch / WebFetch are explicitly
# denied so it can only produce the answer from its own knowledge + the
# injected graph, never fetch it from outside.
_ALLOWED_TOOLS = ["Read", "Write", "Edit"]
_DISALLOWED_TOOLS = ["Bash", "WebSearch", "WebFetch"]

# A CLI runner: (args, cwd, env, timeout) -> stdout string.
CliRunner = Callable[[list[str], Path, dict, int], str]


def _claude_path() -> str:
    path = shutil.which("claude")
    if not path:
        raise RuntimeError("the `claude` CLI is not on PATH; install Claude Code")
    return path


def _subscription_env() -> dict:
    """Env with ANTHROPIC_API_KEY removed so the CLI bills the subscription."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _default_run_cli(args: list[str], cwd: Path, env: dict, timeout: int) -> str:
    proc = subprocess.run(
        [_claude_path(), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _result_text(stdout: str) -> str:
    """Pull the assistant's final text out of `--output-format json` stdout."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    if isinstance(data, dict):
        return str(data.get("result", "")).strip()
    return stdout.strip()


def _extract_json(text: str) -> dict:
    """Best-effort parse of a JSON object out of model text (tolerates fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


class ClaudeCodeRunner:
    """Run a case's seed prompt through the real headless Claude Code, boxed.

    ``output_file`` is the artifact the agent is told to write; its contents
    become the graded output. Falls back to the assistant's final text if the
    file is absent.
    """

    def __init__(
        self,
        output_file: str = "poem.txt",
        run_cli: CliRunner | None = None,
        timeout: int = 240,
    ) -> None:
        self.output_file = output_file
        self.run_cli = run_cli or _default_run_cli
        self.timeout = timeout

    def run(self, case, reader) -> EvalContext:
        # The knowledge graph is a data object — read it and inject it into the
        # session's system prompt. Nothing is written to disk as a graph file.
        knowledge = reader.read(case.seed_prompt)

        with tempfile.TemporaryDirectory(prefix="praxis-box-") as box:
            workdir = Path(box)
            # Seed the box with the start state: an inline fixture dir copied
            # verbatim (empty for the toy case). Future code cases that set
            # start_commit would check that out here instead.
            if case.fixture_path:
                shutil.copytree(case.fixture_path, workdir, dirs_exist_ok=True)
            args = ["-p", case.seed_prompt, "--output-format", "json"]
            if knowledge.strip():
                args += ["--append-system-prompt", knowledge]
            args += [
                "--allowedTools",
                *_ALLOWED_TOOLS,
                "--disallowedTools",
                *_DISALLOWED_TOOLS,
                "--permission-mode",
                "bypassPermissions",
            ]
            stdout = self.run_cli(args, workdir, _subscription_env(), self.timeout)
            output = self._collect_output(workdir, stdout)
            return EvalContext(case_id=case.id, output=output, checkout_path=str(workdir))

    def _collect_output(self, workdir: Path, stdout: str) -> str:
        """The graded output: the named artifact if present, else everything the
        agent wrote into the box, else the assistant's final text.

        Reading the box's files keeps the runner case-agnostic — a poem case
        yields poem.txt, a code case yields the source files — so one runner
        drives every case.
        """
        preferred = workdir / self.output_file
        if preferred.exists():
            return preferred.read_text(encoding="utf-8")

        parts: list[str] = []
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            if any(seg.startswith(".") for seg in path.relative_to(workdir).parts):
                continue  # skip dotfiles / .git
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            parts.append(f"# {path.relative_to(workdir).as_posix()}\n{text}")

        return "\n\n".join(parts) if parts else _result_text(stdout)


class ClaudeCodeJudge:
    """Rubric judge that also runs through real Claude Code (subscription).

    Runs in a fresh temp dir with no CLAUDE.md so the injected knowledge can't
    bias the grade. Returns a score in [0, 1].
    """

    def __init__(self, run_cli: CliRunner | None = None, timeout: int = 120) -> None:
        self.run_cli = run_cli or _default_run_cli
        self.timeout = timeout

    def __call__(self, rubric: Rubric, ctx: EvalContext) -> float:
        items = "\n".join(f"- ({it.weight}) {it.id}: {it.criterion}" for it in rubric.items)
        prompt = (
            "You are grading an artifact against a rubric. Score each criterion "
            "from 0.0 to 1.0, then return ONLY a JSON object of the form "
            '{\"per_item\": {\"<id>\": <score>}, \"overall\": <weighted average 0..1>}. '
            "No prose.\n\n"
            f"RUBRIC:\n{items}\n\n"
            f"ARTIFACT:\n{ctx.output}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            args = [
                "-p",
                prompt,
                "--output-format",
                "json",
                "--disallowedTools",
                *_ALLOWED_TOOLS,
                *_DISALLOWED_TOOLS,
            ]
            stdout = self.run_cli(args, Path(tmp), _subscription_env(), self.timeout)

        parsed = _extract_json(_result_text(stdout))
        overall = float(parsed.get("overall", 0.0))
        return max(0.0, min(1.0, overall))
