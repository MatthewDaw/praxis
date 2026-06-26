"""Serve-level specs for the derived requirement-completeness READ surface.

Covers the HTTP routes the MCP completeness tools forward to:
  * ``GET /requirements/incomplete?project=<p>`` — the active requirements in
    ``prd-<p>`` that are NOT verified-complete, and
  * ``GET /requirements/completeness?project=<p>`` — the done-of-definition counts.

Completeness is DERIVED from verification + staleness signals, never a self-set
flag (see ``PostgresVectorGraph._completeness_reasons``): a requirement is
incomplete when it has never had a successful outcome (never-built), its latest
outcome failed after a prior success (regressed — the bug/ticket path), or a fact
it derives from was invalidated (stale — needs rework).

Like the sibling read-surface tests these exercise behavior ABOVE the component
layer and need a Postgres DSN. Unlike them we seed via the graph object directly
(``write``/``record_outcome``/``add_edge``/``set_state``) rather than the HTTP add
path, so no embedder key is required — the read endpoints never embed.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (  # noqa: E402
    DERIVED_FROM_EDGE,
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import (  # noqa: E402
    Deduper,
    Redactor,
)
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder  # noqa: E402
from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"
PROJECT = "team-app"
SOURCE = f"prd-{PROJECT}"


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
    # Seed via the graph object directly (no embedder key needed). A coexist-friendly
    # policy (no overwriter) keeps the distinct requirement texts as separate facts.
    graph = PostgresVectorGraph(
        conn,
        org,
        USER,
        embedder=FakeEmbedder(),
        recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )
    yield client, graph
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


def _req(graph, text, **extra):
    """Seed one active requirement scoped to ``prd-team-app``."""
    return graph.write(
        text, state="active", category="requirement", source=SOURCE, **extra
    )


def _incomplete(client):
    res = client.get("/requirements/incomplete", params={"project": PROJECT})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["project"] == PROJECT
    return body["incomplete"]


def _by_id(items):
    return {i["id"]: i for i in items}


def _summary(client):
    res = client.get("/requirements/completeness", params={"project": PROJECT})
    assert res.status_code == 200, res.text
    return res.json()


# --- scenario 1: never-built ----------------------------------------------
def test_fresh_requirement_is_never_built(env):
    """A fresh active requirement with no recorded outcome surfaces as never-built."""
    client, graph = env
    rid = _req(graph, "The home screen lists today's tasks.")

    item = _by_id(_incomplete(client))[rid]
    assert item["reason"] == "never-built"
    assert item["reasons"] == ["never-built"]
    assert item["successCount"] == 0
    assert item["lastOutcome"] is None


# --- scenario 2: regressed (the ticket path) -------------------------------
def test_requirement_regresses_after_failed_outcome(env):
    """A once-passing requirement reappears as regressed after a later failed
    outcome — the bug/ticket path. A success-only sibling stays complete."""
    client, graph = env
    bug = _req(graph, "Login rejects an expired token.")
    stable = _req(graph, "The settings page saves the chosen theme.")

    # Both pass once → neither is incomplete (complete: latest outcome succeeded).
    graph.record_outcome(bug, success=True)
    graph.record_outcome(stable, success=True)
    assert bug not in _by_id(_incomplete(client))
    assert stable not in _by_id(_incomplete(client))

    # A ticket records a failing outcome on the previously-complete requirement.
    graph.record_outcome(bug, success=False)

    items = _by_id(_incomplete(client))
    assert bug in items
    assert stable not in items
    regressed = items[bug]
    assert regressed["reason"] == "regressed"
    assert regressed["reasons"] == ["regressed"]
    assert regressed["successCount"] == 1
    assert regressed["failureCount"] == 1  # the recorded failure is surfaced, not 0
    assert regressed["lastOutcome"] == "failed"  # latest-outcome stamp drives regression


# --- scenario 3: stale (the change path) -----------------------------------
def test_requirement_goes_stale_when_dependency_rejected(env):
    """A verified-complete requirement that derives from a dependency goes stale
    (needs rework) when that dependency is rejected; rejecting the requirement
    itself then drops it from the active-only incomplete surface."""
    client, graph = env
    dep = _req(graph, "Auth tokens are signed with RS256.")
    req = _req(graph, "The gateway verifies JWTs with the RS256 public key.")
    graph.add_edge(req, dep, DERIVED_FROM_EDGE)

    # Make the requirement verified-complete so STALE is the only live reason.
    graph.record_outcome(req, success=True)
    assert req not in _by_id(_incomplete(client))

    # Rejecting the dependency flags the dependent for review (H5 stale hook).
    graph.set_state(dep, "rejected")

    item = _by_id(_incomplete(client))[req]
    assert item["reason"] == "stale"
    assert item["reasons"] == ["stale"]
    assert item["successCount"] == 1
    assert item["lastOutcome"] == "succeeded"

    # Rejecting the requirement removes it from the incomplete surface (active-only).
    graph.set_state(req, "rejected")
    assert req not in _by_id(_incomplete(client))


# --- scenario 4: complete + rejected excluded; summary shape ----------------
def test_completeness_summary_counts_and_excludes_rejected(env):
    """A success-only requirement is complete (not incomplete); a rejected one
    appears in NEITHER endpoint; the summary counts/breakdown are correct."""
    client, graph = env
    complete = _req(graph, "The dashboard shows the active org name.")
    never = _req(graph, "Users can export their data as CSV.")
    regressed = _req(graph, "Password reset emails send within a minute.")
    rejected = _req(graph, "A deprecated requirement that was cut from scope.")

    graph.record_outcome(complete, success=True)
    graph.record_outcome(regressed, success=True)
    graph.record_outcome(regressed, success=False)
    graph.set_state(rejected, "rejected")

    items = _by_id(_incomplete(client))
    # Complete and rejected are absent; never-built and regressed are present.
    assert complete not in items
    assert rejected not in items
    assert items[never]["reason"] == "never-built"
    assert items[regressed]["reason"] == "regressed"

    summary = _summary(client)
    assert summary["project"] == PROJECT
    # Rejected is excluded from the active-requirement total entirely.
    assert summary["total_active_requirements"] == 3
    assert summary["complete"] == 1
    assert summary["incomplete"] == 2
    assert summary["breakdown"] == {"never_built": 1, "stale": 0, "regressed": 1}
