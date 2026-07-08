"""Unit tests for the reserved-space-name source of truth (no DB / no app fixture).

Reserving these names makes the retired standalone layout (top-level ``*-validation`` / ``*-plan``
spaces) unrepresentable; the canonical layout is ONE space per project holding per-role snapshots.
"""

from __future__ import annotations

from knowledge.serve.reserved_names import RESERVED_EVAL_SPACE, is_reserved_space_id


def test_eval_space_is_reserved():
    assert is_reserved_space_id(RESERVED_EVAL_SPACE)


def test_retired_standalone_ids_are_reserved():
    for sid in ("coding-validation", "building-validation", "planning-validation", "build-plan"):
        assert is_reserved_space_id(sid), sid


def test_any_plan_suffix_slug_is_reserved():
    # ``<x>-plan`` is a plan snapshot role, never a standalone space.
    assert is_reserved_space_id("shopping-plan")
    assert is_reserved_space_id("foo-plan")


def test_ordinary_project_slugs_are_allowed():
    for sid in ("team-app", "bestie", "shopping", "eval-plan-repro", "planning", "validation"):
        assert not is_reserved_space_id(sid), sid


def test_spaces_store_refuses_reserved_names_without_db():
    # The store guard runs BEFORE any SQL, so a reserved name raises without touching the DB.
    import pytest

    from knowledge.serve.spaces_store import SpacesStore

    store = SpacesStore(conn=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.create_space("org", "building-validation", None)
    with pytest.raises(ValueError):
        store.ensure_space("org", "foo-plan")
