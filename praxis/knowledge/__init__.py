"""The Praxis knowledge loop: ingest -> store -> read.

Three core pieces, each an abstract parent with swappable variants:

- ``knowledge_graph`` — the store (MVP: a CLAUDE.md file).
- ``injestion``      — distills raw input into the graph.
- ``graph_reader``   — retrieves knowledge for the agent, given context.
"""
