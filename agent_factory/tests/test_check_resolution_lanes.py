"""Locks the check-resolution lanes in ``hooks/_ticket_state.py``:

  * MANDATORY (precise) = tag ∪ ``"*"`` wildcard ∪ surface — the coverage contract. A universal
    ``applies_to:["*"]`` check resolves onto EVERY ticket (the wildcard bug fix); a tag-scoped check
    lands ONLY on tickets carrying that tag (so a frontend/notifications check never resolves onto an
    unrelated backend ticket).
  * ADVISORY (semantic) = ``retrieve_advisory_checks`` — hybrid retrieval against the ticket text.
    Candidates only; NEVER part of the mandatory set, so an irrelevant retrieval can't gate completion.

Fake ``_praxis`` (no network): ``facts_by`` honors the ``applies_to`` membership filter the server
applies, and ``context`` returns canned hits, so the lanes are asserted deterministically.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class _DBSpy:
    """A minimal in-memory checks DB. ``facts_by`` mimics the server's array-membership match on
    ``meta.applies_to``; ``context`` returns canned semantic hits."""

    def __init__(self, checks=None, hits=None):
        self._checks = checks or []
        self._hits = hits or []
        self.context_calls = []

    def facts_by(self, category=None, meta=None, state="active", space=None):
        want = (meta or {}).get("applies_to")
        out = []
        for c in self._checks:
            applies = (c.get("meta") or {}).get("applies_to") or []
            if want is None or want in applies:
                out.append(c)
        return out

    def surface_checks(self, project, screen_id, scope=None, space=None):
        return []

    def context(self, query, top_k=10, as_of=None, space=None):
        self.context_calls.append({"query": query, "space": space, "top_k": top_k})
        return list(self._hits)

    def get_fact(self, cid):
        return {"id": cid, "text": cid, "meta": {}}


def _check(cid, applies_to, scope="validation"):
    return {"id": cid, "category": "check", "scope": scope,
            "meta": {"applies_to": applies_to, "scope": scope}}


def _install(monkeypatch, **kw):
    spy = _DBSpy(**kw)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


# --------------------------------------------------------------------------- mandatory: wildcard

def test_universal_wildcard_check_resolves_onto_every_ticket(monkeypatch):
    # A ["*"] floor gate must resolve even though the ticket's only tag is "auth" — the bug fix.
    _install(monkeypatch, checks=[
        _check("floor-typecheck", ["*"]),
        _check("auth-e2e", ["auth"]),
        _check("notif-e2e", ["notifications"]),
    ])
    ticket = {"id": "R1", "meta": {"tags": ["auth"]}}
    got = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert got == {"floor-typecheck", "auth-e2e"}     # universal + tag-matched
    assert "notif-e2e" not in got                       # unrelated tag never resolves


def test_wildcard_resolves_onto_a_tagless_ticket(monkeypatch):
    _install(monkeypatch, checks=[_check("floor-typecheck", ["*"]), _check("auth-e2e", ["auth"])])
    ticket = {"id": "R2", "meta": {}}                   # no tags, no surfaces (a bare backend ticket)
    got = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert got == {"floor-typecheck"}                   # only the universal floor, nothing tag-scoped


def test_frontend_tag_check_does_not_resolve_onto_backend_ticket(monkeypatch):
    # A UI check scoped by a real tag lands only on tickets carrying it — a backend ticket is clean.
    _install(monkeypatch, checks=[_check("playwright-e2e", ["screen"]), _check("floor", ["*"])])
    backend = {"id": "R3", "meta": {"tags": ["rollup", "backend"]}}
    got = {c["id"] for c in ts.resolve_validation_requirements(backend, project="p", scope="validation")}
    assert got == {"floor"}                             # no playwright on a backend ticket


# --------------------------------------------------------------------------- advisory: semantic lane

def test_semantic_lane_is_advisory_not_in_mandatory(monkeypatch):
    # The semantic hit is NOT returned by the mandatory resolve...
    spy = _install(monkeypatch,
                   checks=[_check("floor", ["*"])],
                   hits=[_check("sem-suggested", ["something"])])
    ticket = {"id": "R4", "text": "aggregate participation", "meta": {"acceptance": "rollup is correct"}}
    mandatory = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert mandatory == {"floor"}
    assert "sem-suggested" not in mandatory
    # ...but IS surfaced as an advisory candidate, read from the coding-validation checks-space.
    advisory = {c["id"] for c in ts.retrieve_advisory_checks(ticket, project="p", scope="validation")}
    assert advisory == {"sem-suggested"}
    assert spy.context_calls and spy.context_calls[0]["space"] == "coding-validation"
    assert "participation" in spy.context_calls[0]["query"]  # queried by the ticket's own text


def test_advisory_filters_non_check_and_cross_scope_hits(monkeypatch):
    _install(monkeypatch, hits=[
        {"id": "a-check", "category": "check", "scope": "validation", "meta": {}},
        {"id": "a-req", "category": "requirement", "scope": "validation", "meta": {}},   # not a check
        {"id": "wrong-scope", "category": "check", "scope": "planning", "meta": {}},      # cross-scope
    ])
    ticket = {"id": "R5", "text": "some ticket", "meta": {}}
    got = {c["id"] for c in ts.retrieve_advisory_checks(ticket, project="p", scope="validation")}
    assert got == {"a-check"}


def test_advisory_empty_when_ticket_has_no_text(monkeypatch):
    spy = _install(monkeypatch, hits=[_check("sem", ["x"])])
    ticket = {"id": "R6", "meta": {}}                   # no text, no acceptance -> no semantic query
    assert ts.retrieve_advisory_checks(ticket, project="p", scope="validation") == []
    assert spy.context_calls == []                       # never issues a blind full-scan query
