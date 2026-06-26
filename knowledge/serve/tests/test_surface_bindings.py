"""Serve-level specs for the requirement<->surface RENDERS routes.

Drives the HTTP routes the surface MCP tools forward to:
  * ``POST /surfaces/bind`` — bind a requirement fact to a wireframe screen,
  * ``GET /surfaces/{screen_id}/requirements`` — which requirements govern a screen,
  * ``GET /facts/{id}/surfaces`` — the inverse (which screens a requirement renders),
  * ``GET /surfaces/coverage`` — the bidirectional completeness gate,
  * the active-only drop after ``POST /candidates/{id}/reject``.

Like ``test_compounding_read_surface.py`` these exercise behavior ABOVE the
component layer and need a Postgres DSN AND an OPENROUTER_API_KEY (the HTTP write
path embeds for real).
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    PostgresVectorGraph,
)
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY",
)

USER = "dev-user"
PROJECT = "demo"


@pytest.fixture
def env(unique_org):
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    tables = ("fact_edges", "facts", "cached_facts", "org_members", "orgs")
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    graph = PostgresVectorGraph(conn, org, USER)
    yield client, graph
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _add(client, insight, **extra):
    res = client.post("/insights", json={"insight": insight, **extra})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_bind_read_and_reject_requirement_for_surface(env):
    """Bind a requirement to a screen, read it both ways, then reject it and watch
    the active-only requirement query drop it."""
    client, _ = env
    req_id = _add(
        client,
        "The home screen must list today's tasks.",
        category="requirement",
        scope="mvp",
    )

    bind = client.post(
        "/surfaces/bind",
        json={
            "requirementFactId": req_id,
            "screenId": "s-today",
            "project": PROJECT,
            "title": "Today",
        },
    )
    assert bind.status_code == 200, bind.text
    body = bind.json()
    assert body["requirementFactId"] == req_id
    assert body["screenId"] == "s-today"
    surface_id = body["surfaceId"]

    # Which requirements govern screen s-today?
    reqs = client.get("/surfaces/s-today/requirements", params={"project": PROJECT})
    assert reqs.status_code == 200, reqs.text
    assert req_id in [v["id"] for v in reqs.json()["requirements"]]

    # The inverse: which screens does the requirement render?
    surfaces = client.get(f"/facts/{req_id}/surfaces")
    assert surfaces.status_code == 200, surfaces.text
    screen_ids = [v["meta"].get("screen_id") for v in surfaces.json()["surfaces"]]
    assert "s-today" in screen_ids
    assert surface_id in [v["id"] for v in surfaces.json()["surfaces"]]

    # Reject the requirement — active-only query must drop it.
    assert client.post(f"/candidates/{req_id}/reject").status_code == 200
    after = client.get("/surfaces/s-today/requirements", params={"project": PROJECT})
    assert req_id not in [v["id"] for v in after.json()["requirements"]]



def test_coverage_flags_uncovered_surface_and_requirement(env):
    """``GET /surfaces/coverage`` flags both an unbound surface and an unbound
    requirement — the bidirectional completeness gate."""
    client, _ = env
    # An orphan surface no requirement renders.
    orphan = client.post(
        "/surfaces",
        json={"project": PROJECT, "screenId": "s-orphan", "title": "Orphan"},
    )
    assert orphan.status_code == 200, orphan.text

    # A requirement that renders no surface.
    req_id = _add(
        client,
        "The settings screen must allow changing the theme.",
        category="requirement",
        scope="mvp",
    )

    cov = client.get("/surfaces/coverage", params={"project": PROJECT})
    assert cov.status_code == 200, cov.text
    data = cov.json()
    uncovered_screen_ids = [
        v["meta"].get("screen_id") for v in data["uncoveredSurfaces"]
    ]
    uncovered_req_ids = [v["id"] for v in data["uncoveredRequirements"]]
    assert "s-orphan" in uncovered_screen_ids
    assert req_id in uncovered_req_ids


def test_surfaces_requires_project_query(env):
    """The read routes 400 when the required ``project`` query param is missing."""
    client, _ = env
    assert client.get("/surfaces/s-today/requirements").status_code == 400
    assert client.get("/surfaces/coverage").status_code == 400
