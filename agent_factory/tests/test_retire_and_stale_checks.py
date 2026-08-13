"""BUG B — ``retire_check``: a first-class "this check is STALE, stop it gating anything" verb.
BUG C — ``stale_checks_by_missing_path``: a pure detector for checks whose ``run`` names a phantom file.

BUG B: the build workers asked for "dismiss/retire requirement <id>" twice; the only working path was
hand-patching ``meta.applies_to``. ``retire_check`` composes the atomic stop: empty ``applies_to`` (no
tag/identity lane resolves it), set kill_switch (so ``_ticket_state._is_retired`` drops it from the
surface lane too), record the reason, and emit the push-not-pull suspension flag.

BUG C: a building-validation check's ``run`` was ``mypy … tests/test_taolu_rig_validation_staff_plane.py``
— a file from a DISCARDED worktree attempt that never merged, gating forever against a phantom path.
``stale_checks_by_missing_path`` names such checks (never deletes; a pure detector).

Reuses the shared ``check_store`` fixture (``conftest.FakeCheckStore``): a real ``_patch_check`` /
``_suspend_patch`` / ``emit_flag`` round trip against an in-memory ``building-validation`` snapshot.
"""

from __future__ import annotations

from agent_factory import ingestion_api


# --------------------------------------------------------------------------- BUG B: retire_check

def test_retire_check_empties_applies_to_and_kills_and_flags(check_store):
    check_store.seed_check("phantom", {
        "applies_to": ["R7", "auth"], "scope": "validation",
        ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING,
    })

    result = ingestion_api.retire_check("phantom", "proj", "stale check from a discarded attempt")

    meta = result["meta"]
    assert meta["applies_to"] == [], "every ticket-cid/tag binding is dropped"
    assert meta["kill_switch"] is True
    assert meta["retired"] is True
    assert meta["retire_reason"] == "stale check from a discarded attempt"
    assert meta[ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_SUSPENDED

    # the persisted fact reflects the same (patch_meta wrote it back)
    stored = check_store.check("phantom")["meta"]
    assert stored["applies_to"] == [] and stored["kill_switch"] is True

    # a push-not-pull suspension flag was emitted (R24)
    flags = check_store.facts_by(category=ingestion_api.FLAG_CATEGORY)
    assert flags, "retiring a check emits a pending-attention flag"
    fmeta = flags[-1]["meta"]
    assert fmeta["kind"] == ingestion_api.FLAG_KIND_SUSPENSION
    assert fmeta["check_id"] == "phantom" and fmeta.get("retired") is True


def test_retired_check_is_treated_as_retired_by_the_resolver_guard(check_store):
    """The whole point: after retirement the check reads as retired to the resolver's drop filter."""
    from hooks import _ticket_state as ts

    check_store.seed_check("phantom", {"applies_to": ["R7"],
                                       ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING})
    ingestion_api.retire_check("phantom", "proj", "stale")
    assert ts._is_retired(check_store.check("phantom")) is True


# --------------------------------------------------------------------------- BUG C: stale detector

def _seed(check_store, cid, run, extra=None):
    meta = {"scope": "validation", "run": run}
    meta.update(extra or {})
    check_store.seed_check(cid, meta)


def test_stale_checks_names_the_check_with_a_missing_path(check_store, tmp_path):
    # A file that DOES exist under the repo root...
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_present.py").write_text("x = 1\n", encoding="utf-8")

    _seed(check_store, "live", "mypy tests/test_present.py")
    _seed(check_store, "stale", "mypy tests/test_taolu_rig_validation_staff_plane.py")

    findings = ingestion_api.stale_checks_by_missing_path("proj", tmp_path)

    ids = {f["check_id"] for f in findings}
    assert ids == {"stale"}, "only the check naming a nonexistent path is stale"
    (stale,) = findings
    assert stale["missing_paths"] == ["tests/test_taolu_rig_validation_staff_plane.py"]
    assert "mypy" in stale["run"]


def test_stale_detector_ignores_nonpath_arguments(check_store, tmp_path):
    # `pytest` and `--strict` are not paths; a bare module name (`agent_factory.x`) is not either.
    _seed(check_store, "modules-only", "python -m pytest --strict")
    findings = ingestion_api.stale_checks_by_missing_path("proj", tmp_path)
    assert findings == [], "a run with no file-path argument is never flagged stale"


def test_stale_detector_skips_already_retired_checks(check_store, tmp_path):
    _seed(check_store, "retired-stale", "mypy tests/gone.py",
          {"kill_switch": True, ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_SUSPENDED})
    findings = ingestion_api.stale_checks_by_missing_path("proj", tmp_path)
    assert findings == [], "a retired check no longer gates, so its phantom path is not a live finding"


def test_stale_detector_does_not_mutate(check_store, tmp_path):
    _seed(check_store, "stale", "mypy tests/gone.py")
    calls_before = list(check_store.calls)
    ingestion_api.stale_checks_by_missing_path("proj", tmp_path)
    # No PATCH/POST was issued — it is a pure detector.
    assert not [c for c in check_store.calls[len(calls_before):] if c[0] in ("PATCH", "POST")]
