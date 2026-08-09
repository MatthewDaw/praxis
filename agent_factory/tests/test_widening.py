"""FL14 (R14, D6, D8) — automatic evidence-gated widening + universal promotion.

Covers the ticket's acceptance condition:
  * post-calibration (FL3 exit met), a recurrence with a passing CLASS-SPECIFIC proof widens the
    check's binding into the new scope;
  * a generic breakage that cannot produce the class-specific proof does NOT widen;
  * a sibling-unavailable widen PARKS with a visible flag and can retry on next recurrence;
  * universal promotion refuses below two distinct projects;
  * a promoted universal resolves and pins in an UNINVOLVED third project alongside the toml
    universals in one resolve pass, with its ``promoted-`` prefixed id;
  * a behavioral near-dup (matching canonical-content hash under a different id) trips the loud
    collision report.
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from typing import Any

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:  # mirrors test_promote_universal_gating.py — see its comment: this
    sys.path.insert(0, _HOOKS)  # pins bare `_praxis` resolution to THIS worktree's hooks/, never a
                                # stale copy shadowed in via an inherited PYTHONPATH entry.

import pytest  # noqa: E402
from hooks import _praxis  # noqa: E402
from hooks import _ticket_state as ts  # noqa: E402

from agent_factory import failure_taxonomy as ft  # noqa: E402
from agent_factory import ingestion_api, widening  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repo(tmp_path: Path, name: str, good_text: str, bad_text: str | None) -> dict[str, Any]:
    """A repo with a healthy commit, optionally followed by a regressing (bad) commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text(good_text + "\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "healthy")
    healthy_sha = _git(repo, "rev-parse", "HEAD")
    bad_sha = None
    if bad_text is not None:
        (repo / "f.txt").write_text(bad_text + "\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "bad")
        bad_sha = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "healthy_sha": healthy_sha, "bad_sha": bad_sha}


@pytest.fixture
def scopes(tmp_path: Path) -> dict[str, Any]:
    """Two independent scopes ('origin' where the failure regressed, 'sibling' the healthy new
    scope) so the widening proof genuinely runs each side in its OWN disposable worktree."""
    origin = _make_repo(tmp_path, "origin", "expected-marker", "regressed-value")
    bundle_bytes = ingestion_api.build_repro_bundle(origin["repo"], origin["bad_sha"])
    bad_artifact_meta = {"bundle_b64": base64.b64encode(bundle_bytes).decode("ascii")}
    sibling = _make_repo(tmp_path, "sibling", "expected-marker", None)
    return {"origin": origin, "sibling": sibling, "bad_artifact_meta": bad_artifact_meta}


@pytest.fixture(autouse=True)
def _armed_calibration(monkeypatch):
    """Force R20b's calibration gate ARMED so :func:`widening.attempt_widen` exercises its real
    decision path rather than the (separately-tested) observe-only short circuit."""
    monkeypatch.setattr(ft, "is_armed", lambda: True)
    monkeypatch.setattr(ft, "guard_automation", lambda action: True)
    monkeypatch.setattr(widening.failure_taxonomy, "guard_automation", lambda action: True)


@pytest.fixture(autouse=True)
def _no_real_flags(monkeypatch):
    """:func:`ingestion_api.emit_flag`/:func:`widen` need an authenticated identity and a live
    Praxis; stub both sanely so these tests exercise the DECISION logic, not the transport."""
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: identity or "tester")
    flags: list[dict[str, Any]] = []
    monkeypatch.setattr(ingestion_api, "_write_insight",
                        lambda text, category, **kw: flags.append(
                            {"text": text, "category": category, **kw}) or {"id": f"fake-{len(flags)}"})
    widen_calls: list[dict[str, Any]] = []

    def fake_patch_check(check_id, project, build_patch, *, identity=None):
        patch = build_patch({"id": check_id, "meta": {}}) if callable(build_patch) else dict(build_patch)
        widen_calls.append({"check_id": check_id, "project": project, "patch": patch})
        return {"id": check_id, "meta": patch}

    monkeypatch.setattr(ingestion_api, "_patch_check", fake_patch_check)
    return {"flags": flags, "widen_calls": widen_calls}


# --------------------------------------------------------------------------- R20b: calibration gate

def test_widen_is_observe_only_while_calibration_unarmed(monkeypatch, scopes):
    monkeypatch.setattr(widening.failure_taxonomy, "guard_automation", lambda action: False)
    result = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q expected-marker f.txt",
        registry={"origin-project": str(scopes["sibling"]["repo"])},
    )
    assert result["status"] == "observe-only"


# --------------------------------------------------------------------------- R14/D6: proven widen

def test_class_specific_proof_widens_into_new_scope(scopes, _no_real_flags):
    result = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q expected-marker f.txt",
        registry={"origin-project": str(scopes["sibling"]["repo"])},
    )
    assert result["status"] == "widened"
    assert _no_real_flags["widen_calls"], "widen() was never invoked"
    call = _no_real_flags["widen_calls"][0]
    assert call["patch"]["applies_to"] == ["new-tag"]


# --------------------------------------------------------------------------- R14 inversion guard: generic breakage

def test_generic_breakage_that_fails_the_healthy_sibling_too_does_not_widen(scopes, _no_real_flags):
    """A check whose failure is NOT class-specific fails on the bad artifact AND the healthy
    sibling ('fails-both') — never proven, so it must never widen (E7's inversion guard)."""
    result = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q absolutely-nonexistent f.txt",
        registry={"origin-project": str(scopes["sibling"]["repo"])},
    )
    assert result["status"] == "not-widened"
    assert result["reason"] == "fails-both"
    assert not _no_real_flags["widen_calls"]


def test_vacuous_check_that_passes_the_bad_artifact_too_does_not_widen(scopes, _no_real_flags):
    result = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -c '' f.txt",
        registry={"origin-project": str(scopes["sibling"]["repo"])},
    )
    assert result["status"] == "not-widened"
    assert result["reason"] == "vacuous-pass-on-bad-artifact"
    assert not _no_real_flags["widen_calls"]


# --------------------------------------------------------------------------- E8/R24: sibling unavailable -> parks

def test_sibling_unavailable_parks_with_a_visible_flag_and_never_widens(scopes, _no_real_flags):
    result = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q expected-marker f.txt",
        registry={},  # the box worktree registry has no entry for this project
    )
    assert result["status"] == "parked"
    assert result["retry"] is True
    assert not _no_real_flags["widen_calls"]
    parking = [f for f in _no_real_flags["flags"] if f["category"] == ingestion_api.FLAG_CATEGORY]
    assert parking, "no flag was emitted for the unresolvable sibling"
    assert parking[0]["meta"]["kind"] == ingestion_api.FLAG_KIND_PARKING


def test_a_later_recurrence_after_the_registry_gains_an_entry_can_still_widen(scopes, _no_real_flags):
    """The park is never terminal — retrying attempt_widen (the E8 recurrence path) once the
    registry resolves the sibling succeeds exactly like the direct case."""
    parked = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q expected-marker f.txt",
        registry={},
    )
    assert parked["status"] == "parked"
    retried = widening.attempt_widen(
        "check-1", "origin-project", "new-tag", class_id="class-1",
        bad_artifact_meta=scopes["bad_artifact_meta"], run="grep -q expected-marker f.txt",
        registry={"origin-project": str(scopes["sibling"]["repo"])},
    )
    assert retried["status"] == "widened"


def test_resolve_sibling_worktree_returns_none_for_a_nonexistent_path(tmp_path):
    assert widening.resolve_sibling_worktree(
        "ghost-project", registry={"ghost-project": str(tmp_path / "does-not-exist")}
    ) is None


# --------------------------------------------------------------------------- R14: universal promotion

def _authed(monkeypatch):
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: identity or "tester")


def test_universal_promotion_refuses_below_two_distinct_projects(monkeypatch):
    _authed(monkeypatch)
    result = ingestion_api.promote_universal(
        "always redact secrets before insertion", "grep -L SECRET *.py",
        recurring_projects=["only-one-project"],
    )
    assert result["status"] == "refused"
    assert result["reason"] == "insufficient-recurrence"


def test_universal_promotion_succeeds_with_two_distinct_projects_and_promoted_prefixed_id(monkeypatch):
    _authed(monkeypatch)
    monkeypatch.setattr(ingestion_api, "read_promoted_universals", lambda: [])
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(ingestion_api, "_write_insight",
                        lambda text, category, **kw: written.append(
                            {"text": text, "category": category, **kw}) or {"id": "insight-1"})
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])

    result = ingestion_api.promote_universal(
        "always redact secrets before insertion", "grep -L SECRET *.py",
        recurring_projects=["project-a", "project-b"],
    )
    assert result["status"] == "promoted"
    assert result["check_id"].startswith(ingestion_api.PROMOTED_UNIVERSAL_PREFIX)
    assert written and written[0]["meta"]["applies_to"] == ["*"]
    assert written[0]["meta"]["recurring_projects"] == ["project-a", "project-b"]


def test_universal_promotion_trips_loud_collision_on_matching_canonical_hash_different_id(monkeypatch):
    _authed(monkeypatch)
    monkeypatch.setattr(ingestion_api, "read_promoted_universals", lambda: [
        {"id": "insight-existing", "meta": {
            "check_id": "promoted-abc123",
            "canonical_content_hash": ingestion_api._canonical_content_hash(
                "always redact secrets before insertion", "grep -L SECRET *.py"),
        }},
    ])
    with pytest.raises(ingestion_api.UniversalPromotionCollision):
        ingestion_api.promote_universal(
            "always   redact secrets before insertion", "grep -L SECRET *.py",  # whitespace-only diff
            recurring_projects=["project-a", "project-c"],
        )


# --------------------------------------------------------------------------- D8: one resolve pass, uninvolved project

def test_promoted_universal_resolves_alongside_toml_universals_in_one_pass(monkeypatch):
    """The exact acceptance clause: a promoted universal resolves and PINS in an UNINVOLVED third
    project (one that never carries the promoted check in its own building-validation snapshot)
    alongside the toml universals, in one call to ``universal_requirements``."""
    from agent_factory.rubric import rubric_from_dict
    from agent_factory.seeded_checks import SeededCheck

    toml_check = SeededCheck(
        check_id="minimalism-dry", kind="graded", applies_to=("*",), criterion="strict minimization",
        promote_universal=True,
        rubric=rubric_from_dict({
            "axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "no dead code"}],
            "anchors": {"good": ["return a + b"], "slop": ["unused = a + b"]},
        }),
        report_only=True,
    )
    monkeypatch.setattr(ts, "_universal_checks", lambda: [toml_check])
    monkeypatch.setattr(ts, "_promoted_universal_checks", lambda: [
        {"id": "insight-promoted-1", "text": "always redact secrets before insertion",
         "meta": {"check_id": "promoted-abc123", "applies_to": ["*"], "promoted": True,
                  "run": "grep -L SECRET *.py"}},
    ])

    reqs = ts.universal_requirements("uninvolved-ticket", {"tags": ["backend"]})
    ids = {r["id"] for r in reqs}
    assert "minimalism-dry" in ids  # the toml universal, unaffected
    assert "promoted-abc123" in ids  # the cloud-promoted universal, same resolve pass
    promoted_req = next(r for r in reqs if r["id"] == "promoted-abc123")
    assert promoted_req["meta"]["run"] == "grep -L SECRET *.py"


def test_promoted_universal_lane_degrades_to_empty_on_praxis_failure(monkeypatch):
    """A cloud outage never takes the load-bearing toml universal lane down with it."""
    def _boom():
        raise RuntimeError("no network")
    monkeypatch.setattr(ingestion_api, "read_promoted_universals", _boom)
    assert ts._promoted_universal_checks() == []
