"""Cheap LLM backend via OpenRouter (runner + judge + ingestor LLM).

OpenRouter exposes an OpenAI-compatible chat-completions API, so this is a thin
HTTP client over the stdlib (no extra deps). It is a **single-shot, non-agentic**
backend: one chat completion, the reply text is the output. No tools, no
fixtures, no sandbox — use it for cheap, fast harness iteration and grading, not
production fidelity (that is ``ClaudeCodeRunner``).

Auth/config via env:
- ``OPENROUTER_API_KEY`` (required to actually call out)
- ``OPENROUTER_MODEL`` (default below; pick any cheap model, incl. ``:free`` ones)

The HTTP POST is injected (``post``) so harness tests run fully offline.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
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

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"

# A POST seam: (url, payload, headers, timeout) -> raw response body (str).
Poster = Callable[[str, dict, dict, int], str]


def _default_post(url: str, payload: dict, headers: dict, timeout: int) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed URL
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # The 4xx body names the actual cause (e.g. an invalid model id); a bare
        # "HTTP Error 400" would hide it. Surface it.
        body = e.read().decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"OpenRouter HTTP {e.code}: {body}") from e


def _extract_json(text: str) -> dict:
    """Best-effort parse of a JSON object out of model text (tolerates fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


class OpenRouterClient:
    """Minimal OpenRouter chat-completions client."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        post: Poster | None = None,
        timeout: int = 120,
    ) -> None:
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.api_key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY")
        self.post = post or _default_post
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> str:
        """Return the assistant message text for one chat completion."""
        text, _ = self.complete_raw(
            messages, temperature=temperature, max_tokens=max_tokens, model=model
        )
        return text

    def complete_raw(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
        response_format: dict | None = None,
    ) -> tuple[str, str]:
        """Like :meth:`complete`, but also return the raw response body.

        The raw body carries usage/model/id metadata, so the transcript keeps it
        verbatim — the parallel to ``ClaudeCodeRunner``'s ``raw_response``.
        ``response_format`` (e.g. a ``json_schema`` block) is passed through for
        structured outputs; providers that support it enforce server-side.
        """
        if not self.api_key:
            raise RuntimeError("set OPENROUTER_API_KEY to use the OpenRouter backend")
        model_name = model or self.model
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,  # greedy by default for determinism
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Optional but recommended by OpenRouter for attribution.
        referer = os.getenv("OPENROUTER_HTTP_REFERER")
        title = os.getenv("OPENROUTER_TITLE", "praxis-evals")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title

        with tracing.llm_span("openrouter.chat", model=model_name, input_value=messages) as span:
            raw = self.post(OPENROUTER_URL, payload, headers, self.timeout)
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            tracing.record_output(
                span,
                output=content,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
            return content, raw


class OpenRouterRunner:
    """Cheap single-shot runner: inject the graph as system prompt, one completion.

    NOT an agent — no tools/fixtures/sandbox. The graph (from the reader) becomes
    the system prompt; ``seed_prompt`` is the user turn; the reply text is the
    graded output. Use for cheap iteration; use ``ClaudeCodeRunner`` for fidelity.
    """

    # Single-shot text: no working dir, no file edits. Cases that need a sandbox
    # are skipped rather than graded on a reply this runner can't make faithful.
    provides = frozenset()

    @staticmethod
    def serves_model(model: str) -> bool:
        """OpenRouter ids are provider-prefixed (e.g. ``openai/gpt-4o-mini``); a
        bare alias like ``sonnet`` belongs to another backend."""
        return "/" in model

    def __init__(
        self,
        client: OpenRouterClient | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> None:
        self.client = client or OpenRouterClient()
        self.temperature = temperature
        self.max_tokens = max_tokens

    def run(self, case, reader) -> EvalContext:
        knowledge = reader.read(case.seed_prompt)
        messages: list[dict] = []
        if knowledge.strip():
            messages.append({"role": "system", "content": knowledge})
        messages.append({"role": "user", "content": case.seed_prompt})
        output, raw = self.client.complete_raw(
            messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            model=getattr(case, "model", None),  # case override; None => client default
        )
        return EvalContext(
            case_id=case.id,
            output=output,
            raw_response=raw,
            output_source="completion",  # single-shot reply, no tools/files
            injected_knowledge=knowledge,
        )


# The structured "file_changes" protocol: the model returns the files it would
# write as JSON, so artifact-grading checks work without a real box.
_FILE_CHANGES_SCHEMA = {
    "name": "file_changes",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "file_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "contents": {"type": "string"},
                    },
                    "required": ["path", "contents"],
                    "additionalProperties": False,
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["file_changes", "notes"],
        "additionalProperties": False,
    },
}

_FILE_OUTPUT_INSTRUCTION = (
    "Respond ONLY with the file(s) you would write to satisfy the task, as structured "
    "output: each file as a path and its FULL contents (no diffs, no ellipses, no "
    "markdown fences). Put any commentary in `notes`, never inside file contents."
)


def _parse_file_changes(content: str, case_id: str) -> dict[str, str]:
    """Parse the model's structured reply into ``{path: contents}``.

    A malformed reply is a *capability* failure (this model can't run the case
    faithfully), so it raises loudly rather than being mis-graded as a content fail.
    """
    try:
        data = json.loads(content)
        files = {c["path"]: c["contents"] for c in data["file_changes"]}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise RuntimeError(
            f"StructuredOpenRouterRunner: model did not return valid file_changes for "
            f"case {case_id!r} ({type(e).__name__}: {e}); content={content[:200]!r}"
        ) from e
    if not files:
        raise RuntimeError(
            f"StructuredOpenRouterRunner: model returned no file_changes for case {case_id!r}"
        )
    return files


def _select_structured_output(case, files: dict[str, str]) -> tuple[str, str]:
    """The graded output from the produced files (mirrors ClaudeCode's _collect_output)."""
    name = getattr(case, "output_file", None)
    if name and name in files:
        return files[name], "named_file"
    if len(files) == 1:
        return next(iter(files.values())), "single_file"
    parts = [f"# {p}\n{c}" for p, c in sorted(files.items())]
    return "\n\n".join(parts), "box_sweep"


class StructuredOpenRouterRunner:
    """Single-shot OpenRouter runner that produces gradeable FILE artifacts.

    Asks the model for structured ``file_changes`` output and parses it into a
    virtual file tree, so the same artifact checks (``output_file``, ``writes_file``)
    that drive a real ClaudeCode box also work here — no sandbox, and no chat-prose
    contamination (file contents come from JSON, not the reply).

    Output-side only: it does NOT mount fixtures, so it provides ``file_io`` (produce
    gradeable files) but not the fixture-mounting ``sandbox`` capability — fixture
    cases skip it. ``provides`` is runner-level; whether a given model emits valid
    JSON is checked at parse time (loud capability error), not via a model registry.
    """

    provides = frozenset({"file_io"})

    @staticmethod
    def serves_model(model: str) -> bool:
        # OpenRouter ids are provider-prefixed (e.g. openai/gpt-4o-mini); a bare
        # alias belongs to another backend. (Same rule as OpenRouterRunner.)
        return "/" in model

    def __init__(self, client: OpenRouterClient | None = None, max_tokens: int = 2048) -> None:
        self.client = client or OpenRouterClient()
        self.max_tokens = max_tokens

    def run(self, case, reader) -> EvalContext:
        knowledge = reader.read(case.seed_prompt)
        messages: list[dict] = []
        if knowledge.strip():
            messages.append({"role": "system", "content": knowledge})
        messages.append(
            {"role": "user", "content": f"{case.seed_prompt}\n\n{_FILE_OUTPUT_INSTRUCTION}"}
        )
        content, raw = self.client.complete_raw(
            messages,
            temperature=0.0,
            max_tokens=self.max_tokens,
            model=getattr(case, "model", None),
            response_format={"type": "json_schema", "json_schema": _FILE_CHANGES_SCHEMA},
        )
        files = _parse_file_changes(content, case.id)
        output, source = _select_structured_output(case, files)
        artifacts = [Artifact(path=p, status="created") for p in sorted(files)]
        return EvalContext(
            case_id=case.id,
            output=output,
            raw_response=raw,
            output_source=source,
            injected_knowledge=knowledge,
            artifacts=artifacts,
        )


class OpenRouterJudge:
    """Rubric judge via a cheap OpenRouter model. Returns a :class:`JudgeResult`.

    Forces a per-rubric json_schema so the model returns exactly the rubric's item
    ids as scores; ``align_per_item`` stays as a fallback for any model/provider that
    doesn't enforce the schema. The judge model resolves to ``OPENROUTER_JUDGE_MODEL``
    (so grading can use a different/stronger model than the runner), then the client
    default (``OPENROUTER_MODEL`` / built-in).
    """

    def __init__(self, client: OpenRouterClient | None = None, model: str | None = None) -> None:
        self.client = client or OpenRouterClient(
            model=model or os.getenv("OPENROUTER_JUDGE_MODEL")
        )

    def __call__(self, rubric: Rubric, ctx: EvalContext) -> JudgeResult:
        items = "\n".join(f"- {it.id}: {it.criterion}" for it in rubric.items)
        prompt = (
            "You are grading an artifact against a rubric. Score each criterion from "
            "0.0 to 1.0, keyed by its exact id (the token before its colon). Do not "
            "compute an overall — the harness applies the weights.\n\n"
            f"RUBRIC:\n{items}\n\nARTIFACT:\n{ctx.output}\n"
        )
        text, raw = self.client.complete_raw(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=512,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "rubric_scores",
                    "strict": True,
                    "schema": rubric_score_schema(rubric),
                },
            },
        )
        per_item = align_per_item(rubric, _extract_json(text).get("per_item"))
        return JudgeResult(
            overall=weighted_overall(rubric, per_item), per_item=per_item, raw_response=raw
        )


def openrouter_llm(client: OpenRouterClient | None = None) -> Callable[[str], str]:
    """Adapt OpenRouter to the PromptIngestor ``llm`` callable (prompt -> text)."""
    client = client or OpenRouterClient()

    def _llm(prompt: str) -> str:
        return client.complete(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=512
        )

    return _llm
