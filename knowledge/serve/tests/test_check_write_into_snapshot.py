"""Authoring a CHECK into a section-locked org-shared snapshot, end to end.

``af-intake-build-validation`` is the sole writer of ``building-validation`` and
``af-intake-plan-validation`` the sole writer of ``planning-validation``; both author through
``POST /insights`` with ``category="check"`` and the ``(space, snapshot)`` header pair. A report
claimed that combination had regressed to a 500 while the same payload succeeded against working
memory — i.e. that the ONLY way to author a build gate was broken. It reproduced as healthy, but
nothing in the suite actually pinned the combination, so the claim could not be answered from the
tests. These do that.

The assertions deliberately end at ``facts_by`` with the SAME ``(space, snapshot)`` af-build's
RESOLVE query uses: landing the row is not the property that matters, being found by the reader is.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "proj"


@pytest.fixture
def client(request, monkeypatch):
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", FakeEmbedder)

    org = "test_" + request.node.name
    db.bootstrap()
    conn = db.connect()

    def _clean():
        for t in ("fact_edges", "facts", "snapshots", "org_members", "orgs", "spaces"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE org_id = %s", (org,))
            except Exception:
                pass

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)
    yield TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    _clean()


def _h(snapshot: str) -> dict[str, str]:
    return {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": snapshot}


def _check(check_id: str, scope: str, **meta) -> dict:
    return {
        "insight": f"criterion prose for {check_id}",
        "category": "check",
        "scope": scope,
        "source": f"prd-{PROJECT}",
        "meta": {"check_id": check_id, "applies_to": ["every-site"], **meta},
    }


def test_check_lands_in_building_validation_and_is_readable_by_resolve(client):
    """The exact shape af-intake-build-validation authors, read back the way af-build resolves."""
    run = "test -f backend/src/logic/provider/restricted-record-manifest.ts"
    res = client.post("/insights", json=_check("c-manifest", "validation", run=run),
                      headers=_h("building-validation"))
    assert res.status_code == 200, res.text
    assert res.json()["action"] == "added"

    found = client.get("/facts/by", params={"category": "check", "state": "any"},
                       headers=_h("building-validation")).json()
    rows = found if isinstance(found, list) else found.get("facts", found)
    hit = [f for f in rows if (f.get("meta") or {}).get("check_id") == "c-manifest"]
    assert len(hit) == 1
    # The `run` command IS the check — a gate that lost it silently passes forever.
    assert hit[0]["meta"]["run"] == run


def test_a_complex_run_command_survives_verbatim(client):
    """The reported payload used $VARS, $(...) and nested quotes, and shell metacharacters were a
    prime suspect for the 500. They must round-trip byte-identical: a mangled `run` is a gate that
    still reports green."""
    run = (
        'M=backend/src/logic/provider/restricted-record-manifest.ts; test -f $M || exit 1; '
        'for c in $(find backend/src/routes/provider -name "*.controller.ts" '
        "-exec basename {} .controller.ts ';'); do grep -q $c $M || exit 1; done"
    )
    res = client.post("/insights", json=_check("c-sweep", "validation", run=run),
                      headers=_h("building-validation"))
    assert res.status_code == 200, res.text
    got = client.get(f"/candidates/{res.json()['id']}", headers=_h("building-validation")).json()
    assert got["meta"]["run"] == run


def test_reauthoring_the_same_check_id_updates_in_place(client):
    """Identity-keyed on check_id: a re-author amends the one gate rather than minting a rival,
    which is what makes the intake command idempotent and re-runnable."""
    first = client.post("/insights", json=_check("c-dup", "validation", run="echo v1"),
                        headers=_h("building-validation")).json()
    second = client.post("/insights", json=_check("c-dup", "validation", run="echo v2"),
                         headers=_h("building-validation")).json()
    assert second["action"] == "updated" and second["id"] == first["id"]

    rows = client.get("/facts/by", params={"category": "check", "state": "any"},
                      headers=_h("building-validation")).json()
    rows = rows if isinstance(rows, list) else rows.get("facts", rows)
    same = [f for f in rows if (f.get("meta") or {}).get("check_id") == "c-dup"]
    assert len(same) == 1 and same[0]["meta"]["run"] == "echo v2"


def test_planning_validation_lens_uses_the_same_path(client):
    """The sibling section lock — a planning lens carries no `run` (it is declarative), so a
    check-shaped assumption about `run` must not make it unwritable."""
    res = client.post("/insights", json=_check("l-recovery", "planning"),
                      headers=_h("planning-validation"))
    assert res.status_code == 200, res.text
    rows = client.get("/facts/by", params={"category": "check", "state": "any"},
                      headers=_h("planning-validation")).json()
    rows = rows if isinstance(rows, list) else rows.get("facts", rows)
    assert any((f.get("meta") or {}).get("check_id") == "l-recovery" for f in rows)


def test_check_write_without_a_snapshot_target_is_refused_with_a_reason(client):
    """A check written to working memory is invisible to af-build, so the server refuses it — and
    the refusal must SAY so. This is the case the reporter's control write exercised, where a 200
    would have been the silent failure."""
    res = client.post("/insights", json=_check("c-nowhere", "validation", run="true"))
    assert res.status_code == 400
    assert "snapshot" in res.json()["detail"].lower()


def test_unhandled_errors_return_a_named_detail_not_a_bare_500(client, monkeypatch):
    """A 500 must name its failure. Two separate multi-hour investigations were spent localizing
    errors whose bodies were empty, so an unnamed 500 is itself the defect worth pinning."""
    from fastapi.testclient import TestClient

    from knowledge.serve import app as app_module

    def _boom(*a, **k):
        raise RuntimeError("synthetic failure for the error-body contract")

    monkeypatch.setattr(app_module, "_check_upsert", _boom)
    # raise_server_exceptions=False makes TestClient behave like a real HTTP client: it returns
    # the 500 RESPONSE instead of re-raising the exception in the test process. The default would
    # assert on the exception object, which is precisely the thing a remote caller never sees --
    # and never seeing it is the defect under test.
    raw = TestClient(client.app, headers=dict(client.headers), raise_server_exceptions=False)
    res = raw.post("/insights", json=_check("c-boom", "validation", run="true"),
                   headers=_h("building-validation"))
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert "RuntimeError" in detail
    assert "synthetic failure for the error-body contract" in detail
    assert "/insights" in detail
