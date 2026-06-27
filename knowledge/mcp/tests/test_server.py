"""Offline tests for the MCP tool functions (httpx + identity mocked).

No real network or Cognito: ``identity.token``/``active_org``/``api_base`` are
monkeypatched and ``httpx.get``/``httpx.post`` are stubbed to capture the
request, so we assert the tools hit the right endpoint with Bearer +
X-Praxis-Org and surface the backend payload.
"""

import json

import httpx
import pytest

from knowledge.mcp import identity, server


def _extract_json(out: str) -> dict:
    """Pull the structured ```json block out of a tool's dual-format string."""
    block = out.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


def _patch_identity(monkeypatch):
    # Simulate a logged-in identity with an active org so the data tools' lazy
    # readiness guard (_not_ready) passes through to the HTTP call.
    monkeypatch.setattr(identity, "is_logged_in", lambda: True)
    monkeypatch.setattr(identity, "token", lambda: "id-tok")
    monkeypatch.setattr(identity, "active_org", lambda: "acme")
    monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")


def test_add_insight_posts_with_auth_and_returns_summary(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"summary": "added", "action": "add", "id": "x"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_add_insight("use uv, not pip", scope="global", category="constraint")

    # Structured output: a human summary line plus a consumable JSON block.
    assert "added" in out
    data = _extract_json(out)
    assert data["action"] == "add"
    assert data["id"] == "x"
    assert data["summary"] == "added"
    assert captured["url"] == "http://api.test/insights"
    assert captured["json"] == {
        "insight": "use uv, not pip",
        "onConflict": "auto_resolve",
        "raw": False,
        "scope": "global",
        "category": "constraint",
    }
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_add_insights_batch_posts_list_and_summarizes(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({
            "count": 2,
            "results": [
                {"ok": True, "id": "f1", "action": "added", "retrievable": True,
                 "contradictionsSurfaced": 0},
                {"ok": True, "id": "f2", "action": "added", "retrievable": True,
                 "contradictionsSurfaced": 0},
            ],
        })

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_add_insights([
        {"insight": "use uv, not pip", "category": "constraint"},
        {"insight": "deploy on Fridays", "scope": "ops"},
    ])

    assert "stored 2/2" in out
    data = _extract_json(out)
    assert data["count"] == 2
    assert [r["id"] for r in data["results"]] == ["f1", "f2"]
    assert captured["url"] == "http://api.test/insights/batch"
    assert captured["json"] == {
        "insights": [
            {"insight": "use uv, not pip", "category": "constraint"},
            {"insight": "deploy on Fridays", "scope": "ops"},
        ],
        "onConflict": "auto_resolve",
        "raw": False,
    }
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_add_insights_batch_rejects_empty_list(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx, "post", lambda *a, **k: pytest.fail("must not POST on empty list")
    )
    assert "non-empty list" in server.praxis_add_insights([])


def test_get_context_gets_with_auth_and_returns_context(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp({"context": "uv is the package manager here", "hits": []})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context("how do I install deps?", top_k=3)

    assert "uv is the package manager here" in out
    data = _extract_json(out)
    assert data["context"] == "uv is the package manager here"
    assert data["hits"] == []
    assert captured["url"] == "http://api.test/context"
    assert captured["params"] == {"query": "how do I install deps?", "top_k": 3}
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"


def test_ingest_posts_documents_with_auth(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({"results": [{"id": "f1", "action": "ingested"}], "count": 1})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_ingest("We deploy on Fridays.", source="handbook", state="active")

    assert captured["url"] == "http://api.test/ingest"
    assert captured["json"] == {
        "documents": [{"text": "We deploy on Fridays.", "source": "handbook"}],
        "state": "active",
        "onConflict": "auto_resolve",
    }
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    data = _extract_json(out)
    assert data["count"] == 1
    assert data["results"][0]["action"] == "ingested"


def test_add_insight_surface_mode_plumbs_on_conflict(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["json"] = json
        return _Resp(
            {
                "summary": "surfaced insight",
                "action": "surfaced",
                "id": "n1",
                "onConflict": "surface",
                "contradictionsSurfaced": 1,
            }
        )

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_add_insight("rate limit is 500 rps", on_conflict="surface")

    assert captured["json"]["onConflict"] == "surface"
    # The human line nudges the caller toward review when a contradiction is raised.
    assert "pending contradiction" in out
    data = _extract_json(out)
    assert data["onConflict"] == "surface"
    assert data["contradictionsSurfaced"] == 1


def test_add_insight_rejects_bad_on_conflict(monkeypatch):
    _patch_identity(monkeypatch)
    # Should never hit the network on a bad arg.
    monkeypatch.setattr(
        server.httpx, "post", lambda *a, **k: pytest.fail("must not POST on invalid on_conflict")
    )
    out = server.praxis_add_insight("x", on_conflict="bogus")
    assert "auto_resolve" in out and "surface" in out


def test_ingest_surface_mode_plumbs_on_conflict(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["json"] = json
        return _Resp({"results": [{"id": "f1", "action": "ingested", "surfaced": 1}], "count": 1})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    server.praxis_ingest("doc text", on_conflict="surface")
    assert captured["json"]["onConflict"] == "surface"


def test_ingest_session_posts_narrative_as_proposed(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp({
            "source": "session/abc123",
            "count": 2,
            "candidates": [
                {"id": "f1", "scope": "repo", "category": "convention"},
                {"id": "f2", "scope": "module:migrations", "category": "gotcha"},
            ],
        })

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_ingest_session("PROBLEM ...\nFIX ...")

    assert captured["url"] == "http://api.test/ingest/session"
    assert captured["json"] == {"narrative": "PROBLEM ...\nFIX ..."}  # no source key when omitted
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    assert "2 proposed candidate(s)" in out
    data = _extract_json(out)
    assert data["count"] == 2 and data["source"] == "session/abc123"


def test_ingest_session_includes_source_when_given(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers: captured.update(json=json)
        or _Resp({"source": "session/x", "count": 0, "candidates": []}),
    )
    server.praxis_ingest_session("n", source="session/x")
    assert captured["json"] == {"narrative": "n", "source": "session/x"}


def test_get_contradictions_formats_pairs(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, headers, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp(
            [
                {
                    "id": "a__b",
                    "status": "pending",
                    "slot": {"subject": "log level", "attribute": "verbosity"},
                    "members": [
                        {"id": "a", "content": "logs should be verbose", "state": "active"},
                        {"id": "b", "content": "logs should be terse", "state": "active"},
                    ],
                    "pairs": [
                        {
                            "id": "a__b",
                            "status": "pending",
                            "a": {"id": "a", "content": "logs should be verbose", "state": "active"},
                            "b": {"id": "b", "content": "logs should be terse", "state": "active"},
                        }
                    ],
                }
            ]
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_contradictions()

    assert captured["url"] == "http://api.test/contradictions"
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    assert "a__b" in out
    assert "logs should be verbose" in out and "logs should be terse" in out
    assert "id=a" in out and "id=b" in out


def test_get_contradictions_empty(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp([]))
    assert "No contradictions" in server.praxis_get_contradictions()


def test_resolve_contradiction_keep_ids(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"kept": [{"id": "a"}], "rejected": [{"id": "b"}]})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    # space- or comma-separated ids parse into a keep list (pick-a-winner is one id).
    out = server.praxis_resolve_contradiction("a__b", keep="a")
    assert captured["url"] == "http://api.test/contradictions/a__b/resolve"
    assert captured["json"] == {"keep": ["a"]}
    assert "a__b" in out

    server.praxis_resolve_contradiction("a__b__c", keep="a, b")
    assert captured["json"] == {"keep": ["a", "b"]}


def test_resolve_contradiction_keep_all_and_none(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(json=json) or _Resp({"ok": True}),
    )

    server.praxis_resolve_contradiction("a__b", keep="all")
    assert captured["json"] == {"keep": "all"}
    server.praxis_resolve_contradiction("a__b", keep="none")
    assert captured["json"] == {"keep": "none"}


def test_resolve_contradiction_custom_text(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(json=json) or _Resp({"ok": True}),
    )

    server.praxis_resolve_contradiction("a__b", custom_text="logs verbose in dev, terse in prod")

    assert captured["json"] == {"customText": "logs verbose in dev, terse in prod"}


def test_resolve_contradiction_requires_a_choice(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_resolve_contradiction("a__b")
    assert "keep" in out and "custom_text" in out


def test_list_graph_returns_all_facts_with_state_filter(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp(
            [
                {"id": "f1", "state": "active", "content": "use uv, not pip"},
                {"id": "f2", "state": "active", "title": "ci runs on push"},
            ]
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_list_graph(state="active")

    assert captured["url"] == "http://api.test/candidates"
    assert captured["params"] == {"state": "active"}
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    assert "id=f1" in out and "use uv, not pip" in out
    assert "id=f2" in out and "ci runs on push" in out


def test_list_graph_no_filter_sends_no_params(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers, timeout=None: captured.update(params=params) or _Resp([]),
    )
    out = server.praxis_list_graph()
    assert captured["params"] == {}
    assert "empty" in out.lower()


def test_insert_fact_posts_to_candidates(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"id": "new1", "state": "proposed"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_insert_fact("a title", "raw content", provenance="manual")

    assert captured["url"] == "http://api.test/candidates"
    assert captured["json"] == {
        "title": "a title",
        "content": "raw content",
        "provenance": "manual",
    }
    assert "new1" in out and "proposed" in out


def test_edit_fact_patches_only_given_fields(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_patch(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"id": "f1", "state": "active"})

    monkeypatch.setattr(server.httpx, "patch", fake_patch)

    out = server.praxis_edit_fact("f1", content="updated text")

    assert captured["url"] == "http://api.test/candidates/f1"
    assert captured["json"] == {"content": "updated text"}  # title/provenance omitted
    assert "f1" in out


def test_insert_fact_plumbs_category_meta_and_derived_from(monkeypatch):
    # Fix 2: a raw insert can carry the same structured data add_insight does, so
    # the manual-repair path no longer drops category/meta/derived_from.
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["json"] = json
        return _Resp({"id": "new1", "state": "proposed"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    server.praxis_insert_fact(
        "a title",
        "raw content",
        category="learning",
        meta={"requirement_id": "R4"},
        derived_from=["src1", "src2"],
    )

    assert captured["json"] == {
        "title": "a title",
        "content": "raw content",
        "category": "learning",
        "meta": {"requirement_id": "R4"},
        "derivedFrom": ["src1", "src2"],
    }


def test_edit_fact_plumbs_category_meta_and_derived_from(monkeypatch):
    # Fix 2: an edit can also set category/meta and attach derivation sources.
    _patch_identity(monkeypatch)
    captured = {}

    def fake_patch(url, json, headers, timeout=None):
        captured["json"] = json
        return _Resp({"id": "f1", "state": "active"})

    monkeypatch.setattr(server.httpx, "patch", fake_patch)

    server.praxis_edit_fact(
        "f1",
        category="requirement",
        meta={"slot": "auth"},
        derived_from=["src9"],
    )

    assert captured["json"] == {
        "category": "requirement",
        "meta": {"slot": "auth"},
        "derivedFrom": ["src9"],
    }


def test_edit_fact_requires_a_field(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_edit_fact("f1")
    assert "Nothing to edit" in out


def test_record_derivation_posts_edge(monkeypatch):
    # Fix 3: a direct way to attach derived_from edges between existing facts.
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"factId": "f1", "sourceIds": ["s1", "s2"], "kind": "derived_from"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_record_derivation("f1", ["s1", "s2"])

    assert captured["url"] == "http://api.test/derivations"
    assert captured["json"] == {"factId": "f1", "sourceIds": ["s1", "s2"]}
    assert "f1" in out and "s1" in out


def test_record_derivation_requires_sources(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx, "post", lambda *a, **k: pytest.fail("must not POST without sources")
    )
    out = server.praxis_record_derivation("f1", [])
    assert "non-empty" in out or "source_ids" in out


def test_promote_fact_posts_target_state(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_post(url, json, headers, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp({"id": "f1", "state": "active"})

    monkeypatch.setattr(server.httpx, "post", fake_post)

    out = server.praxis_promote_fact("f1", target_state="active")
    assert captured["url"] == "http://api.test/candidates/f1/promote"
    assert captured["json"] == {"targetState": "active"}
    assert "f1" in out and "active" in out


def test_promote_fact_without_target_sends_empty_body(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(json=json) or _Resp({"id": "f1", "state": "active"}),
    )
    server.praxis_promote_fact("f1")
    assert captured["json"] == {}


def test_reject_fact_posts_reason(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(url=url, json=json)
        or _Resp({"id": "f1", "state": "rejected"}),
    )
    out = server.praxis_reject_fact("f1", reason="stale")
    assert captured["url"] == "http://api.test/candidates/f1/reject"
    assert captured["json"] == {"reason": "stale"}
    assert "rejected" in out


def test_delete_fact_issues_delete(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "delete",
        lambda url, headers, timeout=None: captured.update(url=url, headers=headers) or _Resp({"deleted": "f1"}),
    )
    out = server.praxis_delete_fact("f1")
    assert captured["url"] == "http://api.test/candidates/f1"
    assert captured["headers"]["Authorization"] == "Bearer id-tok"
    assert "f1" in out


def test_delete_fact_conflict_reports_reason(monkeypatch):
    _patch_identity(monkeypatch)

    class _RespText(_Resp):
        text = "fact is referenced"

    monkeypatch.setattr(
        server.httpx, "delete", lambda url, headers, timeout=None: _RespText({}, status_code=409)
    )
    out = server.praxis_delete_fact("f1")
    assert "Cannot delete" in out and "referenced" in out


def test_clear_graph_posts_and_reports_count(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, headers, timeout=None: captured.update(url=url) or _Resp({"cleared": 7}),
    )
    out = server.praxis_clear_graph()
    assert captured["url"] == "http://api.test/graph/clear"
    assert "7" in out


def test_list_snapshots_formats_entries(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, headers, timeout=None: captured.update(url=url)
        or _Resp({"snapshots": [{"name": "wip", "count": 5, "createdAt": "2026-06-24"}]}),
    )
    out = server.praxis_list_snapshots()
    assert captured["url"] == "http://api.test/snapshots"
    assert "wip" in out and "5 node" in out


def test_list_snapshots_empty(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp({"snapshots": []}))
    assert "No snapshots" in server.praxis_list_snapshots()


def test_save_snapshot_posts_name(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(url=url, json=json)
        or _Resp({"name": "wip", "count": 5}),
    )
    out = server.praxis_save_snapshot("  wip  ")
    assert captured["url"] == "http://api.test/snapshots"
    assert captured["json"] == {"name": "wip"}  # trimmed
    assert "wip" in out and "5 node" in out


def test_save_snapshot_rejects_blank_name(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_save_snapshot("   ")
    assert "non-empty" in out


def test_load_snapshot_posts_mode(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(url=url, json=json)
        or _Resp({"loaded": 5, "mode": "add"}),
    )
    out = server.praxis_load_snapshot("wip", mode="add")
    assert captured["url"] == "http://api.test/snapshots/wip/load"
    assert captured["json"] == {"mode": "add"}
    assert "5 node" in out and "add" in out


def test_load_snapshot_defaults_to_replace(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(json=json) or _Resp({"loaded": 1, "mode": "replace"}),
    )
    server.praxis_load_snapshot("wip")
    assert captured["json"] == {"mode": "replace"}


def test_load_snapshot_rejects_bad_mode(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_load_snapshot("wip", mode="merge")
    assert "add" in out and "replace" in out


def test_load_snapshot_unknown_is_friendly(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx, "post", lambda url, json, headers, timeout=None: _Resp({}, status_code=404)
    )
    out = server.praxis_load_snapshot("nope")
    assert "Unknown snapshot" in out


def test_delete_snapshot_issues_delete(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "delete",
        lambda url, headers, timeout=None: captured.update(url=url) or _Resp({"deleted": "wip"}),
    )
    out = server.praxis_delete_snapshot("wip")
    assert captured["url"] == "http://api.test/snapshots/wip"
    assert "wip" in out


def test_list_org_sources_formats(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, headers, timeout=None: _Resp(
            {
                "sources": [
                    {
                        "userId": "u1",
                        "username": "me@x.com",
                        "role": "owner",
                        "isSelf": True,
                        "snapshots": [{"name": "wip", "count": 3}],
                    }
                ]
            }
        ),
    )
    out = server.praxis_list_org_sources()
    assert "u1" in out and "me@x.com" in out and "wip" in out and "(you)" in out


def test_browse_snapshot_structured(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, headers, timeout=None: captured.update(url=url)
        or _Resp(
            {
                "userId": "u1",
                "snapshot": "wip",
                "groups": [{"key": "backend", "label": "backend", "facts": [{"id": "f1", "text": "x"}]}],
            }
        ),
    )
    out = server.praxis_browse_snapshot("u1", "wip")
    assert captured["url"] == "http://api.test/org/sources/u1/snapshots/wip/facts"
    data = _extract_json(out)
    assert data["groups"][0]["facts"][0]["id"] == "f1"


def test_fold_in_posts_selection(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(url=url, json=json)
        or _Resp({"folded": 2, "deduped": 1, "conflicts": [], "mode": "add"}),
    )
    out = server.praxis_fold_in("u1", "wip", ["f1", "f2"], mode="add")
    assert captured["url"] == "http://api.test/fold-in"
    assert captured["json"] == {
        "sourceUser": "u1",
        "snapshot": "wip",
        "factIds": ["f1", "f2"],
        "mode": "add",
    }
    data = _extract_json(out)
    assert data["folded"] == 2 and data["deduped"] == 1


def test_fold_in_requires_fact_ids(monkeypatch):
    _patch_identity(monkeypatch)
    out = server.praxis_fold_in("u1", "wip", [])
    assert "fact_ids" in out


def test_list_mounts_formats(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, headers, timeout=None: _Resp(
            {"mounts": [{"sourceUser": "u1", "snapshot": "wip", "isSelf": False, "count": 4}]}
        ),
    )
    out = server.praxis_list_mounts()
    assert "wip" in out and "from u1" in out and "4 node" in out


def test_list_mounts_empty(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp({"mounts": []}))
    assert "No snapshots are mounted" in server.praxis_list_mounts()


def test_mount_snapshot_posts_self_by_default(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(url=url, json=json)
        or _Resp({"sourceUser": "dev", "snapshot": "wip", "mounted": True}),
    )
    out = server.praxis_mount_snapshot("wip")
    assert captured["url"] == "http://api.test/mounts"
    assert captured["json"] == {"snapshot": "wip"}  # no sourceUser => defaults to self
    assert "Mounted" in out and "wip" in out


def test_mount_snapshot_posts_source_user(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: captured.update(json=json)
        or _Resp({"sourceUser": "u1", "snapshot": "wip", "mounted": True}),
    )
    server.praxis_mount_snapshot("wip", source_user="u1")
    assert captured["json"] == {"snapshot": "wip", "sourceUser": "u1"}


def test_mount_snapshot_unknown_is_friendly(monkeypatch):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        server.httpx, "post", lambda url, json, headers, timeout=None: _Resp({}, status_code=404)
    )
    out = server.praxis_mount_snapshot("nope")
    assert "Unknown member or snapshot" in out


def test_unmount_snapshot_sends_delete_with_body(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_request(method, url, json, headers, timeout=None):
        captured.update(method=method, url=url, json=json)
        return _Resp({"sourceUser": "dev", "snapshot": "wip", "mounted": False})

    monkeypatch.setattr(server.httpx, "request", fake_request)
    out = server.praxis_unmount_snapshot("wip")
    assert captured["method"] == "DELETE"
    assert captured["url"] == "http://api.test/mounts"
    assert captured["json"] == {"snapshot": "wip"}
    assert "Unmounted" in out


def test_mount_requires_name(monkeypatch):
    _patch_identity(monkeypatch)
    assert "snapshot name" in server.praxis_mount_snapshot("  ")
    assert "snapshot name" in server.praxis_unmount_snapshot("  ")


def test_data_tool_when_not_logged_in_guides_to_login(monkeypatch):
    monkeypatch.setattr(identity, "is_logged_in", lambda: False)
    out = server.praxis_get_context("anything")
    assert "praxis_login" in out and "not logged in" in out.lower()


def test_mcp_auth_disabled_skips_login_gate(monkeypatch):
    # The MCP client bypass (distinct from the backend's PRAXIS_AUTH_DISABLED) lets
    # the tools run with no cached login, sending X-Praxis-Org and no bearer token.
    monkeypatch.setenv("PRAXIS_MCP_AUTH_DISABLED", "1")
    monkeypatch.setenv("PRAXIS_MCP_ORG", "myorg")
    monkeypatch.setattr(identity, "is_logged_in", lambda: False)
    assert server._not_ready() is None
    assert server._headers() == {"X-Praxis-Org": "myorg"}


def test_mcp_auth_disabled_defaults_org(monkeypatch):
    monkeypatch.setenv("PRAXIS_MCP_AUTH_DISABLED", "1")
    monkeypatch.delenv("PRAXIS_MCP_ORG", raising=False)
    assert server._headers() == {"X-Praxis-Org": "default"}


def test_api_base_from_env_when_mcp_auth_disabled(monkeypatch):
    # In bypass mode api_base() must not require a cached login.
    monkeypatch.setenv("PRAXIS_MCP_AUTH_DISABLED", "1")
    monkeypatch.setenv("PRAXIS_API_BASE_URL", "http://here:9000")

    def _boom():
        raise AssertionError("load_identity should not be called in bypass mode")

    monkeypatch.setattr(identity, "load_identity", _boom)
    assert identity.api_base() == "http://here:9000"


def test_praxis_login_auto_selects_single_org(monkeypatch):
    from knowledge.mcp.identity import Tenant

    tenant = Tenant("rt", "sub-1", "me@x.com", "acme", "http://api.test")
    monkeypatch.setattr(identity, "authenticate", lambda e, p: (tenant, [{"orgId": "acme"}]))
    out = server.praxis_login("me@x.com", "pw")
    assert "acme" in out and "me@x.com" in out


def test_write_uses_long_timeout_and_read_short(monkeypatch):
    # The conflict-checked write path can run an inline LLM judge, so writes get a
    # generous client timeout while reads stay snappy. Capture each call's timeout.
    _patch_identity(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        server.httpx,
        "post",
        lambda url, json, headers, timeout=None: seen.update(write=timeout)
        or _Resp({"summary": "added"}),
    )
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers, timeout=None: seen.update(read=timeout)
        or _Resp({"context": "", "hits": []}),
    )
    server.praxis_add_insight("X is A")
    server.praxis_get_context("X")
    assert seen["write"] == server._WRITE_TIMEOUT
    assert seen["read"] == server._READ_TIMEOUT
    assert seen["write"] > seen["read"]


def test_add_insight_timeout_returns_clear_note(monkeypatch):
    # A client-side timeout on a write must not look like a server failure: tell the
    # caller the write may have committed and to read it back.
    _patch_identity(monkeypatch)

    def fake_post(url, json, headers, timeout=None):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(server.httpx, "post", fake_post)
    out = server.praxis_add_insight("X is A", on_conflict="surface")
    assert "may still have committed" in out
    assert "praxis_list_graph" in out


def test_auth_failure_maps_to_friendly_message(monkeypatch):
    _patch_identity(monkeypatch)

    def fake_get(url, params, headers, timeout=None):
        return _Resp({}, status_code=403)

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context("anything")
    assert "login" in out.lower()


def test_facts_by_sends_filters_and_json_meta(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Resp({"facts": [{"id": "c1", "category": "check"}]})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_facts_by(
        category="check",
        state="active",
        meta_filter={"scope": "validation", "applies_to": "auth"},
    )

    assert captured["url"] == "http://api.test/facts/by"
    assert captured["params"]["category"] == "check"
    assert captured["params"]["state"] == "active"
    # meta_filter is JSON-encoded into a single query param.
    assert json.loads(captured["params"]["meta"]) == {
        "scope": "validation",
        "applies_to": "auth",
    }
    assert captured["headers"]["X-Praxis-Org"] == "acme"
    data = _extract_json(out)
    assert data["facts"] == [{"id": "c1", "category": "check"}]


def test_facts_by_omits_meta_param_when_unset(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers, timeout=None: captured.update(params=params)
        or _Resp({"facts": []}),
    )
    out = server.praxis_facts_by(source="prd-app")
    assert "meta" not in captured["params"]
    assert captured["params"]["source"] == "prd-app"
    assert "No facts match" in out


def test_checks_for_surface_sends_project_and_scope(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp({"checks": [{"id": "c1"}, {"id": "c2"}]})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_checks_for_surface("demo", "s-home", scope="validation")

    assert captured["url"] == "http://api.test/surfaces/s-home/checks"
    assert captured["params"] == {"project": "demo", "scope": "validation"}
    data = _extract_json(out)
    assert {c["id"] for c in data["checks"]} == {"c1", "c2"}


def test_checks_for_surface_omits_scope_when_unset(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers, timeout=None: captured.update(params=params)
        or _Resp({"checks": []}),
    )
    out = server.praxis_checks_for_surface("demo", "s-home")
    assert captured["params"] == {"project": "demo"}
    assert "No checks" in out


def test_get_context_plumbs_positive_filters(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}

    def fake_get(url, params, headers, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp({"context": "ctx", "hits": [{"id": "c1", "category": "check"}]})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    out = server.praxis_get_context(
        "a part",
        category="check",
        categories=["check", "requirement"],
        scope="mvp",
        meta_filter={"scope": "planning"},
    )

    assert captured["url"] == "http://api.test/context"
    p = captured["params"]
    assert p["query"] == "a part"
    assert p["category"] == "check"
    assert p["categories"] == "check,requirement"  # list -> CSV
    assert p["scope"] == "mvp"
    assert json.loads(p["meta"]) == {"scope": "planning"}  # dict -> JSON string
    data = _extract_json(out)
    assert data["hits"] == [{"id": "c1", "category": "check"}]


def test_get_context_omits_filter_params_when_unset(monkeypatch):
    _patch_identity(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.httpx,
        "get",
        lambda url, params, headers, timeout=None: captured.update(params=params)
        or _Resp({"context": "", "hits": []}),
    )
    server.praxis_get_context("just a query")
    # Parity: no positive-filter params are sent when none are passed.
    assert captured["params"] == {"query": "just a query", "top_k": 8}
