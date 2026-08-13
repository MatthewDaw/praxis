"""Shared test fixtures — the OFFLINE judge harness (U4 of the universal-minimalism-gate plan).

Once a universal GRADED check gates, every test that drives a ticket to ``finished`` needs an LLM
``Complete`` judge that does not exist offline. This harness supplies deterministic stub judges so CI
has a judge with no API key: a pass judge (scores every declared axis at the ceiling, no defects) and
a fail judge (a located defect above any floor). The universal check ships ``report_only=true`` — so
it does NOT gate and existing ticket-to-finished tests keep passing WITHOUT a judge; these stubs
prove the pipeline works when a universal is flipped to gating (and cover the graded VERIFY path).

The stub parses axis names out of the real judge prompt, so it grades any rubric correctly.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from hooks import _praxis

_AXIS_LINE = re.compile(r"^\s*-\s+(.+?)\s+\(pass threshold", re.MULTILINE)


def _axes_in_prompt(prompt: str) -> list[str]:
    """The axis names build_judge_prompt listed, so a stub grades any rubric it is handed."""
    return _AXIS_LINE.findall(prompt)


def stub_pass_judge(prompt: str) -> str:
    """A deterministic judge that PASSES: every declared axis at 1.0, no located defects."""
    return json.dumps({"axis_scores": {name: 1.0 for name in _axes_in_prompt(prompt)}, "defects": []})


def stub_fail_judge(prompt: str) -> str:
    """A deterministic judge that FAILS: a maximally-confident located defect on the first axis."""
    axes = _axes_in_prompt(prompt)
    return json.dumps({
        "axis_scores": {name: 0.1 for name in axes},
        "defects": [{"file": "m.py", "line": 1, "problem": "duplicated logic",
                     "remedy": "consolidate", "confidence": 10}],
    })


@pytest.fixture
def pass_judge():
    """The offline pass judge as a ``Complete`` callable."""
    return stub_pass_judge


@pytest.fixture
def fail_judge():
    """The offline fail judge as a ``Complete`` callable."""
    return stub_fail_judge


# --------------------------------------------------------------------------- shared ingestion-API test doubles
# The ONE double for "is this caller org-authenticated" (`_praxis.whoami`) plus the ONE double for
# "record every `_praxis._request` write" — every ingestion_api/af_learn test needs one or both, so
# they live here rather than being redefined per test module (three near-identical copies were
# found and consolidated: test_ingestion_api_fl2.py, test_ingestion_api_fl12.py, test_af_learn.py).

class WhoAmIStub:
    def __init__(self, ok: bool, principal: str = "user-1", detail: str = "") -> None:
        self.ok = ok
        self.principal = principal
        self.detail = detail


def authed(monkeypatch: pytest.MonkeyPatch, principal: str = "user-1") -> None:
    """Stub ``_praxis.whoami`` as a successfully authenticated caller."""
    monkeypatch.setattr(_praxis, "whoami", lambda: WhoAmIStub(True, principal))


def unauthed(monkeypatch: pytest.MonkeyPatch, detail: str = "not logged in") -> None:
    """Stub ``_praxis.whoami`` as an UNauthenticated caller (R1b refusal tests)."""
    monkeypatch.setattr(_praxis, "whoami", lambda: WhoAmIStub(False, "?", detail))


def recording_request(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A ``_praxis._request`` double that records every call and hands back a stable fake id — for
    a test that only needs to observe WHAT was written, never read it back afterward. (A test that
    needs a stateful read-back too — e.g. an ``upgrade_on_first_pass`` lookup against a check
    ``ingest`` just wrote — wants :class:`FakeCheckStore`/``check_store`` instead.)"""
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


def calls_for(calls: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    """The subset of recorded ``/insights`` calls whose body is the given fact ``category``."""
    return [c for c in calls if c["body"] and c["body"].get("category") == category]


class FakeCheckStore:
    """A minimal in-memory double for a project's ``building-validation`` snapshot: enough of
    ``_praxis``'s surface (``_request``, ``facts_by``, ``patch_meta``) for an ingest-then-read-back
    round trip (e.g. ``ingest`` writing a check, then ``upgrade_on_first_pass`` fetching + patching
    the SAME check by its ``check_id`` meta) — plus a call log so a test can assert no delete-shaped
    call was ever made."""

    def __init__(self) -> None:
        self.facts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def seed_check(self, check_id: str, meta: dict[str, Any]) -> str:
        """Seed one check whose STORAGE id is deliberately DIFFERENT from its AUTHORED
        ``meta.check_id``.

        Praxis mints its own fact id on write, so those two identifiers never coincide in
        production — and the whole ``check_id`` contract (every lifecycle verb resolves its
        argument through ``_fetch_check``, which queries ``meta={"check_id": ...}``) is only
        testable when they differ. Seeding ``id == check_id``, as this double used to, made a verb
        handed the WRONG identifier look like it worked. Use :meth:`check` to read one back the way
        production looks it up. Returns the storage id."""
        fid = f"fact-{check_id}"
        self.facts[fid] = {"id": fid, "category": "check",
                          "meta": {"check_id": check_id, **dict(meta)}}
        return fid

    def check(self, check_id: str) -> dict[str, Any]:
        """Look a check up by AUTHORED ``meta.check_id`` — the only identifier production resolves
        by. Raises ``KeyError`` when nothing carries it, so a test cannot accidentally assert
        against a storage id."""
        for fact in self.facts.values():
            if fact["category"] == "check" and fact["meta"].get("check_id") == check_id:
                return fact
        raise KeyError(f"no check with meta.check_id={check_id!r}")

    def seed_lesson(self, lesson_id: str, meta: dict[str, Any] | None = None) -> None:
        self.facts[lesson_id] = {"id": lesson_id, "category": "lesson", "meta": dict(meta or {})}

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
               space: str | None = None, snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, path))
        if method == "POST" and path == "/insights":
            self._n += 1
            fid = f"fake-{self._n}"
            self.facts[fid] = {"id": fid, "category": (body or {}).get("category"),
                              "source": (body or {}).get("source"),
                              "meta": dict((body or {}).get("meta") or {})}
            return {"id": fid, "action": "added"}
        if method == "POST" and path == "/requirements/regress":
            return {"count": len((body or {}).get("ids", []))}
        return {}

    def get_fact(self, cid: str, *, space: str | None = None,
                snapshot: str | None = None, not_found_ok: bool = False) -> dict[str, Any]:
        self.calls.append(("GET", cid))
        return self.facts.get(cid, {})

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
        self.calls.append(("PATCH", cid))
        self.facts[cid]["meta"].update(meta_dict)
        return self.facts[cid]


@pytest.fixture
def check_store(monkeypatch: pytest.MonkeyPatch) -> FakeCheckStore:
    """A fresh :class:`FakeCheckStore`, wired onto ``_praxis`` and pre-authenticated."""
    st = FakeCheckStore()
    authed(monkeypatch)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    monkeypatch.setattr(_praxis, "_request", st.request)
    monkeypatch.setattr(_praxis, "facts_by", st.facts_by)
    monkeypatch.setattr(_praxis, "patch_meta", st.patch_meta)
    monkeypatch.setattr(_praxis, "get_fact", st.get_fact)
    return st


@pytest.fixture(autouse=True)
def _allow_dirty_finish(monkeypatch):
    """Neutralize ``release``'s clean-worktree guard for the unit suite.

    A real per-ticket worker finishes inside its OWN git worktree, so ``release(state="finished")``
    refuses while that tree is dirty (uncommitted work is invisible to the orchestrator's merge).
    Unit tests are not worktrees — they run from whatever checkout pytest was invoked in, which is
    routinely dirty during development — so without this every ticket-to-finished test would flip
    red on an unrelated edit. Tests that assert the guard itself delete this var explicitly.
    """
    monkeypatch.setenv("AF_ALLOW_DIRTY_FINISH", "1")
