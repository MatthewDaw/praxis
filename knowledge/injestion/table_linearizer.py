"""Deterministic tabular / templated input -> one atomic fact per row.

Loss point A in the tabular-ingestion-integrity proposal: a prose-oriented
distillation prompt (or a sentence splitter) collapses rows that share a
sentence shape, so an N-row table silently becomes fewer than N facts. The fix
is to *not* ask a model to enumerate rows at all: detect structured input and
linearize it deterministically, emitting one self-contained fact per row.

This module is intentionally **liftable** — its public surface is a single pure
function (:func:`linearize_table`: ``str`` in -> ``list[str]`` of atomic facts
out) with no dependency on any Praxis private internal. That lets it be promoted
into Praxis core, or copied to the agent-factory port, *wholesale* — so the two
copies cannot drift and shift behavior under us at migration time.

The folded text is the point: a row ``daily_prompt | required | yes`` becomes
``"For the daily_prompt field, required = true"`` rather than ``"required:
yes"``. Folding the row/column identity into the sentence makes each fact
lexically distinct and self-contained, so the embedder/judge downstream is far
less likely to collapse sibling rows (defense in depth with the dedup
slot-guard, which remains the actual guarantee).
"""

from __future__ import annotations

import re

# A markdown separator row under the header, e.g. ``|---|:--:|---|`` or
# ``--- | --- | ---``. Only dashes, pipes, colons and whitespace; at least one
# dash so a real divider isn't confused with a data row of empty cells.
_MD_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

# A ``key: value`` line, e.g. ``Athlete: completes daily rep``. The key is a
# short label (no internal colon) and the value is non-empty. Leading list
# bullets (``*``, ``-``, ``•``) and numbering are tolerated and stripped.
_KEY_VALUE_RE = re.compile(r"^[\s*\-•\d.)]*([^:|]{1,80}?)\s*:\s+(\S.*)$")

# Boolean-ish cell values we normalize so the folded fact reads as a statement
# (``required = true``) rather than echoing the raw cell (``required: yes``).
_TRUE_TOKENS = {"yes", "y", "true", "required", "x", "✓", "✔"}
_FALSE_TOKENS = {"no", "n", "false", "optional", "—", "-"}


def linearize_table(text: str) -> list[str]:
    """Linearize detected tabular/templated ``text`` into atomic facts.

    Returns one self-contained fact per row, with the row/column identity folded
    into the sentence. Returns an empty list when ``text`` is not recognized as
    tabular — callers use that as the "fall through to prose handling" signal.
    """
    facts = _linearize_markdown(text)
    if facts:
        return facts
    facts = _linearize_delimited(text)
    if facts:
        return facts
    return _linearize_key_value(text)


def _split_row(line: str) -> list[str]:
    """Split a delimited row into trimmed cells, dropping the outer pipes."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    delimiter = "|" if "|" in stripped else ","
    return [cell.strip() for cell in stripped.split(delimiter)]


def _looks_boolean(value: str) -> str | None:
    """Map a boolean-ish cell to ``"true"``/``"false"``; ``None`` otherwise."""
    token = value.strip().lower()
    if token in _TRUE_TOKENS:
        return "true"
    if token in _FALSE_TOKENS:
        return "false"
    return None


def _fold_cell(header: str, value: str) -> str:
    """Fold one (column, value) pair into a clause, normalizing booleans."""
    boolean = _looks_boolean(value)
    if boolean is not None:
        return f"{header} = {boolean}"
    return f"{header} is {value}"


def _fold_row(headers: list[str], cells: list[str]) -> str | None:
    """Fold a header/cell row into a self-contained sentence.

    The first column is treated as the row's subject ("For the <subject>
    <header0>, ...") and remaining columns become folded clauses. Returns
    ``None`` for a degenerate row (no subject or no attributes).
    """
    if not cells or not cells[0]:
        return None
    width = min(len(headers), len(cells))
    if width < 2:
        return None
    subject_label, subject = headers[0], cells[0]
    clauses = [
        _fold_cell(headers[i], cells[i])
        for i in range(1, width)
        if cells[i]  # skip empty trailing cells
    ]
    if not clauses:
        return None
    return f"For the {subject} {subject_label}, " + ", ".join(clauses)


def _linearize_markdown(text: str) -> list[str]:
    """Linearize a GitHub-style markdown table (header, ``---`` row, then data)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sep_index = next(
        (
            i
            for i, ln in enumerate(lines)
            if i > 0 and "|" in lines[i - 1] and _MD_SEPARATOR_RE.match(ln) and "-" in ln
        ),
        None,
    )
    if sep_index is None:
        return []
    headers = _split_row(lines[sep_index - 1])
    facts: list[str] = []
    for line in lines[sep_index + 1 :]:
        if "|" not in line:
            break  # table ended; trailing prose is not ours to linearize
        fact = _fold_row(headers, _split_row(line))
        if fact:
            facts.append(fact)
    return facts


def _linearize_delimited(text: str) -> list[str]:
    """Linearize headerless CSV-ish rows (a consistent delimiter, no ``---``).

    Treats the first row as a header only when every subsequent row has the same
    column count; this keeps ordinary prose (which has wildly varying "comma
    counts") from being misread as a table.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:  # need a header + at least two data rows to be confident
        return []
    delimiter = "|" if all("|" in ln for ln in lines) else ","
    if not all(delimiter in ln for ln in lines):
        return []
    rows = [[c.strip() for c in ln.split(delimiter)] for ln in lines]
    width = len(rows[0])
    if width < 2 or any(len(r) != width for r in rows[1:]):
        return []
    headers = rows[0]
    facts = [fact for r in rows[1:] if (fact := _fold_row(headers, r))]
    return facts


def _linearize_key_value(text: str) -> list[str]:
    """Linearize a repeated ``key: value`` block (e.g. a roles list).

    Requires at least two ``key: value`` lines so a single stray colon in prose
    does not trip the branch. Each line becomes ``"<key>: <value>"`` with the
    key preserved as the subject so sibling rows stay lexically distinct.
    """
    pairs: list[tuple[str, str]] = []
    saw_non_kv = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _KEY_VALUE_RE.match(line)
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            pairs.append((key, value))
        else:
            saw_non_kv += 1
    # Be conservative: a couple of key:value lines amid mostly prose is prose.
    if len(pairs) < 2 or saw_non_kv > len(pairs):
        return []
    return [f"{key}: {value}" for key, value in pairs]
