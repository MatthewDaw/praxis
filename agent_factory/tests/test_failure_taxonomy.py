"""FL3 — failure-class taxonomy dedup at ingestion + staged calibration rollout (R3, R20b, KD4).

Covers the ticket's acceptance condition: ingesting a failure matching an existing class attaches
evidence and increments recurrence WITHOUT duplicating the lesson; a novel failure mints a new
class; while calibration is unmet, taxonomy-dependent automation stays observe-only (the guard
refuses); once a configured streak of uncorrected assignments passes, automation arms (an
observable one-way state flip).
"""

from __future__ import annotations

import importlib

import pytest

from agent_factory import failure_taxonomy as ft
from agent_factory import ingestion_api


@pytest.fixture(autouse=True)
def _reset_calibration_env(monkeypatch):
    monkeypatch.delenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", raising=False)


class _FakeStore:
    """In-memory stand-in for the shared learnings space, wired through ingestion_api's own
    write/read functions (never bypassing them) so the sole-writer contract stays honored."""

    def __init__(self):
        self.classes: dict[str, dict] = {}
        self.lessons: list[dict] = []
        self.calibration: dict | None = None
        self._next_id = 0

    def new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"


@pytest.fixture
def store(monkeypatch):
    s = _FakeStore()

    def fake_write_class(label, *, source=None, meta=None):
        cid = s.new_id("class")
        fact = {"id": cid, "text": label, "meta": dict(meta or {})}
        s.classes[cid] = fact
        return {"id": cid, "action": "added"}

    def fake_write_lesson(text, *, source=None, meta=None):
        s.lessons.append({"text": text, "source": source, "meta": dict(meta or {})})
        return {"id": s.new_id("lesson"), "action": "added"}

    def fake_read_classes():
        return list(s.classes.values())

    def fake_update_class_meta(class_id, meta):
        s.classes[class_id]["meta"] = dict(meta)
        return s.classes[class_id]

    def fake_read_calibration_state():
        return s.calibration

    def fake_write_calibration_state(meta):
        if s.calibration is None:
            s.calibration = {"id": s.new_id("calib"), "meta": dict(meta)}
        else:
            s.calibration["meta"] = dict(meta)
        return s.calibration

    monkeypatch.setattr(ingestion_api, "write_class", fake_write_class)
    monkeypatch.setattr(ingestion_api, "write_lesson", fake_write_lesson)
    monkeypatch.setattr(ingestion_api, "read_classes", fake_read_classes)
    monkeypatch.setattr(ingestion_api, "update_class_meta", fake_update_class_meta)
    monkeypatch.setattr(ingestion_api, "read_calibration_state", fake_read_calibration_state)
    monkeypatch.setattr(ingestion_api, "write_calibration_state", fake_write_calibration_state)
    return s


# --------------------------------------------------------------------------- dedup (R3)

def test_novel_failure_mints_a_new_class(store):
    result = ft.assign_class("connection pool exhausted under load", evidence="stack trace A")
    assert result["action"] == "minted"
    assert result["recurrence_count"] == 1
    assert len(store.classes) == 1
    assert len(store.lessons) == 1


def test_matching_failure_attaches_evidence_and_increments_recurrence_without_duplicating(store):
    first = ft.assign_class("connection pool exhausted under load", evidence="trace A")
    assert len(store.lessons) == 1

    second = ft.assign_class("connection pool exhausted under heavy load", evidence="trace B")

    assert second["action"] == "matched"
    assert second["class_id"] == first["class_id"]
    assert second["recurrence_count"] == 2
    # No duplicate lesson written for the recurrence.
    assert len(store.lessons) == 1
    assert len(store.classes) == 1
    evidence = store.classes[first["class_id"]]["meta"]["evidence"]
    assert [e["text"] for e in evidence] == ["trace A", "trace B"]


def test_unrelated_failure_mints_a_second_distinct_class(store):
    first = ft.assign_class("connection pool exhausted under load")
    second = ft.assign_class("frontend build fails on a missing type export")

    assert second["action"] == "minted"
    assert second["class_id"] != first["class_id"]
    assert len(store.classes) == 2


def test_empty_text_is_rejected(store):
    with pytest.raises(ValueError):
        ft.assign_class("   ")


# --------------------------------------------------------------------------- calibration (R20b/KD4)

def test_automation_stays_unarmed_before_the_calibration_exit_condition(store, monkeypatch):
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "3")
    ft.assign_class("failure one")
    ft.assign_class("failure two")

    assert ft.is_armed() is False
    assert ft.guard_automation("widen") is False
    state = ft.calibration_state()
    assert state["streak"] == 2
    assert state["armed"] is False


def test_calibration_arms_automation_as_an_observable_state_flip_once_the_streak_is_met(store, monkeypatch):
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "3")
    ft.assign_class("failure one")
    ft.assign_class("failure two")
    assert ft.is_armed() is False

    result = ft.assign_class("failure three")

    assert ft.is_armed() is True
    assert ft.guard_automation("widen") is True
    assert result["calibration"]["armed"] is True
    assert result["calibration"]["armed_at"] is not None


def test_a_correction_resets_the_streak_without_arming(store, monkeypatch):
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "3")
    ft.assign_class("failure one")
    ft.assign_class("failure two")
    ft.assign_class("failure three (mislabeled)", corrected=True)

    assert ft.is_armed() is False
    assert ft.calibration_state()["streak"] == 0
    assert ft.calibration_state()["corrections"] == 1


def test_a_correction_after_calibration_has_armed_does_not_disarm(store, monkeypatch):
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "2")
    ft.assign_class("failure one")
    ft.assign_class("failure two")
    assert ft.is_armed() is True

    ft.assign_class("failure three (mislabeled)", corrected=True)

    assert ft.is_armed() is True  # one-way flip — graduation is not rolled back


def test_calibration_exit_count_env_override_defaults_when_unset_or_invalid(monkeypatch):
    monkeypatch.delenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", raising=False)
    assert ft.calibration_exit_count() == ft.DEFAULT_CALIBRATION_EXIT_COUNT
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "not-a-number")
    assert ft.calibration_exit_count() == ft.DEFAULT_CALIBRATION_EXIT_COUNT
    monkeypatch.setenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", "7")
    assert ft.calibration_exit_count() == 7


# --------------------------------------------------------------------------- R20/FL15: near-duplicate sweep

def _seed_class(store: _FakeStore, label: str, *, recurrence: int = 1,
                evidence: list | None = None) -> str:
    """Seed a failure-class fact directly (bypassing :func:`assign_class`'s own ingestion-time
    dedup threshold) so a near-dup sweep test controls its corpus deterministically."""
    cid = store.new_id("class")
    store.classes[cid] = {"id": cid, "text": label,
                          "meta": {"recurrence_count": recurrence, "evidence": evidence or []}}
    return cid


def test_sweep_finds_and_merges_a_planted_near_duplicate_pair_crediting_recurrence(store):
    """The acceptance scenario: a planted near-duplicate lesson pair merges and recurrence is
    retroactively credited onto the survivor."""
    a_id = _seed_class(store, "connection pool exhausted under load", recurrence=2,
                       evidence=[{"text": "trace A"}])
    b_id = _seed_class(store, "connection pool exhausted under load spike",
                       recurrence=1, evidence=[{"text": "trace B"}])

    merges = ft.sweep_near_duplicate_classes()

    assert len(merges) == 1
    merge = merges[0]
    assert {merge["survivor_id"], merge["loser_id"]} == {a_id, b_id}
    assert merge["survivor_id"] == a_id  # higher recurrence survives
    assert merge["credited_recurrence"] == 3
    survivor = store.classes[a_id]
    loser = store.classes[b_id]
    assert survivor["meta"]["recurrence_count"] == 3
    assert loser["meta"]["merged_into"] == a_id
    # Evidence from both sides survives the merge (retroactive credit is not just a number).
    assert len(survivor["meta"]["evidence"]) == 2


def test_sweep_never_gates_on_calibration(store, monkeypatch):
    """The near-dup sweep is housekeeping over the corpus the calibration streak itself watches —
    gating it on calibration would be circular, so it always runs regardless of armed state."""
    monkeypatch.setattr(ft, "guard_automation", lambda action: (_ for _ in ()).throw(
        AssertionError("sweep must never consult guard_automation")))
    _seed_class(store, "disk quota exceeded during snapshot")
    _seed_class(store, "disk quota exceeded during backup snapshot")
    merges = ft.sweep_near_duplicate_classes()
    assert len(merges) == 1


def test_sweep_is_a_no_op_below_threshold_and_on_already_merged_classes(store):
    _seed_class(store, "frontend build fails on a missing type export")
    _seed_class(store, "database connection refused on startup")
    assert ft.sweep_near_duplicate_classes() == []

    # Re-running after nothing merged stays empty (idempotent, no spurious self-merge).
    assert ft.sweep_near_duplicate_classes() == []


def test_find_near_duplicate_pairs_excludes_already_merged_losers(store):
    _seed_class(store, "timeout waiting for upstream response")
    _seed_class(store, "timeout waiting for upstream response again")
    ft.sweep_near_duplicate_classes()
    merged_ids = {cid for cid, c in store.classes.items() if c["meta"].get("merged_into")}
    assert len(merged_ids) == 1

    # A re-run over the now-merged corpus never re-surfaces the merged loser in a pair.
    pairs = ft.find_near_duplicate_pairs()
    ids_in_pairs = {c["id"] for pair in pairs for c in pair[:2]}
    assert not (ids_in_pairs & merged_ids)


# --------------------------------------------------------------------------- surfaced in af-retro

def test_af_retro_surfaces_calibration_state(
    store: "_FakeStore", monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    af_retro = importlib.import_module("agent_factory.af_retro")
    monkeypatch.setattr(af_retro, "read_lessons", lambda *a, **kw: [])
    monkeypatch.setattr(af_retro, "read_checks", lambda *a, **kw: [])
    monkeypatch.setattr(af_retro, "read_flags", lambda *a, **kw: [])
    monkeypatch.setattr(ft, "calibration_state", lambda: {
        "streak": 1, "total_assignments": 4, "corrections": 0, "armed": False,
        "armed_at": None, "required": 20,
    })
    monkeypatch.setattr(af_retro, "read_classes", lambda: [
        {"id": "cls-1", "text": "connection pool exhausted", "meta": {"recurrence_count": 3}},
        {"id": "cls-2", "text": "frontend build fails", "meta": {"recurrence_count": 1,
                                                                  "merged_into": "cls-1"}},
    ])
    rc = af_retro.main(["some-project", "--calibration"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "armed=False" in out
    assert "streak=1/20" in out
    # R20/FL15 — class assignments (recurrence + merge status) are spot-auditable from af-retro.
    assert "recurrence=3" in out
    assert "[active] cls-1" in out
    assert "merged->cls-1" in out


# --------------------------------------------------------------------------- sole-writer discipline

def test_failure_taxonomy_module_never_bypasses_the_ingestion_api_writer():
    """failure_taxonomy.py must route every write through ingestion_api, never call a
    write-shaped _praxis primitive directly (mirrors test_ingestion_api's sole-writer guard)."""
    import re
    from pathlib import Path

    path = Path(failure_taxonomy_file())
    text = path.read_text(encoding="utf-8")
    write_call_re = re.compile(
        r"\b(add_insight|ingest_batch|save_snapshot)\s*\(|"
        r'_request\(\s*"(POST|PUT|PATCH|DELETE)"|'
        r"\b_praxis\.(patch_meta|delete_fact)\s*\("
    )
    assert not write_call_re.search(text), (
        "failure_taxonomy.py must write via agent_factory.ingestion_api only"
    )


def failure_taxonomy_file() -> str:
    return ft.__file__
