"""Unit tests for the write-time snapshot-KIND invariant (no DB required).

The invariant makes it impossible to co-mingle validation ``check`` facts with a ``prd-<project>``
plan (or a plan fact with a ``*-validation`` snapshot), enforced on WRITE. These tests pin the pure
name-derivation + row/SQL predicates that feed BOTH the per-row (``_add``) and bulk (save/copy)
enforcement, so there is exactly one source of truth for the rule.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    SnapshotKindError,
    _row_allowed,
    _snapshot_kind,
    _snapshot_violator_sql,
)


def test_kind_derives_from_snapshot_name():
    assert _snapshot_kind("prd-bestie") == "plan"
    assert _snapshot_kind("prd-") == "plan"
    assert _snapshot_kind("building-validation") == "building-validation"
    assert _snapshot_kind("planning-validation") == "planning-validation"
    # Everything else (evals, demos, ad-hoc, empty) is UNCONSTRAINED.
    for name in ("__evals__", "monica/case-1", "scratch", "", None):
        assert _snapshot_kind(name) is None


def test_plan_admits_no_check_facts():
    assert _row_allowed("plan", "requirement", None) is True
    assert _row_allowed("plan", "surface", None) is True
    # A check — regardless of scope — is forbidden in a plan snapshot.
    assert _row_allowed("plan", "check", "validation") is False
    assert _row_allowed("plan", "check", "planning") is False
    assert _row_allowed("plan", "check", None) is False


def test_validation_snapshot_admits_only_matching_scope_checks():
    assert _row_allowed("building-validation", "check", "validation") is True
    assert _row_allowed("building-validation", "check", "planning") is False
    assert _row_allowed("building-validation", "requirement", None) is False
    assert _row_allowed("planning-validation", "check", "planning") is True
    assert _row_allowed("planning-validation", "check", "validation") is False
    assert _row_allowed("planning-validation", "requirement", None) is False


def test_unconstrained_kind_admits_anything():
    for category in ("check", "requirement", "surface", None):
        for scope in ("validation", "planning", None):
            assert _row_allowed(None, category, scope) is True


def test_violator_sql_is_none_only_for_unconstrained():
    assert _snapshot_violator_sql(None) is None
    assert _snapshot_violator_sql("plan") == "category = 'check'"
    # The kinded predicates resolve a check's scope from meta.scope, falling back to the column.
    for kind, scope in (("building-validation", "validation"), ("planning-validation", "planning")):
        sql = _snapshot_violator_sql(kind)
        assert "COALESCE(meta->>'scope', scope)" in sql
        assert scope in sql


def test_snapshot_kind_error_is_value_error():
    # Subclassing ValueError lets the candidate write routes (except ValueError -> 400) reuse it.
    assert issubclass(SnapshotKindError, ValueError)
    with pytest.raises(ValueError):
        raise SnapshotKindError("boom")
