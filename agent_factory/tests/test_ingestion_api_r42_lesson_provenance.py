"""R42 — a duplicate-match ``ingest()`` occurrence APPENDS its source (plus channel and a
timestamp) to the existing lesson's accumulated ``meta.provenance`` list via a clobber-guarded
read-modify-write (:func:`agent_factory.ingestion_api._append_lesson_provenance`, following the
``hooks._ticket_state.accumulate_regression_detail`` precedent), instead of the occurrence being
discarded outright (the pre-R42 behaviour: the duplicate branch reused the existing lesson id and
wrote nothing else).
"""

from __future__ import annotations

import pytest

from agent_factory import ingestion_api
from conftest import FakeCheckStore as _FakeStore


@pytest.fixture
def store(check_store: _FakeStore) -> _FakeStore:
    return check_store


def test_ingesting_the_same_text_twice_under_different_sources_accumulates_both_in_provenance(
    store: _FakeStore,
) -> None:
    """The R42 acceptance condition, happy path: two ``ingest()`` calls with identical text but
    different ``source`` values must land as ONE lesson whose ``meta.provenance`` list carries
    BOTH source values -- the second occurrence must never be silently discarded."""
    first = ingestion_api.ingest("the same complaint text", "proj", source="source-a",
                                 channel="human")
    second = ingestion_api.ingest("the same complaint text", "proj", source="source-b",
                                  channel="machine")

    assert first["lesson_id"] == second["lesson_id"], "must dedup onto ONE lesson row"
    lesson = store.facts[first["lesson_id"]]
    provenance = lesson["meta"]["provenance"]
    sources = [p["source"] for p in provenance]
    assert sources == ["source-a", "source-b"], provenance
    # channel + a timestamp ride along on the appended entry, not just the source.
    assert provenance[-1]["channel"] == "machine"
    assert provenance[-1]["at"] is not None


def test_a_third_duplicate_occurrence_appends_without_clobbering_the_first_two(
    store: _FakeStore,
) -> None:
    """A clobber-guarded read-modify-write: the THIRD occurrence must not overwrite the list a
    ``patch_meta`` wholesale-replace of a stale in-memory copy would produce -- it must extend it."""
    ingestion_api.ingest("dup text", "proj", source="source-a", channel="human")
    ingestion_api.ingest("dup text", "proj", source="source-b", channel="human")
    result = ingestion_api.ingest("dup text", "proj", source="source-c", channel="human")

    lesson = store.facts[result["lesson_id"]]
    sources = [p["source"] for p in lesson["meta"]["provenance"]]
    assert sources == ["source-a", "source-b", "source-c"], lesson["meta"]["provenance"]


def test_a_lesson_written_before_this_shipped_seeds_provenance_from_its_legacy_source_on_first_append(
    store: _FakeStore,
) -> None:
    """A lesson written before R42 shipped has no ``meta.provenance`` list at all -- only the
    legacy single top-level ``source`` field every lesson has always carried. The FIRST append onto
    such a lesson must not start the provenance list empty (which would silently drop the lesson's
    original source): it must be seeded from that legacy value first."""
    legacy_id = "legacy-lesson-1"
    store.facts[legacy_id] = {
        "id": legacy_id, "category": ingestion_api.LESSON_CATEGORY, "source": "legacy-source",
        "meta": {"class": "uncategorized", "duplicate_of": None,
                "content_hash": ingestion_api._hash_text(
                    ingestion_api._normalize_lesson_text("a pre-existing complaint")),
                "channel": "human"},
    }

    result = ingestion_api.ingest("a pre-existing complaint", "proj", source="new-source",
                                  channel="machine")

    assert result["lesson_id"] == legacy_id
    provenance = store.facts[legacy_id]["meta"]["provenance"]
    sources = [p["source"] for p in provenance]
    assert sources == ["legacy-source", "new-source"], provenance


def test_accumulate_lesson_provenance_is_a_pure_read_modify_write_helper() -> None:
    """Unit-level pin on the helper itself (mirrors ``accumulate_regression_detail``'s own test
    shape): given an existing lesson fact and a new entry, it returns the full merged list without
    mutating the caller's original dict, and copes with a missing/empty ``meta.provenance``."""
    existing = {"id": "L1", "source": "orig", "meta": {}}
    out = ingestion_api.accumulate_lesson_provenance(existing, {"source": "s2", "channel": "c",
                                                                "at": 123.0})
    assert [e["source"] for e in out] == ["orig", "s2"]
    # the caller's own meta dict must be untouched -- accumulate_lesson_provenance is read-only
    # over its input, the caller (_append_lesson_provenance) does the actual write.
    assert "provenance" not in existing["meta"]
