"""FL11 — the human channel: a dedicated ``/af-learn`` skill callable from any repo.

Covers the ticket's acceptance condition end-to-end:
  * a complaint filed from another repo lands a lesson plus active check in the NAMED project,
    with proof status recorded;
  * bulk mode inserts all requested checks without per-check oversight;
  * an unresolvable project space refuses with guidance and writes NOTHING;
  * an unproven check auto-upgrades to proven on its first live catch (both the machine
    report_only path FL12 already covered, and the FL11-new gating-but-unproven human path).
"""

from __future__ import annotations

from typing import Any

import pytest
from hooks import _praxis

from agent_factory import af_learn, ingestion_api


class _WhoAmIStub:
    def __init__(self, ok: bool, principal: str = "user-1") -> None:
        self.ok = ok
        self.principal = principal
        self.detail = ""


def _authed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_praxis, "whoami", lambda: _WhoAmIStub(True))


def _recording_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    counter = {"n": 0}

    def fake_request(method: str, path: str, *, body: dict[str, Any] | None = None,
                     params: dict[str, Any] | None = None, space: str | None = None,
                     snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, "body": body,
                      "space": space, "snapshot": snapshot})
        if method == "POST" and path == "/insights":
            counter["n"] += 1
            return {"id": f"fake-{counter['n']}", "action": "added"}
        if method == "POST" and path == "/requirements/regress":
            return {"count": len((body or {}).get("ids", []))}
        return {}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    return calls


def _calls_for(calls: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    return [c for c in calls if c["body"] and c["body"].get("category") == category]


class _FakeStore:
    """A minimal in-memory double for a project's ``building-validation`` snapshot — enough of
    ``_praxis``'s surface (``_request``, ``facts_by``, ``patch_meta``) to round-trip an ``ingest``
    write followed by an ``upgrade_on_first_pass`` lookup against the SAME check."""

    def __init__(self) -> None:
        self.facts: dict[str, dict[str, Any]] = {}
        self._n = 0

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
               space: str | None = None, snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        if method == "POST" and path == "/insights":
            self._n += 1
            fid = f"fake-{self._n}"
            self.facts[fid] = {"id": fid, "category": (body or {}).get("category"),
                              "meta": dict((body or {}).get("meta") or {})}
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


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    st = _FakeStore()
    _authed(monkeypatch)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    monkeypatch.setattr(_praxis, "_request", st.request)
    monkeypatch.setattr(_praxis, "facts_by", st.facts_by)
    monkeypatch.setattr(_praxis, "patch_meta", st.patch_meta)
    return st


# --------------------------------------------------------------------------- E9: project resolution

def test_resolve_target_project_uses_explicit_argument_over_env() -> None:
    assert af_learn.resolve_target_project("explicit-proj", env={"FACTORY_PROJECT": "env-proj"}) == "explicit-proj"


def test_resolve_target_project_falls_back_to_factory_project_env() -> None:
    assert af_learn.resolve_target_project(None, env={"FACTORY_PROJECT": "env-proj"}) == "env-proj"


def test_resolve_target_project_strips_a_leading_prd_prefix() -> None:
    assert af_learn.resolve_target_project("prd-my-proj") == "my-proj"
    assert af_learn.resolve_target_project(None, env={"FACTORY_PROJECT": "prd-my-proj"}) == "my-proj"


def test_resolve_target_project_refuses_with_no_argument_and_no_env() -> None:
    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.resolve_target_project(None, env={})


def test_resolve_target_project_refuses_on_blank_strings() -> None:
    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.resolve_target_project("   ", env={"FACTORY_PROJECT": "   "})


# --------------------------------------------------------------------------- R9/E9: learn() single mode

def test_learn_lands_a_lesson_and_active_check_in_the_named_project_with_proof_status_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)

    result = af_learn.learn(
        "the deploy script silently swallows a failed migration", project="other-repo-proj",
        drafted_run="pytest tests/test_deploy_migration.py -q", source="matt via af-learn",
    )

    assert result["lesson_id"] is not None
    assert result["check_id"] is not None
    assert result["proof_status"] in ("proven", "unproven")

    lesson_calls = _calls_for(calls, "lesson")
    check_calls = _calls_for(calls, "check")
    assert lesson_calls and lesson_calls[0]["space"] == _praxis.FACTORY_LEARNINGS_SPACE
    assert check_calls and check_calls[0]["space"] == "other-repo-proj"
    check_meta = check_calls[0]["body"]["meta"]
    assert check_meta["channel"] == "human"
    assert check_meta["proof_status"] in ("proven", "unproven")
    # DF4: lenient human insert lands GATING immediately regardless of proof outcome.
    assert check_meta[ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING


def test_learn_offers_ticket_regression_in_the_same_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)

    af_learn.learn("the login screen drops the session cookie on refresh", project="proj-x",
                   drafted_run="pytest tests/test_login_cookie.py -q", ticket_ids=["t1", "t2"])

    regress_calls = [c for c in calls if c["path"] == "/requirements/regress"]
    assert regress_calls and regress_calls[0]["body"]["ids"] == ["t1", "t2"]


def test_learn_with_no_drafted_check_still_lands_the_lesson_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)
    result = af_learn.learn("a bare complaint with nothing provable", project="proj-x")
    assert result["lesson_id"] is not None
    assert result["check_id"] is None
    assert all(c["body"].get("category") != "check" for c in calls if c["body"])


def test_learn_refuses_and_writes_nothing_when_project_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    calls: list[Any] = []
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: calls.append((a, kw)) or {})
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: calls.append(("ensure_space", a)) or a[0])

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn("a complaint with no named project", project=None, env={})

    assert calls == [], f"an unresolvable-project learn() call must write NOTHING: {calls}"


# --------------------------------------------------------------------------- R9/E10: learn_bulk()

def test_learn_bulk_inserts_every_requested_check_without_per_check_oversight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    calls = _recording_request(monkeypatch)

    results = af_learn.learn_bulk(
        [
            {"complaint_text": "complaint one", "drafted_run": "pytest tests/test_one.py -q"},
            {"complaint_text": "complaint two", "drafted_run": "pytest tests/test_two.py -q"},
            {"complaint_text": "complaint three"},  # no drafted check — lesson-only entry
        ],
        project="bulk-proj",
    )

    assert len(results) == 3
    assert all(r["lesson_id"] is not None for r in results)
    assert results[0]["check_id"] is not None
    assert results[1]["check_id"] is not None
    assert results[2]["check_id"] is None

    check_calls = _calls_for(calls, "check")
    assert len(check_calls) == 2
    assert all(c["body"]["meta"]["channel"] == "human" for c in check_calls)
    assert all(c["space"] == "bulk-proj" for c in check_calls)


def test_learn_bulk_refuses_and_writes_nothing_when_project_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authed(monkeypatch)
    calls: list[Any] = []
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: calls.append((a, kw)) or {})

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn_bulk(
            [{"complaint_text": "one"}, {"complaint_text": "two"}], project=None, env={},
        )

    assert calls == [], f"an unresolvable-project learn_bulk() call must write NOTHING: {calls}"


# --------------------------------------------------------------------------- R10: unproven -> proven upgrade

def test_unproven_gating_human_check_upgrades_to_proven_on_first_live_catch(store: _FakeStore) -> None:
    """FL11's new case: DF4 already lands a lenient human check as GATING even when unproven — the
    first REAL (non-drafting) pass must clear that unproven flag without touching enforcement
    state (it already gates). Distinct from FL12's existing REPORT_ONLY -> GATING machine-channel
    upgrade, which this must not regress (see test_ingestion_api_fl12.py)."""
    result = af_learn.learn("humans always review before shipping", project="proj-x",
                            drafted_run="curl -s http://internal/healthz")
    assert result["proof_status"] == "unproven"  # no proof_runner wired -> unproven per attempt_proof

    check_fact = store.facts[result["check_id"]]
    assert check_fact["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING

    upgraded = ingestion_api.upgrade_on_first_pass(check_fact["meta"]["check_id"], "proj-x", True)

    assert upgraded["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert upgraded["meta"]["proof_status"] == "proven"


def test_unproven_gating_human_check_is_a_no_op_on_a_failing_execution(store: _FakeStore) -> None:
    result = af_learn.learn("humans always review before shipping", project="proj-x",
                            drafted_run="curl -s http://internal/healthz")
    check_fact = store.facts[result["check_id"]]

    upgraded = ingestion_api.upgrade_on_first_pass(check_fact["meta"]["check_id"], "proj-x", False)

    assert upgraded["meta"]["proof_status"] == "unproven"


# --------------------------------------------------------------------------- E9: never writes cross-org

def test_learn_never_writes_to_the_shared_learnings_space_before_project_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stronger E9 check: even the FACTORY_LEARNINGS_SPACE (the one write every successful call
    always makes) must not be touched when the project cannot be resolved."""
    _authed(monkeypatch)
    written_spaces: list[str | None] = []

    def fake_request(method: str, path: str, *, space: str | None = None, **kw: Any) -> dict[str, Any]:
        written_spaces.append(space)
        return {}

    monkeypatch.setattr(_praxis, "_request", fake_request)

    with pytest.raises(af_learn.UnresolvableProjectSpace):
        af_learn.learn("orphan complaint", project=None, env={})

    assert _praxis.FACTORY_LEARNINGS_SPACE not in written_spaces
    assert written_spaces == []
