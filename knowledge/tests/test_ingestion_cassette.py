"""Tests for the IngestionCassette (replay / record / loud-miss / skip)."""

import json

import pytest

from knowledge.llm.ingestion_cassette import IngestionCassette


def _boom(_):  # an `inner` that must never be called on a hit
    raise AssertionError("inner must not run on a hit")


def test_replays_committed_output_without_calling_inner(tmp_path):
    path = tmp_path / "ingest.json"
    # Seed by recording once with compute allowed.
    rec = IngestionCassette(path, model_id="m", inner=lambda s: "distilled", allow_compute=True)
    rec("raw input")
    # A fresh replay-only cassette returns it without invoking inner.
    cas = IngestionCassette(path, model_id="m", inner=_boom, allow_compute=False)
    assert cas("raw input") == "distilled"


def test_records_on_miss_when_allowed(tmp_path):
    path = tmp_path / "ingest.json"
    calls = {"n": 0}

    def inner(raw):
        calls["n"] += 1
        return f"facts::{raw}"

    cas = IngestionCassette(path, model_id="m", inner=inner, allow_compute=True)
    assert cas("X") == "facts::X"
    assert calls["n"] == 1
    # Persisted: a new replay-only cassette serves it without recomputing.
    again = IngestionCassette(path, model_id="m", inner=inner, allow_compute=False)
    assert again("X") == "facts::X"
    assert calls["n"] == 1  # not recomputed


def test_loud_miss_when_recording_disabled(tmp_path):
    cas = IngestionCassette(tmp_path / "ingest.json", model_id="m", inner=None, allow_compute=False)
    with pytest.raises(RuntimeError, match="cassette miss"):
        cas("missing input")


def test_loud_miss_when_allow_compute_but_no_inner(tmp_path):
    # allow_compute without a live inner can't compute -> loud, never silent/empty.
    cas = IngestionCassette(tmp_path / "ingest.json", model_id="m", inner=None, allow_compute=True)
    with pytest.raises(RuntimeError, match="cassette miss"):
        cas("missing input")


def test_model_id_is_part_of_the_key(tmp_path):
    path = tmp_path / "ingest.json"
    IngestionCassette(path, model_id="m1", inner=lambda s: "out", allow_compute=True)("raw")
    # A different ingest model is a clean miss, not a stale hit.
    other = IngestionCassette(path, model_id="m2", inner=None, allow_compute=False)
    with pytest.raises(RuntimeError, match="cassette miss"):
        other("raw")


def test_changed_input_is_a_clean_miss(tmp_path):
    path = tmp_path / "ingest.json"
    IngestionCassette(path, model_id="m", inner=lambda s: "out", allow_compute=True)("seed v1")
    cas = IngestionCassette(path, model_id="m", inner=None, allow_compute=False)
    assert cas("seed v1") == "out"  # unchanged input replays
    with pytest.raises(RuntimeError, match="cassette miss"):
        cas("seed v2")  # edited input -> new key -> loud, never a stale reuse


def test_save_merges_concurrent_on_disk_writes(tmp_path):
    path = tmp_path / "ingest.json"
    a = IngestionCassette(path, model_id="m", inner=lambda s: "a-out", allow_compute=True)
    b = IngestionCassette(path, model_id="m", inner=lambda s: "b-out", allow_compute=True)
    a("input-a")
    b("input-b")  # b loaded before a's write; its save must merge, not clobber
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk) == 2
