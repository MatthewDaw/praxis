"""FL15 — resurrection path (R20): ingestion consults archived/suspended checks of the same
failure class BEFORE drafting anew, and a match is resurrected carrying its prior proof history
forward instead of minting a duplicate.

Covers the ticket's acceptance condition (resurrection half): post-calibration, re-ingesting a
failure of an archived class resurrects the prior check with its history instead of drafting new.
"""

from __future__ import annotations

from typing import Any

import pytest
from hooks import _praxis

from agent_factory import failure_taxonomy as ft
from agent_factory import ingestion_api


class _WhoAmIStub:
    def __init__(self, ok: bool, principal: str = "user-1") -> None:
        self.ok = ok
        self.principal = principal
        self.detail = ""


def _authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_praxis, "whoami", lambda: _WhoAmIStub(True))


class _FakeStore:
    """In-memory double for both the shared learnings space (classes/lessons) and a project's
    building-validation snapshot (checks) — enough of ``_praxis``'s surface for :func:`ingest`
    and the resurrection primitives to round-trip end-to-end."""

    def __init__(self) -> None:
        self.facts: dict[str, dict[str, Any]] = {}
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"fake-{self._n}"

    def seed_check(self, check_id: str, meta: dict[str, Any]) -> dict[str, Any]:
        fact = {"id": check_id, "category": "check", "meta": dict(meta)}
        self.facts[check_id] = fact
        return fact

    def seed_class(self, class_id: str, label: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        fact = {"id": class_id, "category": ingestion_api.CLASS_CATEGORY, "text": label,
                "meta": dict(meta or {})}
        self.facts[class_id] = fact
        return fact

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
               space: str | None = None, snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        if method == "POST" and path == "/insights":
            fid = self.new_id()
            fact = {"id": fid, "category": (body or {}).get("category"),
                    "text": (body or {}).get("insight"), "meta": dict((body or {}).get("meta") or {})}
            self.facts[fid] = fact
            return {"id": fid, "action": "added"}
        if method == "POST" and path == "/requirements/regress":
            return {"count": len((body or {}).get("ids", []))}
        return {}

    def facts_by(self, category: str | None = None, meta: dict[str, Any] | None = None,
                state: str = "active", space: str | None = None,
                snapshot: str | None = None) -> list[dict[str, Any]]:
        out = []
        for fact in self.facts.values():
            if category is not None and fact["category"] != category:
                continue
            if meta:
                ok = True
                for k, v in meta.items():
                    fv = fact["meta"].get(k)
                    ok = ok and (v in fv if isinstance(fv, list) else fv == v)
                if not ok:
                    continue
            out.append(fact)
        return out

    def patch_meta(self, cid: str, meta_dict: dict[str, Any], *, space: str | None = None,
                   snapshot: str | None = None) -> dict[str, Any]:
        self.facts[cid]["meta"].update(meta_dict)
        return self.facts[cid]

    def get_fact(self, cid: str, *, space: str | None = None, snapshot: str | None = None,
                not_found_ok: bool = False) -> dict[str, Any]:
        return self.facts.get(cid, {"meta": {}})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    st = _FakeStore()
    _authed(monkeypatch)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    monkeypatch.setattr(_praxis, "_request", st.request)
    monkeypatch.setattr(_praxis, "facts_by", st.facts_by)
    monkeypatch.setattr(_praxis, "patch_meta", st.patch_meta)
    monkeypatch.setattr(_praxis, "get_fact", st.get_fact)
    monkeypatch.setattr(_praxis, "regress_requirements", lambda *a, **kw: {})
    monkeypatch.delenv("FAILURE_TAXONOMY_CALIBRATION_COUNT", raising=False)
    return st


def _arm_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force calibration armed without going through the streak (post-FL3-exit shape)."""
    monkeypatch.setattr(ft, "is_armed", lambda: True)


# --------------------------------------------------------------------------- find/resurrect primitives

def test_find_resurrectable_check_ignores_gating_and_report_only(store: _FakeStore) -> None:
    store.seed_check("c-gating", {"failure_class_id": "cls-1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_GATING})
    store.seed_check("c-report", {"failure_class_id": "cls-1", ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_REPORT_ONLY})
    assert ingestion_api.find_resurrectable_check("cls-1", "proj") is None


def test_find_resurrectable_check_finds_archived_and_suspended(store: _FakeStore) -> None:
    store.seed_check("c-archived", {"failure_class_id": "cls-1",
                                    ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_ARCHIVED})
    found = ingestion_api.find_resurrectable_check("cls-1", "proj")
    assert found is not None and found["id"] == "c-archived"


def test_resurrect_check_carries_prior_proof_history_forward(store: _FakeStore) -> None:
    store.seed_check("c1", {"check_id": "c1", "failure_class_id": "cls-1", "run": "pytest tests/test_x.py",
                            "proof_status": "proven",
                            ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_ARCHIVED})
    result = ingestion_api.resurrect_check("c1", "proj", evidence="fresh recurrence")
    meta = result["meta"]
    assert meta[ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    # Prior proof history is untouched, not overwritten by the resurrection.
    assert meta["run"] == "pytest tests/test_x.py"
    assert meta["proof_status"] == "proven"
    assert meta["resurrection_history"][-1]["evidence"] == "fresh recurrence"


# --------------------------------------------------------------------------- attempt_resurrect (calibration-gated)

def test_attempt_resurrect_stays_observe_only_before_calibration_is_armed(store: _FakeStore) -> None:
    store.seed_check("c1", {"failure_class_id": "cls-1",
                            ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_ARCHIVED})
    result = ft.attempt_resurrect("cls-1", "proj")
    assert result == {"resurrected": False, "check": store.facts["c1"], "class_id": "cls-1",
                      "reason": "calibration-not-armed"}
    assert store.facts["c1"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_ARCHIVED


def test_attempt_resurrect_with_no_candidate_reports_no_resurrectable_check(
    store: _FakeStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm_calibration(monkeypatch)
    result = ft.attempt_resurrect("cls-missing", "proj")
    assert result == {"resurrected": False, "check": None, "class_id": "cls-missing",
                      "reason": "no-resurrectable-check"}


def test_attempt_resurrect_flips_the_check_once_armed(
    store: _FakeStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm_calibration(monkeypatch)
    store.seed_check("c1", {"check_id": "c1", "failure_class_id": "cls-1", "proof_status": "proven",
                            ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_SUSPENDED})
    result = ft.attempt_resurrect("cls-1", "proj", evidence="recurrence")
    assert result["resurrected"] is True
    assert result["check"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING


# --------------------------------------------------------------------------- end-to-end: ingest() wiring

def test_ingest_resurrects_the_archived_check_of_a_matching_class_instead_of_drafting_new(
    store: _FakeStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance scenario: post-calibration, re-ingesting a failure of an archived class
    resurrects the prior check with its history instead of drafting new."""
    _arm_calibration(monkeypatch)
    store.seed_class("cls-1", "connection pool exhausted under load")
    store.seed_check("c1", {"check_id": "c1", "failure_class_id": "cls-1", "run": "pytest tests/test_pool.py",
                            "proof_status": "proven",
                            ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_ARCHIVED})
    monkeypatch.setattr(ft, "find_matching_class",
                        lambda text, classes=None, **kw: store.facts["cls-1"])

    before = len([f for f in store.facts.values() if f["category"] == "check"])
    result = ingestion_api.ingest(
        "connection pool exhausted under load again", "proj",
        drafted_run="pytest tests/test_pool_v2.py", channel="machine",
    )
    after = len([f for f in store.facts.values() if f["category"] == "check"])

    assert result["resurrected"] is True
    assert result["check_id"] == "c1"
    assert after == before  # no NEW check was drafted
    assert store.facts["c1"]["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert store.facts["c1"]["meta"]["run"] == "pytest tests/test_pool.py"  # history preserved verbatim


def test_ingest_drafts_normally_when_no_class_matches(store: _FakeStore) -> None:
    result = ingestion_api.ingest(
        "an entirely novel failure never seen before", "proj",
        drafted_run="pytest tests/test_new.py", channel="machine",
    )
    assert result.get("resurrected") is False
    assert result["check_id"] is not None
    assert store.facts[result["check_id"]]["meta"]["failure_class_id"] is None
