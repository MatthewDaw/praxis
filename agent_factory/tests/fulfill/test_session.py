"""U6 — session lifecycle + seeding tests (against the in-memory FakeBackend).

Both gates green for a fresh tax session (completeness 0/6, surface_coverage(mvp) 0 uncovered);
the dependents requirement (renders []) is seeded but not bound; isolation holds across two
sessions; a Praxis error during seeding propagates fail-closed.
"""

from __future__ import annotations

import pytest

from agent_factory.fulfill.session import space_id_for, start_session
from tests.fulfill.conftest import FakeFulfillPraxis


def test_seeds_six_requirements_binds_five(domain, backend):
    client = FakeFulfillPraxis(backend, space=space_id_for("alice"))
    sess = start_session(domain, "alice", client=client)
    assert len(sess.requirement_fact_ids) == 6
    binds = [c for c in client.calls if c[0] == "bind"]
    assert len(binds) == 5  # T1-T5 render; T6 (dependents) renders [] -> not bound


def test_both_gates_green_for_fresh_session(domain, backend):
    client = FakeFulfillPraxis(backend, space=space_id_for("bob"))
    sess = start_session(domain, "bob", client=client)
    summary = client.completeness_summary(domain.project)
    assert summary["total"] == 6 and summary["complete"] == 0
    cov = client.surface_coverage(domain.project, scope="mvp")
    assert cov["uncoveredSurfaces"] == []
    assert cov["uncoveredRequirements"] == []  # T6 is post-mvp, filtered out


def test_dependents_seeded_but_not_bound(domain, backend):
    client = FakeFulfillPraxis(backend, space=space_id_for("carol"))
    sess = start_session(domain, "carol", client=client)
    t6_fact = sess.requirement_fact_ids["T6"]
    sp = backend.space(sess.space_id)
    bound_src = {src for src, _dst in sp["binds"]}
    assert t6_fact not in bound_src
    assert t6_fact in sp["reqs"]  # but it IS seeded


def test_two_sessions_are_isolated(domain, backend):
    c1 = FakeFulfillPraxis(backend, space=space_id_for("s1"))
    c2 = FakeFulfillPraxis(backend, space=space_id_for("s2"))
    s1 = start_session(domain, "s1", client=c1)
    start_session(domain, "s2", client=c2)
    # cover one requirement in session 1.
    c1.record_outcome(s1.requirement_fact_ids["T1"], True)
    assert c1.completeness_summary(domain.project)["complete"] == 1
    # session 2 is unaffected.
    assert c2.completeness_summary(domain.project)["complete"] == 0


def test_fail_closed_during_seeding(domain, backend):
    class Boom(FakeFulfillPraxis):
        def ingest_requirement(self, **kw):
            raise RuntimeError("praxis down")

    client = Boom(backend, space=space_id_for("dan"))
    with pytest.raises(RuntimeError):
        start_session(domain, "dan", client=client)


def test_close_deletes_space(domain, backend):
    client = FakeFulfillPraxis(backend, space=space_id_for("eve"))
    sess = start_session(domain, "eve", client=client)
    assert sess.space_id in backend.spaces
    sess.close()
    assert sess.space_id not in backend.spaces


def test_space_id_slugifies():
    assert space_id_for("Alice 01") == "sess-alice-01"
