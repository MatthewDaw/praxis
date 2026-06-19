"""MVP graph reader: return the whole graph, every time.

``synthesis`` ignores the context and emits a single whole-graph
:class:`ReadRequest`, so the inherited ``read`` returns the full graph. The
:func:`as_claude_tool` adapter wraps ``read`` as a registered Claude tool
without leaking tool plumbing into the reader itself.
"""

from __future__ import annotations

from typing import Any

from knowledge.graph_reader.graph_reader_def import ReadRequest
from knowledge.graph_reader.parent_graph_reader import GraphReader


class WholeFileReader(GraphReader):
    """Returns the entire knowledge graph regardless of context."""

    def synthesis(self, context: str | None = None) -> list[ReadRequest]:
        return [ReadRequest(query=context or '')]


def as_claude_tool(reader: GraphReader) -> dict[str, Any]:
    """Expose ``reader.read`` as a Claude tool spec.

    Returns a dict with the tool ``name``, ``description``, JSON-schema
    ``input_schema``, and a ``func`` the host invokes with the parsed input.
    Keeping this an adapter means the reader's ``read`` stays a pure function.
    """

    def func(context: str | None = None) -> str:
        return reader.read(context)

    return {
        "name": "read_knowledge",
        "description": (
            "Retrieve the project's accumulated knowledge. Call this before "
            "starting work to recall prior lessons."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "What you're about to work on (optional hint).",
                }
            },
            "required": [],
        },
        "func": func,
    }
