"""Offline truth-table test for the completeness decision logic.

``PostgresVectorGraph._completeness_reasons`` is a pure static function — the one
piece of the derived-completeness feature that needs no Postgres — so it can pin
the never-built / regressed / stale / complete classification (and the primary
precedence never-built > regressed > stale) even on a CI box with no DSN, where
the DB-gated graph/serve tests skip. Keep this aligned with the model documented
on ``incomplete_requirements``.
"""

from __future__ import annotations

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)

reasons = PostgresVectorGraph._completeness_reasons


def test_never_built_when_no_success():
    # No successful outcome yet -> never-built, regardless of last_outcome/failures.
    assert reasons(0, None, False) == ["never-built"]
    assert reasons(0, "failed", False) == ["never-built"]  # attempted but never passed


def test_regressed_when_latest_failed_after_a_success():
    assert reasons(1, "failed", False) == ["regressed"]
    assert reasons(3, "failed", False) == ["regressed"]


def test_complete_when_latest_succeeded_and_not_stale():
    # Latest outcome succeeded and nothing stale -> complete (no reasons).
    assert reasons(1, "succeeded", False) == []
    assert reasons(5, "succeeded", False) == []


def test_stale_is_additive_and_independent_of_outcome():
    # A passing requirement whose dependency changed is incomplete for rework.
    assert reasons(1, "succeeded", True) == ["stale"]


def test_primary_precedence_never_built_over_regressed_over_stale():
    # never-built dominates even when also stale.
    assert reasons(0, "failed", True) == ["never-built", "stale"]
    # regressed dominates stale; both surface, primary first.
    assert reasons(2, "failed", True) == ["regressed", "stale"]
    # primary is always reasons[0].
    assert reasons(2, "failed", True)[0] == "regressed"
