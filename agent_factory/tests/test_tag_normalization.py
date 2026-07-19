"""Locks tag NORMALIZATION across the check↔ticket seam — the fix for the silent case/whitespace
footgun where a check authored ``applies_to:["Auth"]`` would never pin onto a ticket tagged ``["auth"]``.

Three lanes, all in this one module (the MCP package IS importable from the agent_factory venv when the
repo root is on ``sys.path`` — this file adds it explicitly, mirroring how it adds ``hooks/`` — so the
mirror-agreement and write-path assertions live here alongside the resolve-side test rather than being
split into ``knowledge/mcp/tests``):

  1. RESOLVE-SIDE — ``_ticket_state.resolve_validation_requirements`` normalizes each ticket tag before
     the ``applies_to`` membership query, so a mixed-case / whitespace-padded ticket tag still resolves a
     check stored under the normalized value, and ``"*"`` still resolves onto every ticket.
  2. MIRROR AGREEMENT — the MCP write-path normalizer (``server._normalize_tag``) is byte-identical to
     the hook's canonical ``normalize_tag`` for a battery of inputs (they are kept in lockstep by hand).
  3. WRITE-PATH — ``server._normalize_applicability`` lowercases + dedups the applicability lanes at
     author time (``applies_to`` on a check, ``tags`` on a ticket), preserves ``"*"``, and leaves every
     other meta key untouched.

Fake ``_praxis`` (no network): ``facts_by`` mimics the server's exact array-membership match on
``meta.applies_to`` (``want in applies``), so the resolve lane is asserted deterministically.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

# Repo root on sys.path so the MCP write-path package imports (tests run from the agent_factory cwd,
# which does NOT put the repo root on the path); the hook subprocess itself is stdlib-only and never
# imports this, but the test may, to assert the two normalizers agree byte-for-byte.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import _ticket_state as ts  # noqa: E402
from _ticket_state import normalize_tag  # noqa: E402
from knowledge.mcp.server import _normalize_applicability, _normalize_tag  # noqa: E402


class _DBSpy:
    """A minimal in-memory checks DB whose ``facts_by`` mimics the server's EXACT array-membership match
    on ``meta.applies_to`` (``want in applies``) — the same predicate the real backend applies, so the
    normalization the resolve query does is what makes an otherwise-missed tag land."""

    def __init__(self, checks=None):
        self._checks = checks or []

    def facts_by(self, category=None, meta=None, state="active", space=None, snapshot=None):
        want = (meta or {}).get("applies_to")
        out = []
        for c in self._checks:
            applies = (c.get("meta") or {}).get("applies_to") or []
            if want is None or want in applies:   # EXACT membership — no normalization on the DB side
                out.append(c)
        return out

    def surface_checks(self, project, screen_id, scope=None, space=None, snapshot=None):
        return []

    def get_fact(self, cid):
        return {"id": cid, "text": cid, "meta": {}}


def _check(cid, applies_to, scope="validation"):
    return {"id": cid, "category": "check", "scope": scope,
            "meta": {"applies_to": applies_to, "scope": scope}}


def _install(monkeypatch, **kw):
    spy = _DBSpy(**kw)
    monkeypatch.setattr(ts, "_praxis", spy)
    return spy


# --------------------------------------------------------------------------- 1. resolve-side normalize

def test_mixed_case_ticket_tag_resolves_normalized_stored_check(monkeypatch):
    # Store is normalized ("auth"); the ticket's tag is mixed-case ("Auth"). Because resolve normalizes
    # the QUERY tag before the exact-membership facts_by, the check is caught — WITHOUT normalization the
    # query would be "Auth", which is not `in ["auth"]`, and the check would silently drop out.
    _install(monkeypatch, checks=[_check("auth-e2e", ["auth"])])
    ticket = {"id": "R1", "meta": {"tags": ["Auth"]}}
    got = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert got == {"auth-e2e"}


def test_whitespace_padded_tag_still_matches(monkeypatch):
    # A padded, upper-case tag ("  AUTH  ") normalizes to "auth" and matches the stored ["auth"] check —
    # the exact miss the normalizer exists to prevent.
    _install(monkeypatch, checks=[_check("auth-e2e", ["auth"])])
    ticket = {"id": "R2", "meta": {"tags": ["  AUTH  "]}}
    got = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert got == {"auth-e2e"}


def test_wildcard_still_resolves_onto_every_ticket(monkeypatch):
    # "*" is preserved verbatim by the normalizer, so the universal floor still resolves onto any ticket
    # (here one whose only tag is mixed-case) alongside its tag match.
    _install(monkeypatch, checks=[_check("floor", ["*"]), _check("auth-e2e", ["auth"])])
    ticket = {"id": "R3", "meta": {"tags": ["Auth"]}}
    got = {c["id"] for c in ts.resolve_validation_requirements(ticket, project="p", scope="validation")}
    assert got == {"floor", "auth-e2e"}


# --------------------------------------------------------------------------- 2. mirror agreement

def test_mcp_and_hook_normalizers_are_byte_identical():
    # The hook subprocess is stdlib-only and cannot import the MCP package, so the two normalizers are
    # hand-kept in lockstep; this asserts they agree for a battery including case, whitespace, "*", tabs,
    # and the empty string.
    for x in ("Auth", "  auth ", "*", "MixedCase", "tab\tspace", ""):
        assert _normalize_tag(x) == normalize_tag(x), x


# --------------------------------------------------------------------------- 3. write-path normalize

def test_normalize_applicability_lowercases_and_dedups_applies_to():
    out = _normalize_applicability({"applies_to": ["Auth", "auth"]})
    assert out["applies_to"] == ["auth"]     # lowercased AND deduped, order preserved


def test_normalize_applicability_preserves_wildcard():
    out = _normalize_applicability({"applies_to": ["*"]})
    assert out["applies_to"] == ["*"]


def test_normalize_applicability_normalizes_scalar_tags():
    out = _normalize_applicability({"tags": "Backend"})
    assert out["tags"] == "backend"          # scalar in -> scalar out, normalized


def test_normalize_applicability_leaves_other_keys_untouched():
    meta = {"applies_to": ["Auth", "auth"], "tags": "Backend",
            "scope": "validation", "verify": "Automated", "run": "pytest -q"}
    out = _normalize_applicability(meta)
    assert out["applies_to"] == ["auth"]
    assert out["tags"] == "backend"
    assert out["scope"] == "validation"       # untouched
    assert out["verify"] == "Automated"       # untouched (not an applicability lane)
    assert out["run"] == "pytest -q"          # untouched
