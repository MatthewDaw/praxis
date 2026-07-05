"""Agent-factory eval harness.

Mirrors the Praxis eval shape (``cases/<name>/case.yaml`` + deterministic checks
referenced by ``module:function``), scoped to the factory's own behaviour rather
than the knowledge graph. Each case authors an input scenario and asserts the
verdict a factory component produces, so edge cases (e.g. H14 dangling concept
references) are caught by a runnable suite instead of by eye.
"""
