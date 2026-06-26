"""DB-gated unit tests for derived requirement-completeness queries.

Exercises the completeness methods added to ``PostgresVectorGraph`` directly
against Postgres (SQL-only, so they cannot live in the offline write_policy
tests). Mirrors ``test_surface_bindings.py``: same ``skipif`` on a resolvable
DSN, a fresh per-test tenant (``unique_org`` + a deterministic user), and a
graph built with ``FakeEmbedder`` and a ``[Redactor, Deduper]`` policy (no
overwriter, so the distinct requirement texts coexist instead of folding into
one another).

Completeness is DERIVED — never a self-set flag. An active
``category="requirement"`` fact scoped to ``source="prd-<project>"`` is
INCOMPLETE when it has never had a successful outcome (never-built), most
recently failed after a prior success (regressed — the bug/ticket path), or
carries a ``STALE_DERIVED_EDGE`` because a fact it derives from was invalidated
(stale). Otherwise complete. Rejected/superseded requirements are excluded
everywhere (active-only). Primary reason precedence partitions the set:
never-built > regressed > stale.
"""

from __future__ import annotations

import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    DERIVED_FROM_EDGE,
    PostgresVectorGraph,
)
from knowledge.knowledge_graph.write_policy.write_step_variants import Deduper, Redactor
from knowledge.llm.embedder_variants.fake_embedder import FakeEmbedder
from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "u1"
PROJECT = "team-app"


def _graph(conn, org, user):
    """Fresh tenant + a coexist-friendly graph (distinct texts never merge)."""
    # Edges first (FK), then facts — a clean slate every run.
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s AND user_id = %s", (org, user))
    conn.execute("DELETE FROM facts WHERE org_id = %s AND user_id = %s", (org, user))
    return PostgresVectorGraph(
        conn,
        org,
        user,
        embedder=FakeEmbedder(),
        recall_floor=-1.0,
        policy=[Redactor(), Deduper()],
    )


def _requirement(graph, text, project=PROJECT, **kw):
    """Seed an active requirement scoped to ``prd-<project>`` (the factory's source)."""
    return graph.write(
        text,
        state="active",
        category="requirement",
        source=f"prd-{project}",
        **kw,
    )


def _incomplete_ids(graph, project=PROJECT) -> list[str]:
    return [item["fact"].id for item in graph.incomplete_requirements(project)]


def _reason_for(graph, fact_id, project=PROJECT) -> str | None:
    for item in graph.incomplete_requirements(project):
        if item["fact"].id == fact_id:
            return item["reason"]
    return None


def test_never_built_requirement_is_incomplete(unique_org):
    """A fresh active requirement with no recorded outcome is never-built."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = _requirement(graph, "The home screen lists today's tasks.")

    items = graph.incomplete_requirements(PROJECT)
    assert [i["fact"].id for i in items] == [req]
    entry = items[0]
    assert entry["reason"] == "never-built"
    assert entry["reasons"] == ["never-built"]
    assert entry["success_count"] == 0
    assert entry["last_outcome"] is None


def test_success_only_requirement_is_complete(unique_org):
    """A requirement whose latest (and only) outcome succeeded is complete and
    drops out of ``incomplete_requirements``."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = _requirement(graph, "Settings let the user change the theme.")
    graph.record_outcome(req, success=True)

    assert _incomplete_ids(graph) == []


def test_regressed_requirement_reappears_after_failure(unique_org):
    """The ticket path: a previously-complete requirement (single success) is NOT
    incomplete, then reappears as ``regressed`` once a later outcome fails."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = _requirement(graph, "Login rejects an expired token.")

    graph.record_outcome(req, success=True)
    assert _incomplete_ids(graph) == []  # complete after the first success

    graph.record_outcome(req, success=False)  # a ticket records a failing outcome
    items = graph.incomplete_requirements(PROJECT)
    assert [i["fact"].id for i in items] == [req]
    entry = items[0]
    assert entry["reason"] == "regressed"
    assert entry["last_outcome"] == "failed"  # latest-outcome stamp drives regression
    assert entry["success_count"] == 1
    assert entry["failure_count"] == 1  # the recorded failure is surfaced, not 0


def test_stale_requirement_when_dependency_rejected(unique_org):
    """The change path: a complete requirement derives_from a dep; rejecting the
    dep flags the dependent stale, so it reappears with ``reason=="stale"``."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    dep = graph.write("Tasks are stored in DynamoDB.", state="active", category="decision")
    req = _requirement(graph, "The task list reads from the store.")
    graph.add_edge(req, dep, DERIVED_FROM_EDGE)
    graph.record_outcome(req, success=True)
    assert _incomplete_ids(graph) == []  # complete before the dependency changes

    graph.set_state(dep, "rejected")  # invalidates the basis -> flags dependent stale

    assert _reason_for(graph, req) == "stale"


def test_compound_reasons_regressed_and_stale(unique_org):
    """A requirement can be incomplete for more than one reason at once: ``reasons``
    lists every cause (primary first), while ``reason`` stays the single primary.
    Here a regressed requirement also has a rejected dependency -> stale."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    dep = graph.write("Streaks are computed nightly.", state="active", category="decision")
    req = _requirement(graph, "The roster shows each member's streak.")
    graph.add_edge(req, dep, DERIVED_FROM_EDGE)
    graph.record_outcome(req, success=True)
    graph.record_outcome(req, success=False)  # regressed (latest outcome failed)
    graph.set_state(dep, "rejected")  # also flags the dependent stale

    entry = next(
        i for i in graph.incomplete_requirements(PROJECT) if i["fact"].id == req
    )
    assert entry["reason"] == "regressed"  # primary: regressed > stale
    assert entry["reasons"] == ["regressed", "stale"]  # both surfaced, in precedence


def test_rejecting_requirement_excludes_it_everywhere(unique_org):
    """A rejected requirement is active-only-excluded: it appears in neither
    ``incomplete_requirements`` nor the ``completeness_summary`` totals."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    req = _requirement(graph, "A never-built requirement to retire.")
    assert _incomplete_ids(graph) == [req]

    graph.set_state(req, "rejected")

    assert _incomplete_ids(graph) == []
    summary = graph.completeness_summary(PROJECT)
    assert summary["total_active_requirements"] == 0
    assert summary["incomplete"] == 0


def test_completeness_summary_shape_and_breakdown(unique_org):
    """The summary reports totals and a by-reason breakdown that sums to
    ``incomplete``; rejected requirements are not counted."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)

    # never-built: no outcome.
    _requirement(graph, "Unbuilt: export tasks to CSV.")
    # complete: single success.
    done = _requirement(graph, "Complete: greet the user by name.")
    graph.record_outcome(done, success=True)
    # regressed: success then failure.
    regressed = _requirement(graph, "Regressed: search filters by tag.")
    graph.record_outcome(regressed, success=True)
    graph.record_outcome(regressed, success=False)
    # rejected: excluded from totals entirely.
    rejected = _requirement(graph, "Rejected: legacy import flow.")
    graph.set_state(rejected, "rejected")

    summary = graph.completeness_summary(PROJECT)
    assert set(summary) == {
        "total_active_requirements",
        "complete",
        "incomplete",
        "breakdown",
    }
    assert summary["total_active_requirements"] == 3  # rejected not counted
    assert summary["complete"] == 1
    assert summary["incomplete"] == 2
    assert set(summary["breakdown"]) == {"never_built", "stale", "regressed"}
    assert summary["breakdown"] == {"never_built": 1, "stale": 0, "regressed": 1}
    assert (
        summary["breakdown"]["never_built"]
        + summary["breakdown"]["stale"]
        + summary["breakdown"]["regressed"]
        == summary["incomplete"]
    )


def test_project_scoping_isolates_requirements(unique_org):
    """A requirement in ``prd-other`` never leaks into ``team-app``'s queries."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)
    mine = _requirement(graph, "Team-app: pin a task to the top.")
    _requirement(graph, "Other-app: archive a project.", project="other")

    assert _incomplete_ids(graph, PROJECT) == [mine]
    assert graph.completeness_summary(PROJECT)["total_active_requirements"] == 1
    # The other project sees only its own requirement.
    assert graph.completeness_summary("other")["total_active_requirements"] == 1


def test_record_outcome_sets_last_outcome(unique_org):
    """``record_outcome`` stamps ``last_outcome`` so latest-result wins: a
    succeeded-then-failed requirement is regressed, a fail-then-succeed one is
    complete (the order of outcomes, not just the counts, decides)."""
    conn = db.connect()
    graph = _graph(conn, unique_org, USER)

    # last_outcome is surfaced via the completeness read path (_active_project_
    # requirements), which is where the derived queries consume it.
    regressed = _requirement(graph, "Succeeded then failed.")
    graph.record_outcome(regressed, success=True)
    graph.record_outcome(regressed, success=False)
    entry = next(
        i for i in graph.incomplete_requirements(PROJECT) if i["fact"].id == regressed
    )
    assert entry["last_outcome"] == "failed"  # latest result, not the prior success
    assert _reason_for(graph, regressed) == "regressed"

    recovered = _requirement(graph, "Failed then succeeded.")
    graph.record_outcome(recovered, success=False)
    graph.record_outcome(recovered, success=True)
    # latest success wins over the earlier failure -> complete, drops out.
    assert recovered not in _incomplete_ids(graph)


@pytest.fixture
def unique_org(request):
    # Unique per test node so reruns and parallel tenants never collide.
    return "test_" + request.node.name
