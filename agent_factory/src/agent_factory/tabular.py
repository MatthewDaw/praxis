"""Deterministic tabular / templated input linearizer — the H6 ingestion shim.

Praxis distillation silently under-emits on tabular input (rows that share a
sentence shape) and the deduper over-merges siblings, so distinct rows vanish into
the ``rejected`` pile (gap H6). This module converts tables and ``key: value`` blocks
into **atomic, lexically-distinct** fact sentences BEFORE they reach Praxis, folding
each row's and column's identity into the text so siblings stay distinct.

Scope and limits:
- This is the *local* half of the H6 fix. It reduces distillation loss (loss point A).
- It cannot fix the server-side over-merge (loss point B) — pair it with the
  rejected-pile audit in the knowledge-port policy (``docs/af-memory-policy.md``), which is the safety net.
- Detection is deliberately conservative: prefer leaving prose alone (returned as
  ``residual_prose``) over false-positiving a list into fragmented facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A markdown separator row, e.g. ``| --- | :--: |`` (dashes, optional colons/pipes).
_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$")
# A conservative ``key: value`` / ``key = value`` line; key is short and identifier-ish.
_KV_RE = re.compile(r"^\s*([A-Za-z0-9][\w .\-/]{0,40}?)\s*[:=]\s*(\S.*?)\s*$")


@dataclass
class LinearizeResult:
    """Facts extracted from tabular regions, plus the prose left untouched."""

    facts: list[str] = field(default_factory=list)
    residual_prose: str = ""


def _split_md_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_pipe_row(line: str) -> bool:
    return "|" in line and line.strip() != ""


def _facts_from_table(header: list[str], rows: list[list[str]]) -> list[str]:
    facts: list[str] = []
    subject_col = header[0] if header else "item"
    for row in rows:
        if not row or not row[0].strip():
            continue
        key = row[0].strip()
        if len(header) <= 1:
            facts.append(f'{subject_col} includes "{key}".')
            continue
        # One atomic fact per (row, non-subject column) — maximal distinctness so the
        # embedder/judge cannot collapse sibling rows.
        emitted = False
        for col in range(1, min(len(header), len(row))):
            attr = header[col].strip()
            val = row[col].strip()
            if not attr or not val:
                continue
            facts.append(f'For {subject_col} "{key}", {attr} is {val}.')
            emitted = True
        if not emitted:
            facts.append(f'{subject_col} includes "{key}".')
    return facts


def _try_markdown_table(lines: list[str], i: int) -> tuple[int, list[str]]:
    """If a markdown table starts at ``lines[i]``, return (lines_consumed, facts)."""
    n = len(lines)
    if i + 2 >= n + 0:  # need at least header + separator + one row
        pass
    if not _is_pipe_row(lines[i]):
        return 0, []
    if i + 1 >= n or not _SEP_RE.match(lines[i + 1]):
        return 0, []
    header = _split_md_row(lines[i])
    j = i + 2
    rows: list[list[str]] = []
    while j < n and _is_pipe_row(lines[j]) and not _SEP_RE.match(lines[j]):
        rows.append(_split_md_row(lines[j]))
        j += 1
    if not rows:
        return 0, []
    return (j - i), _facts_from_table(header, rows)


def _try_kv_block(lines: list[str], i: int) -> tuple[int, list[str]]:
    """If >=2 consecutive ``key: value`` lines start at ``lines[i]``, linearize them."""
    n = len(lines)
    j = i
    pairs: list[tuple[str, str]] = []
    while j < n:
        m = _KV_RE.match(lines[j])
        if not m:
            break
        pairs.append((m.group(1).strip(), m.group(2).strip()))
        j += 1
    if len(pairs) < 2:  # a single ``key: value`` line is likely prose, not a block
        return 0, []
    facts = [f"The {key} is {value}." for key, value in pairs]
    return (j - i), facts


def linearize(text: str) -> LinearizeResult:
    """Linearize markdown tables and ``key: value`` blocks into atomic facts.

    Returns the extracted ``facts`` (one atomic, distinct sentence per row/cell) and
    the ``residual_prose`` (everything that was not tabular), so the caller can route
    facts to ``praxis_add_insight`` and prose to ``praxis_ingest``.
    """
    lines = text.splitlines()
    facts: list[str] = []
    residual: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        consumed, tbl_facts = _try_markdown_table(lines, i)
        if consumed:
            facts.extend(tbl_facts)
            i += consumed
            continue
        consumed, kv_facts = _try_kv_block(lines, i)
        if consumed:
            facts.extend(kv_facts)
            i += consumed
            continue
        residual.append(lines[i])
        i += 1
    return LinearizeResult(facts=facts, residual_prose="\n".join(residual).strip())
