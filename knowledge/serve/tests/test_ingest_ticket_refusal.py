"""``POST /ingest`` refuses a requirement TICKET instead of distilling it (corruption door).

``/insights`` and ``/insights/batch`` already route a ticket-shaped write to the identity-keyed
``_requirement_upsert``. ``/ingest`` had no such gate at all: it accepts per-document ``category``
and ``meta``, so a "document" carrying ``meta.build_state`` + ``meta.requirement_id`` went straight
into ``ingest_dump`` under ``[Redactor(), Deduper()]`` — the reconciled lane. There, step 2a merges
a recalled ESTABLISHED fact with the newcomer and **overwrites the incumbent's text** with the
longer of the two (``dump_ingest._merge``), so one ingested document silently destroys an existing
ticket's content and the new ticket is never created. Identical failure, different front door.

The fix REFUSES the write (400) rather than rerouting it: a document is not a fact — one text
distills into MANY facts, all stamped with the document's ``meta`` — so honoring
``meta.requirement_id`` here would mint N active facts sharing one identity, which is the very
duplicate-identity damage ``_requirement_upsert`` exists to prevent.

WHY THIS HARNESS IS NOT VACUOUS
-------------------------------
The default ``FakeEmbedder`` hashes whole texts, so nothing is ever recalled and no merge step can
fire — a naive test would pass against a completely unfixed server. So, at the exact seams the
route uses, this module injects:

* ``TopicEmbedder`` — the deterministic bag-of-words embedder from
  ``test_ticket_write_no_merge_corruption`` (shared words => real recall, no network);
* ``StubDistillerLlm`` for ``OpenRouterLlm`` — answers ``ingest_dump``'s distillation call with a
  fixed fact+claim, and its batched "same fact?" judge with "yes" (the merge lane).

``test_control_plain_document_really_destroys_an_incumbent_ticket`` fires the SAME document, minus
only the ticket identity keys, and shows the incumbent's text destroyed. Every refusal assertion
below is therefore a real door being shut, not a no-op.

Postgres-gated (skips without a DSN), like the rest of the serve suite.
"""

from __future__ import annotations

import json

import pytest

from knowledge.serve import db
from knowledge.serve.tests.test_ticket_write_no_merge_corruption import TopicEmbedder

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub
PROJECT = "ingestproj"
SNAPSHOT = f"prd-{PROJECT}"
HEADERS = {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": SNAPSHOT}

# The incumbent ticket already in the plan, and the document that (unguarded) eats it. They share
# most of their words so the TopicEmbedder genuinely recalls one from the other; the document's
# distilled text is LONGER, which is what makes ``_merge`` overwrite the incumbent rather than the
# other way round.
INCUMBENT_TEXT = "R7 the exporter writes a csv manifest for each nightly batch run"
DOC_TEXT = (
    "R11 the exporter writes a parquet manifest for each nightly batch run "
    "and validates the checksum before upload"
)
# Deliberately a DIFFERENT attribute from the incumbent's claim, so the write policy's slot guard
# does not settle this — the merge under test is step 2a's "same fact, different phrasing" one.
DOC_CLAIM = {"subject": "nightly export manifest", "attribute": "encoding", "value": "parquet"}
INCUMBENT_CLAIM = {"subject": "exporter manifest", "attribute": "format", "value": "csv"}


class StubDistillerLlm:
    """``OpenRouterLlm`` stand-in driving ``ingest_dump`` deterministically, no network.

    Dispatches on the JSON schema name the caller asks for: one distilled fact for the
    distillation call, "these two are the same fact" for the batched dedup judge (the merge that
    overwrites an incumbent), and "no shared slot" for the conflict judge.
    """

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, response_format=None) -> str:
        name = ((response_format or {}).get("json_schema") or {}).get("name")
        if name == "distillation":
            return json.dumps({"facts": [{"text": DOC_TEXT, **DOC_CLAIM}]})
        if name == "same_fact":
            return json.dumps({"same": [0]})
        return json.dumps({"same_slot": []})


@pytest.fixture
def env(unique_org, monkeypatch):
    """TestClient over the real app, with the recall + distillation seams made deterministic."""
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.knowledge_graph.write_policy.write_step_variants import Redactor
    from knowledge.serve import app as app_module
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    # The server builds its own graph + LLM, so both seams are swapped at module level.
    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", TopicEmbedder)
    monkeypatch.setattr(app_module, "OpenRouterLlm", StubDistillerLlm)

    org = unique_org
    tables = ("fact_edges", "facts", "snapshot_edges", "snapshots",
              "org_members", "orgs", "spaces")

    db.bootstrap()
    conn = db.connect()

    def _clean() -> None:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE org_id = %s", (org,))

    _clean()
    OrgsStore(conn).create_org(org, org, "pw", USER)
    SpacesStore(conn).create_space(org, PROJECT, PROJECT)

    # Redact-only seeding so the incumbent lands exactly as written (the default policy would
    # reconcile the seed itself and any damage would be a fixture artifact).
    seed_graph = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=PROJECT, snapshot=SNAPSHOT,
        embedder=TopicEmbedder(), recall_floor=-1.0, policy=[Redactor()],
    )
    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, seed_graph
    _clean()
    conn.close()


@pytest.fixture
def incumbent(env):
    """A finished ticket already living in the plan snapshot — the thing that gets destroyed."""
    _client, seed_graph = env
    return seed_graph.write(
        INCUMBENT_TEXT,
        state="active",
        source=SNAPSHOT,
        category="requirement",
        meta={
            "requirement_id": "R7",
            "build_state": "finished",
            # ingest_dump only resolves a newcomer against an established fact that carries a
            # stored claim, so the incumbent must look like a normally-ingested fact.
            "claim": INCUMBENT_CLAIM,
        },
    )


def _ingest(client, document):
    return client.post("/ingest", json={"documents": [document]}, headers=HEADERS)


def _texts(client):
    body = client.get(f"/spaces/{PROJECT}/snapshots/{SNAPSHOT}/facts").json()
    return [f["text"] for g in body["groups"] for f in g["facts"]]


# --------------------------------------------------------------------------- the control

def test_control_plain_document_really_destroys_an_incumbent_ticket(env, incumbent):
    """The rig is LIVE: the same document without the identity keys eats the incumbent ticket.

    Nothing about this document is malformed — it is exactly what /ingest is for. That is the
    point: the distillation lane's merge step rewrites an established fact's text in place, so a
    ticket that reaches this lane is not "maybe" corrupted, it is corrupted.
    """
    client, seed_graph = env
    resp = _ingest(client, {"text": DOC_TEXT, "source": SNAPSHOT})
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["merged"] == 1

    survivor = seed_graph.get_fact(incumbent)
    assert survivor.text == DOC_TEXT, "expected the incumbent's content to be overwritten"
    assert INCUMBENT_TEXT not in _texts(client), "the R7 ticket's text is gone from the snapshot"


# --------------------------------------------------------------------------- the fix

def test_ticket_shaped_document_is_refused_and_nothing_is_written(env, incumbent):
    """The identical document, now carrying the ticket identity, is refused with the same effect
    the /insights fix has: the incumbent is untouched and no ticket is silently absorbed."""
    client, seed_graph = env
    resp = _ingest(client, {
        "text": DOC_TEXT,
        "source": SNAPSHOT,
        "category": "requirement",
        "meta": {"requirement_id": "R11", "build_state": "incomplete"},
    })
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "POST /insights" in detail, detail
    assert "requirement_id" in detail, detail

    assert seed_graph.get_fact(incumbent).text == INCUMBENT_TEXT
    assert _texts(client) == [INCUMBENT_TEXT]


def test_missing_category_does_not_open_the_door(env, incumbent):
    """``category`` is not trusted here either — the identity keys alone are the evidence.

    The prd-sotos corruption came from a caller who shipped ``meta.requirement_id`` +
    ``meta.build_state`` but forgot ``category="requirement"``; that omission must not turn the
    refusal off, or the exact observed failure walks straight back in through /ingest.
    """
    client, seed_graph = env
    resp = _ingest(client, {
        "text": DOC_TEXT,
        "meta": {"requirement_id": "R11", "build_state": "incomplete"},
    })
    assert resp.status_code == 400, resp.text
    assert seed_graph.get_fact(incumbent).text == INCUMBENT_TEXT


def test_refusal_precedes_ingestion_of_the_whole_batch(env, incumbent):
    """One ticket anywhere in ``documents`` refuses the CALL, before any document is written.

    A per-document skip would leave the caller with a partially-applied ingest and a ticket that
    silently vanished — the refusal has to be loud enough that they re-author it correctly.
    """
    client, seed_graph = env
    resp = client.post(
        "/ingest",
        json={"documents": [
            {"text": "an unrelated note about deployment windows"},
            {"text": DOC_TEXT, "category": "requirement",
             "meta": {"requirement_id": "R11", "build_state": "incomplete"}},
        ]},
        headers=HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert "documents[1]" in resp.json()["detail"]
    assert _texts(client) == [INCUMBENT_TEXT], "no document from a refused call may land"


def test_plain_requirement_assertion_is_still_ingestible(env):
    """Only the ticket SHAPE is refused. A requirement assertion with no ``build_state`` is
    ordinary distillable knowledge and must keep working, or the guard has quietly closed the
    endpoint to a legitimate caller."""
    client, _seed_graph = env
    resp = _ingest(client, {
        "text": DOC_TEXT,
        "category": "requirement",
        "meta": {"topic": "exports"},
    })
    assert resp.status_code == 200, resp.text
