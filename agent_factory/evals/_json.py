"""Tolerant JSON extraction from LLM output (```fences + prose-embedded values).

Model responses that "should be JSON only" routinely arrive wrapped in ```fences or
padded with prose. These helpers strip the common wrappers, then fall back to a
balanced-brace/bracket scan for the first JSON value embedded in the text.
"""

from __future__ import annotations

import json
import re

_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_-]*\n?")
_FENCE_CLOSE = re.compile(r"\n?```$")


def _strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = _FENCE_OPEN.sub("", s)
        s = _FENCE_CLOSE.sub("", s).strip()
    return s


def extract_json(text: str, *, allow_array: bool = False):
    """Parse the first top-level JSON value out of model text; ``None`` on failure.

    Strips ```fences, tries ``json.loads``, then scans for the first balanced ``{...}``
    (or ``[...]`` when ``allow_array``) embedded in surrounding prose.
    """
    s = _strip_fences(text)
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    opens = "{[" if allow_array else "{"
    positions = [p for p in (s.find(c) for c in opens) if p != -1]
    if not positions:
        return None
    start = min(positions)
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for i in range(start, len(s)):
        if s[i] == open_ch:
            depth += 1
        elif s[i] == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    return None
    return None


def extract_json_object(text: str) -> dict:
    """The first balanced JSON object from model text; ``{}`` on any failure or non-object."""
    obj = extract_json(text)
    return obj if isinstance(obj, dict) else {}
