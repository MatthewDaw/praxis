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
import urllib.request
from typing import Callable

from knowledge.evals.eval_def import EvalContext, JudgeResult, Rubric

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"

# A POST seam: (url, payload, headers, timeout) -> raw response body (str).
Poster = Callable[[str, dict, dict, int], str]


def _default_post(url: str, payload: dict, headers: dict, timeout: int) -> str:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed URL
        return resp.read().decode("utf-8")


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
    ) -> tuple[str, str]:
        """Like :meth:`complete`, but also return the raw response body.

        The raw body carries usage/model/id metadata, so the transcript keeps it
        verbatim — the parallel to ``ClaudeCodeRunner``'s ``raw_response``.
        """
        if not self.api_key:
            raise RuntimeError("set OPENROUTER_API_KEY to use the OpenRouter backend")
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,  # greedy by default for determinism
            "max_tokens": max_tokens,
        }
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

        raw = self.post(OPENROUTER_URL, payload, headers, self.timeout)
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"], raw


class OpenRouterRunner:
    """Cheap single-shot runner: inject the graph as system prompt, one completion.

    NOT an agent — no tools/fixtures/sandbox. The graph (from the reader) becomes
    the system prompt; ``seed_prompt`` is the user turn; the reply text is the
    graded output. Use for cheap iteration; use ``ClaudeCodeRunner`` for fidelity.
    """

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
            messages, temperature=self.temperature, max_tokens=self.max_tokens
        )
        return EvalContext(
            case_id=case.id,
            output=output,
            raw_response=raw,
            output_source="completion",  # single-shot reply, no tools/files
            injected_knowledge=knowledge,
        )


class OpenRouterJudge:
    """Rubric judge via a cheap OpenRouter model. Returns a :class:`JudgeResult`."""

    def __init__(self, client: OpenRouterClient | None = None) -> None:
        self.client = client or OpenRouterClient()

    def __call__(self, rubric: Rubric, ctx: EvalContext) -> JudgeResult:
        items = "\n".join(
            f"- ({it.weight}) {it.id}: {it.criterion}" for it in rubric.items
        )
        prompt = (
            "You are grading an artifact against a rubric. Score each criterion "
            "from 0.0 to 1.0, then return ONLY a JSON object of the form "
            '{"per_item": {"<id>": <score>}, "overall": <weighted average 0..1>}. '
            "No prose.\n\n"
            f"RUBRIC:\n{items}\n\nARTIFACT:\n{ctx.output}\n"
        )
        text, raw = self.client.complete_raw(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=512
        )
        parsed = _extract_json(text)
        overall = max(0.0, min(1.0, float(parsed.get("overall", 0.0))))
        per_item = {k: float(v) for k, v in (parsed.get("per_item") or {}).items()}
        return JudgeResult(overall=overall, per_item=per_item, raw_response=raw)


def openrouter_llm(client: OpenRouterClient | None = None) -> Callable[[str], str]:
    """Adapt OpenRouter to the PromptIngestor ``llm`` callable (prompt -> text)."""
    client = client or OpenRouterClient()

    def _llm(prompt: str) -> str:
        return client.complete(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=512
        )

    return _llm
