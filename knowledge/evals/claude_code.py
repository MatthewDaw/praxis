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

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from knowledge.evals.eval_def import (
    Artifact,
    EvalContext,
    JudgeResult,
    Rubric,
    align_per_item,
    rubric_score_schema,
    weighted_overall,
)
from knowledge.observability import tracing

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
        # The CLI reports some failures (e.g. an invalid --model) on stdout, not
        # stderr — fall back to stdout so the reason isn't swallowed.
        detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
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


def _claude_usage(stdout: str) -> dict:
    """Pull cost / token usage / turns out of `claude --output-format json` stdout."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage") or {}
    return {
        "cost_usd": data.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "num_turns": data.get("num_turns"),
    }


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


def _hash_tree(workdir: Path) -> dict[str, str]:
    """Map every non-dotfile in the box to a hash of its bytes (box-relative posix).

    Hashing raw bytes (not decoded text) means binary and unreadable files diff
    correctly. Dotfile paths (``.git`` etc.) are skipped, matching the sweep.
    """
    tree: dict[str, str] = {}
    for path in sorted(workdir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workdir)
        if any(seg.startswith(".") for seg in rel.parts):
            continue
        try:
            tree[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return tree


def _diff_artifacts(start: dict[str, str], end: dict[str, str]) -> list[Artifact]:
    """Files the agent produced: created (new) or modified (hash changed)."""
    artifacts: list[Artifact] = []
    for path in sorted(end):
        if path not in start:
            artifacts.append(Artifact(path=path, status="created"))
        elif start[path] != end[path]:
            artifacts.append(Artifact(path=path, status="modified"))
    return artifacts


def mount_fixtures(case, workdir: Path) -> int:
    """Copy ``<case.source_dir>/fixtures/**`` into ``workdir`` (structure preserved).

    Returns the number of files copied. A no-op when the case has no
    ``source_dir`` or no ``fixtures/`` subdir.
    """
    if not getattr(case, "source_dir", None):
        return 0
    fixtures = Path(case.source_dir) / "fixtures"
    if not fixtures.is_dir():
        return 0
    copied = 0
    for src in sorted(fixtures.rglob("*")):
        if not src.is_file():
            continue
        dest = workdir / src.relative_to(fixtures)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


class ClaudeCodeRunner:
    """Run a case's seed prompt through the real headless Claude Code, boxed.

    ``output_file`` is the artifact the agent is told to write; its contents
    become the graded output. Falls back to the assistant's final text if the
    file is absent.
    """

    # A real working dir: mounts fixtures (sandbox) AND grades the files the agent
    # writes (file_io). file_io is also offered by the structured single-shot runner,
    # so file-writing cases without a fixture run on both.
    provides = frozenset({"sandbox", "file_io"})

    @staticmethod
    def serves_model(model: str) -> bool:
        """The Claude CLI takes aliases (sonnet/opus) or claude-* names — never a
        provider-prefixed id, so a ``/`` means it's another backend's model."""
        return "/" not in model

    def __init__(
        self,
        output_file: str = "poem.txt",
        run_cli: CliRunner | None = None,
        timeout: int = 240,
        model: str | None = None,
    ) -> None:
        self.output_file = output_file
        self.run_cli = run_cli or _default_run_cli
        self.timeout = timeout
        # Default model: explicit arg, else CLAUDE_CODE_MODEL, else None (= the
        # `claude` CLI's own default). Mirrors OPENROUTER_MODEL for OpenRouter.
        self.model = model or os.getenv("CLAUDE_CODE_MODEL")

    def run(self, case, reader) -> EvalContext:
        # The knowledge graph is a data object — read it and inject it into the
        # session's system prompt. Nothing is written to disk as a graph file.
        knowledge = reader.read(case.seed_prompt)

        with tempfile.TemporaryDirectory(prefix="praxis-box-") as box:
            workdir = Path(box)
            # Mount the case's start state into the box. Two conventions are
            # supported: a fixtures/ subdir (mount_fixtures) and a whole fixture/
            # dir recorded on case.fixture_path (Monica's cases).
            mount_fixtures(case, workdir)
            if getattr(case, "fixture_path", None):
                shutil.copytree(case.fixture_path, workdir, dirs_exist_ok=True)
            start_tree = _hash_tree(workdir)  # snapshot the mounted start state
            args = ["-p", case.seed_prompt, "--output-format", "json"]
            model = getattr(case, "model", None) or self.model  # case pin > env/default
            if model:
                args += ["--model", model]
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
            with tracing.llm_span(
                "claude_code.agent",
                kind="AGENT",
                model="claude-code",
                input_value=case.seed_prompt,
            ) as span:
                stdout = self.run_cli(args, workdir, _subscription_env(), self.timeout)
                out_name = getattr(case, "output_file", None) or self.output_file
                output, source = self._collect_output(workdir, stdout, out_name)
                artifacts = _diff_artifacts(start_tree, _hash_tree(workdir))
                usage = _claude_usage(stdout)
                tracing.record_output(
                    span,
                    output=output,
                    prompt_tokens=usage.get("input_tokens"),
                    completion_tokens=usage.get("output_tokens"),
                    cost_usd=usage.get("cost_usd"),
                    **{
                        "praxis.case_id": case.id,
                        "praxis.output_source": source,
                        "praxis.num_turns": usage.get("num_turns"),
                    },
                )
                return EvalContext(
                    case_id=case.id,
                    output=output,
                    checkout_path=str(workdir),
                    raw_response=stdout,
                    output_source=source,
                    injected_knowledge=knowledge,
                    artifacts=artifacts,
                )

    def _collect_output(self, workdir: Path, stdout: str, output_file: str) -> tuple[str, str]:
        """The graded output plus which artifact it came from.

        ``output_file`` (the case's, falling back to the runner default) is graded
        if present, else everything the agent wrote into the box, else the
        assistant's final text. Reading the box's files keeps the runner
        case-agnostic — a poem case yields poem.txt, a code case yields the source
        files — so one runner drives every case. The source tag is recorded on the
        transcript so a graded result is traceable to its origin.
        """
        preferred = workdir / output_file
        if preferred.exists():
            return preferred.read_text(encoding="utf-8"), "named_file"

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

        if parts:
            return "\n\n".join(parts), "box_sweep"
        return _result_text(stdout), "final_text"


class ClaudeCodeJudge:
    """Rubric judge that also runs through real Claude Code (subscription).

    Runs in a fresh temp dir with no CLAUDE.md so the injected knowledge can't
    bias the grade. Returns a :class:`JudgeResult` (overall score in [0, 1],
    per-item scores, and the raw response for the transcript).
    """

    def __init__(self, run_cli: CliRunner | None = None, timeout: int = 120) -> None:
        self.run_cli = run_cli or _default_run_cli
        self.timeout = timeout

    def __call__(self, rubric: Rubric, ctx: EvalContext) -> JudgeResult:
        items = "\n".join(f"- {it.id}: {it.criterion}" for it in rubric.items)
        prompt = (
            "You are grading an artifact against a rubric. Score each criterion from "
            "0.0 to 1.0, keyed by its exact id (the token before its colon). Do not "
            "compute an overall — the harness applies the weights.\n\n"
            f"RUBRIC:\n{items}\n\n"
            f"ARTIFACT:\n{ctx.output}\n"
        )
        # Constrained decoding: the schema-conforming object lands in the envelope's
        # `structured_output` field (NOT `result`, which stays prose).
        schema = json.dumps(rubric_score_schema(rubric))
        with tracing.llm_span(
            "claude_code.judge", kind="LLM", model="claude-code", input_value=prompt
        ) as span:
            with tempfile.TemporaryDirectory() as tmp:
                args = [
                    "-p",
                    prompt,
                    "--output-format",
                    "json",
                    "--json-schema",
                    schema,
                    "--disallowedTools",
                    *_ALLOWED_TOOLS,
                    *_DISALLOWED_TOOLS,
                ]
                stdout = self.run_cli(args, Path(tmp), _subscription_env(), self.timeout)

            structured = (json.loads(stdout) if stdout.strip() else {}).get("structured_output") or {}
            per_item = align_per_item(rubric, structured.get("per_item"))
            overall = weighted_overall(rubric, per_item)
            usage = _claude_usage(stdout)
            tracing.record_output(
                span,
                output=stdout,
                prompt_tokens=usage.get("input_tokens"),
                completion_tokens=usage.get("output_tokens"),
                cost_usd=usage.get("cost_usd"),
                **{"praxis.case_id": ctx.case_id, "praxis.overall": overall},
            )
            return JudgeResult(overall=overall, per_item=per_item, raw_response=stdout)
