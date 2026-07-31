"""A requirement-TICKET write can never be additively merged into another ticket (prod corruption).

The live failure this pins (observed against the prod backend on the ``prd-sotos`` plan):
``praxis_add_insight`` shipped a brand-new ticket with ``on_conflict="surface"`` but WITHOUT
``category="requirement"``. ``_is_requirement_ticket`` looked only at ``category``, said "not a
ticket", and the write fell through to the RECONCILED path. There ``on_conflict="surface"`` selects
``default_write_policy()`` — the only policy that contains an ``Augmenter`` (the Mem0-style additive
merge). The Augmenter folded the new ticket into a topically-similar EXISTING ticket. The response
was::

    {"summary": "merged insight", "action": "merged",
     "id": "<an id the caller never wrote>", "contradictionsSurfaced": 0}

and the effect was: the new ticket never created, the other ticket's content destroyed, unrelated
facts flipped to ``state="rejected"``, and the counter reporting ``0``.

The fix routes ANY write carrying the ticket IDENTITY (``meta.build_state`` + ``meta.requirement_id``)
to ``_requirement_upsert`` — identity-keyed, on a redact-only graph — even when ``category`` is
missing or wrong, and stamps ``category="requirement"`` on the way in.

WHY THIS HARNESS IS NOT VACUOUS
-------------------------------
``Augmenter``/``ConflictOverwriter`` are LLM-backed, and the default ``FakeEmbedder`` hashes text so
nothing is ever semantically recalled — a naive offline test would prove nothing because no merge
step could ever fire. So this module injects, at the exact seams ``POST /insights`` uses:

* ``TopicEmbedder`` — a deterministic BAG-OF-WORDS embedder, so texts that SHARE WORDS genuinely
  recall each other above the graph's 0.45 ``recall_floor`` (``FakeEmbedder`` cannot do this);
* ``default_write_policy`` -> a policy whose ``Augmenter`` ALWAYS merges (the ``surface`` lane);
* ``OpenRouterLlm`` -> a ``FakeLlm`` that always answers "yes" (the ``auto_resolve`` lane's
  ``ConflictOverwriter``, which rejects every recalled fact).

``test_control_non_ticket_surface_write_really_does_merge`` proves the rig is live: the SAME text and
the SAME ``category="requirement"``, differing ONLY by the absence of the ticket identity keys, IS
merged and IS corrupted. Every ticket assertion below is therefore a real bypass, not a no-op.

Postgres-gated (skips without a DSN), like the rest of the serve suite.
"""

from __future__ import annotations

import hashlib
import math

import pytest

from knowledge.serve import db

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub
PROJECT = "ticketproj"
SNAPSHOT = f"prd-{PROJECT}"
HEADERS = {"X-Praxis-Space": PROJECT, "X-Praxis-Snapshot": SNAPSHOT}

# Deliberately overlapping prose: the incoming ticket and the incumbent share most of their
# words, so the TopicEmbedder recalls one from the other and the merge steps get a real target.
INCUMBENT_TEXT = "the source discriminator dedups scraped rows by canonical url on ingest"
NEWCOMER_TEXT = "the source discriminator dedups scraped rows by product id on ingest"


# --------------------------------------------------------------------------- deterministic seams

class TopicEmbedder:
    """Deterministic bag-of-words embedder: shared words => high cosine, no network.

    ``FakeEmbedder`` hashes the WHOLE text, so two topically-similar sentences land at cosine ~0
    and never enter ``decision.candidates`` — which would make every merge step a silent no-op and
    every assertion here vacuous. This hashes each WORD into a bucket instead, so similarity is a
    real, reproducible function of shared vocabulary.
    """

    _DIM = 32

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vec(text)

    @classmethod
    def _vec(cls, text: str) -> list[float]:
        vals = [0.0] * cls._DIM
        for word in text.lower().split():
            bucket = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % cls._DIM
            vals[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]


class AlwaysMergeJudge:
    """An ``AugmentJudge`` stand-in that always approves an additive merge, deterministically."""

    def merged_text(self, incoming: str, existing: str) -> str:
        return f"{existing} AND {incoming}"


def merging_write_policy(llm=None):  # noqa: ANN001 - matches default_write_policy's signature
    """Stand-in for ``default_write_policy()``: the ``surface`` lane WITH a live Augmenter.

    Same shape as production's surface policy (redact, dedup, additively merge) minus the LLM
    round-trips. If a ticket write ever reaches this policy, it gets merged — which is precisely
    the corruption these tests forbid.
    """
    from knowledge.knowledge_graph.write_policy.write_step_variants import (
        Augmenter,
        Deduper,
        Redactor,
    )

    return [Redactor(), Deduper(), Augmenter(judge=AlwaysMergeJudge())]


def always_yes_llm():
    """``OpenRouterLlm`` stand-in: answers "yes" to ConflictOverwriter's contradiction prompt."""
    from knowledge.llm.llm_variants.fake_llm import FakeLlm

    return FakeLlm(default="yes")


# --------------------------------------------------------------------------- fixture

@pytest.fixture
def env(unique_org, monkeypatch):
    """TestClient over the real app, with the merge/conflict seams made deterministic."""
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")
    from fastapi.testclient import TestClient

    from knowledge.knowledge_graph.knowledge_graph_variants import postgres_vector_graph
    from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
        PostgresVectorGraph,
    )
    from knowledge.serve import app as app_module
    from knowledge.serve.app import create_app
    from knowledge.serve.orgs_store import OrgsStore
    from knowledge.serve.spaces_store import SpacesStore

    # The server builds its own graphs, so the seams are swapped at module level.
    monkeypatch.setattr(postgres_vector_graph, "OpenRouterEmbedder", TopicEmbedder)
    monkeypatch.setattr(app_module, "default_write_policy", merging_write_policy)
    monkeypatch.setattr(app_module, "OpenRouterLlm", always_yes_llm)

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

    # Seeding is REDACT-ONLY on purpose: the incumbents must land exactly as written, at exactly
    # the state asked for. With the graph's default policy the seed writes run the real pipeline
    # and FR-005 demotes a later incumbent to ``proposed`` — a fixture artifact that would be
    # mistaken for damage done by the write under test.
    from knowledge.knowledge_graph.write_policy.write_step_variants import Redactor

    seed_graph = PostgresVectorGraph(
        conn, org, facts_table="snapshots", space=PROJECT, snapshot=SNAPSHOT,
        embedder=TopicEmbedder(), recall_floor=-1.0, policy=[Redactor()],
    )
    client = TestClient(create_app(conn), headers={"X-Praxis-Org": org})
    yield client, conn, org, seed_graph
    _clean()
    conn.close()


# --------------------------------------------------------------------------- helpers

def _ticket(insight: str, rid: str, **meta):
    """The add-a-ticket body: identity keys in meta, ``surface`` — the production call shape."""
    return {
        "insight": insight,
        "source": SNAPSHOT,
        "category": "requirement",
        "onConflict": "surface",
        "meta": {"requirement_id": rid, "build_state": "incomplete", **meta},
    }


def _post(client, body):
    return client.post("/insights", json=body, headers=HEADERS)


def _batch(client, items, **body):
    """``POST /insights/batch`` at the same snapshot target the singular route uses."""
    return client.post(
        "/insights/batch", json={"insights": items, **body}, headers=HEADERS
    )


def _rows(conn, org):
    """Every fact in the targeted snapshot as {id: (text, state, category, meta)}."""
    rows = conn.execute(
        "SELECT id, text, state, category, meta FROM snapshots "
        "WHERE org_id = %s AND space = %s AND snapshot = %s",
        (org, PROJECT, SNAPSHOT),
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4] or {}) for r in rows}


def _seed(graph, text, rid=None, category="requirement", state="active"):
    meta = {"requirement_id": rid} if rid else None
    return graph.write(text, state=state, source=SNAPSHOT, category=category, meta=meta)


# --------------------------------------------------------------------------- the rig is live

def test_control_non_ticket_surface_write_really_does_merge(env):
    """CONTROL — not a fix assertion. The SAME text and category, differing ONLY by the missing
    ticket identity keys, IS additively merged into the incumbent and DOES corrupt it.

    This is what makes every other test in this module meaningful: it proves the Augmenter really
    fires in this harness, so a ticket write landing distinct is a genuine bypass of a live merge
    step, not an artifact of an inert offline policy.
    """
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")

    res = _post(client, {
        "insight": NEWCOMER_TEXT, "source": SNAPSHOT,
        "category": "requirement", "onConflict": "surface",  # no build_state / requirement_id
    })
    assert res.status_code == 200, res.text
    body = res.json()

    # The exact production signature: an id the caller never wrote, and a destroyed incumbent.
    assert body["action"] == "merged"
    assert body["id"] == incumbent
    assert _rows(conn, org)[incumbent][0] != INCUMBENT_TEXT


# --------------------------------------------------------------------------- AC1

def test_ticket_write_with_surface_lands_distinct_and_rejects_nothing(env):
    """AC1 — a ticket write with ``on_conflict="surface"`` lands as a DISTINCT fact: a new id, the
    topically-similar incumbent byte-identical afterwards, and no other fact's state changed."""
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")
    bystander = _seed(graph, "unrelated ticket about the settings dialog", rid="R31")
    before = _rows(conn, org)

    res = _post(client, _ticket(NEWCOMER_TEXT, "R42"))
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["action"] == "added"
    assert body["id"] not in before                       # a NEW id, never a caller-unknown one
    assert body["contradictionsSurfaced"] == 0
    assert body.get("factsRejected", []) == []             # it rejected nothing, and says so

    after = _rows(conn, org)
    assert len(after) == len(before) + 1                  # the ticket really landed
    assert after[incumbent][0] == INCUMBENT_TEXT          # byte-identical content
    assert after[bystander][0] == before[bystander][0]
    assert {i: r[1] for i, r in after.items() if i in before} == {
        i: r[1] for i, r in before.items()
    }, "a ticket write changed the state of a pre-existing fact"


# --------------------------------------------------------------------------- AC2

def test_ticket_without_category_lands_distinct_and_is_stamped_requirement(env):
    """AC2 — THE production shape: no ``category`` at all, only ``meta.requirement_id`` +
    ``meta.build_state``. It must still land distinct AND come back stored as a requirement."""
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")
    before = _rows(conn, org)

    body = _ticket(NEWCOMER_TEXT, "R42")
    del body["category"]                                   # <- the one difference that corrupted prod
    res = _post(client, body)
    assert res.status_code == 200, res.text
    out = res.json()

    assert out["action"] == "added"
    assert out["id"] not in before
    after = _rows(conn, org)
    assert after[incumbent][0] == INCUMBENT_TEXT           # incumbent untouched
    text, state, category, meta = after[out["id"]]
    assert text == NEWCOMER_TEXT
    assert state == "active"
    assert category == "requirement", (
        "an identity-carrying ticket must be stamped category='requirement' so it stays queryable "
        "as the ticket it always was"
    )
    assert meta.get("requirement_id") == "R42"

    # And it is visible through the query the factory actually uses.
    read = client.get("/facts/by", params={"category": "requirement"}, headers=HEADERS)
    assert read.status_code == 200, read.text
    assert out["id"] in {f["id"] for f in read.json()["facts"]}


def test_ticket_with_wrong_category_is_still_routed_as_a_ticket(env):
    """AC2 (sibling) — a MISLABELLED ticket (``category="learning"`` + identity keys) is routed by
    its identity too. The label is not trusted; the identity keys are self-describing."""
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")

    body = _ticket(NEWCOMER_TEXT, "R43")
    body["category"] = "learning"
    res = _post(client, body)
    assert res.status_code == 200, res.text
    out = res.json()

    assert out["action"] == "added"
    assert out["id"] != incumbent
    assert _rows(conn, org)[incumbent][0] == INCUMBENT_TEXT
    assert _rows(conn, org)[out["id"]][2] == "requirement"


# --------------------------------------------------------------------------- AC3

def test_ticket_write_never_rejects_a_fact_the_caller_did_not_name(env):
    """AC3 — several active facts are seeded and a new ticket is written; every fact the caller
    did not name is still ``active``. In prod the same call flipped three of them to ``rejected``."""
    client, conn, org, graph = env
    seeded = [
        _seed(graph, INCUMBENT_TEXT, rid="R30"),
        _seed(graph, "the source discriminator dedups scraped rows by sku on ingest", rid="R31"),
        _seed(graph, "the source discriminator dedups scraped rows by title on ingest", rid="R32"),
        _seed(graph, "scraped rows are written by the ingest worker", category="learning"),
    ]

    res = _post(client, _ticket(NEWCOMER_TEXT, "R42"))
    assert res.status_code == 200, res.text

    after = _rows(conn, org)
    assert [after[f][1] for f in seeded] == ["active"] * len(seeded)
    assert [after[f][0] for f in seeded] == [
        INCUMBENT_TEXT,
        "the source discriminator dedups scraped rows by sku on ingest",
        "the source discriminator dedups scraped rows by title on ingest",
        "scraped rows are written by the ingest worker",
    ]


# --------------------------------------------------------------------------- AC4

def test_same_requirement_id_restatement_updates_in_place(env):
    """AC4 — the other half of the identity contract: a restatement of the SAME requirement_id
    updates the one fact in place (one fact, not two), so the fix cannot regress into duplicates."""
    client, conn, org, graph = env

    first = _post(client, _ticket("pagination stops at the last page", "R7"))
    assert first.status_code == 200, first.text
    second = _post(client, _ticket("pagination stops at the last page and never loops", "R7"))
    assert second.status_code == 200, second.text

    assert first.json()["action"] == "added"
    assert second.json()["action"] == "updated"
    assert second.json()["id"] == first.json()["id"]

    rows = _rows(conn, org)
    assert len(rows) == 1
    assert rows[first.json()["id"]][0] == "pagination stops at the last page and never loops"


def test_restatement_without_category_also_updates_in_place(env):
    """AC4 (sibling) — the category-less shape must hit the SAME identity key, not mint a twin."""
    client, conn, org, graph = env

    body = _ticket("pagination stops at the last page", "R7")
    del body["category"]
    first = _post(client, body)
    body2 = _ticket("pagination stops at the last page and never loops", "R7")
    del body2["category"]
    second = _post(client, body2)

    assert first.json()["action"] == "added"
    assert second.json()["action"] == "updated"
    assert second.json()["id"] == first.json()["id"]
    assert len(_rows(conn, org)) == 1


# --------------------------------------------------------------------------- AC5

def test_write_reports_every_fact_whose_state_it_actually_changed(env):
    """AC5 — the report must not UNDER-REPORT. Prod returned ``contradictionsSurfaced: 0`` while
    three facts were being flipped to ``rejected``, because only ``contradiction`` edges were
    counted and an auto-resolved rejection is linked by a ``contradicted_by`` edge.

    The contract asserted here is behavioural, not lexical: whatever the write reports must ACCOUNT
    FOR every fact whose state it actually changed. Every fact that flipped is named, and nothing
    is named that did not flip."""
    client, conn, org, graph = env
    victims = [
        _seed(graph, INCUMBENT_TEXT, rid="R30"),
        _seed(graph, "the source discriminator dedups scraped rows by sku on ingest", rid="R31"),
        _seed(graph, "the source discriminator dedups scraped rows by title on ingest", rid="R32"),
    ]
    before = {i: r[1] for i, r in _rows(conn, org).items()}

    # A NON-ticket auto_resolve write: the ConflictOverwriter lane, which rejects every fact it
    # confirms as contradicting. This is the reporting path, exercised where it really rejects.
    res = client.post("/insights", json={
        "insight": NEWCOMER_TEXT, "source": SNAPSHOT,
        "category": "requirement", "onConflict": "auto_resolve",
    }, headers=HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()

    after = _rows(conn, org)
    changed = {i for i, r in after.items() if i in before and r[1] != before[i]}
    rejected = {i for i in changed if after[i][1] == "rejected"}
    assert rejected, (
        "harness check: the auto_resolve lane rejected nothing, so this test would be vacuous"
    )
    assert rejected <= set(victims)
    assert changed == rejected, "auto_resolve should only ever change state by rejecting"

    reported = set(body.get("factsRejected") or [])
    assert reported == changed, (
        f"reported {sorted(reported)} but {sorted(changed)} facts changed state — the write's "
        f"report must account for every fact it damaged"
    )
    assert len(body.get("factsRejected") or []) == len(changed)
    # The winner never reports itself as its own loser.
    assert body["id"] not in reported


def test_ticket_write_reports_zero_because_it_really_rejected_nothing(env):
    """AC5 (the ticket half) — a ticket write reports ``0`` and that ``0`` is TRUE: no fact
    changed state. A truthful zero, unlike prod's zero that hid three rejections."""
    client, conn, org, graph = env
    for i, text in enumerate([
        INCUMBENT_TEXT,
        "the source discriminator dedups scraped rows by sku on ingest",
        "the source discriminator dedups scraped rows by title on ingest",
    ]):
        _seed(graph, text, rid=f"R{30 + i}")
    before = {i: r[1] for i, r in _rows(conn, org).items()}

    res = _post(client, _ticket(NEWCOMER_TEXT, "R42"))
    assert res.status_code == 200, res.text
    assert res.json()["contradictionsSurfaced"] == 0
    assert res.json().get("factsRejected", []) == []

    after = _rows(conn, org)
    assert {i: r[1] for i, r in after.items() if i in before} == before


# --------------------------------------------------------------------------- AC7

def test_raw_ticket_write_is_visible_to_facts_by_immediately(env):
    """AC7 — a ``raw=True`` ticket write is visible to ``/facts/by`` in a BOUNDED window.

    The write path is SYNCHRONOUS in this harness (the endpoint commits on autocommit before it
    returns), so the asserted bound is ZERO retries: the very first read-back must see it. No
    sleep, no polling — an unbounded sleep-and-hope would hide exactly the latency regression this
    is meant to catch.
    """
    client, conn, org, graph = env

    body = _ticket(NEWCOMER_TEXT, "R42")
    body["raw"] = True
    res = _post(client, body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["retrievable"] is True                       # the endpoint's own read-your-writes claim

    read = client.get("/facts/by", params={"category": "requirement"}, headers=HEADERS)
    assert read.status_code == 200, read.text
    facts = {f["id"]: f for f in read.json()["facts"]}
    assert out["id"] in facts, "raw ticket write not visible on the FIRST read-back"
    assert facts[out["id"]]["text"] == NEWCOMER_TEXT


# --------------------------------------------------------------------------- AC8

def _active_rid_collisions(conn, org) -> dict[str, list[str]]:
    """requirement_id -> ids of ACTIVE facts carrying it, for every id claimed more than once."""
    by_rid: dict[str, list[str]] = {}
    for fid, (_text, state, _cat, meta) in _rows(conn, org).items():
        rid = str((meta or {}).get("requirement_id") or "").strip()
        if rid and state == "active":
            by_rid.setdefault(rid, []).append(fid)
    return {rid: ids for rid, ids in by_rid.items() if len(ids) > 1}


def test_two_active_facts_never_share_a_requirement_id(env):
    """AC8 — detection test for the observable residue. Two ACTIVE facts sharing one
    ``meta.requirement_id`` inside a snapshot is a corruption signal (it means a ticket was
    duplicated or re-minted). The fixed write path cannot produce it, however it is addressed:
    with the category, without it, mislabelled, or restated."""
    client, conn, org, graph = env
    _seed(graph, INCUMBENT_TEXT, rid="R30")

    with_category = _ticket(NEWCOMER_TEXT, "R30")
    no_category = _ticket(NEWCOMER_TEXT + " and by sku", "R30")
    del no_category["category"]
    mislabelled = _ticket(NEWCOMER_TEXT + " and by title", "R30")
    mislabelled["category"] = "learning"

    for body in (with_category, no_category, mislabelled, with_category):
        assert _post(client, body).status_code == 200
        assert _active_rid_collisions(conn, org) == {}, (
            "two ACTIVE facts now share a requirement_id — the corruption residue"
        )

    # All four writes addressed R30, so exactly one fact carries it.
    rids = [m.get("requirement_id") for _t, _s, _c, m in _rows(conn, org).values()]
    assert rids.count("R30") == 1


# ======================================================================== POST /insights/batch
#
# The BULK lane had the same hole. ``POST /insights`` was fixed and is covered above; the batch
# route collected every item into ``to_decide`` and handed it to the reconciled batch writer,
# whose policy under ``onConflict="surface"`` is ``default_write_policy()`` — the one carrying the
# additive ``Augmenter``. So a ticket batched at a plan snapshot could be folded into a
# topically-similar existing ticket exactly as the singular route did, only N at a time.
#
# The fix detects ticket items with ``_is_requirement_ticket`` and upserts them serially inline
# through ``_requirement_upsert`` on a lazily-built REDACT-ONLY graph, BEFORE the ``raw`` meta
# stamp and before anything is collected into ``to_decide``.
#
# These reuse the module's rig verbatim (``TopicEmbedder`` + the always-merging
# ``default_write_policy`` + the always-yes LLM), so a ticket landing distinct here is a real
# bypass of a live merge step. ``test_control_non_ticket_batch_item_really_does_merge`` proves
# that for the batch lane SPECIFICALLY — the batch writer builds its own policy via
# ``_insight_write_policy``/``policy_factory`` on cloned graphs, so the singular route's control
# does not vouch for this one.


def _ticket_item(insight, rid, **meta):
    """A batch ITEM in the add-a-ticket shape (``onConflict`` is batch-level here)."""
    item = _ticket(insight, rid, **meta)
    item.pop("onConflict")
    return item


# --------------------------------------------------------------------------- the rig is live

def test_control_non_ticket_batch_item_really_does_merge(env):
    """CONTROL — not a fix assertion. A NON-ticket batch item, same text and category, differing
    ONLY by the missing ticket identity keys, IS additively merged into the incumbent through the
    batch writer and DOES corrupt it.

    Without this, every batch assertion below could pass against an inert offline policy.
    """
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")

    res = _batch(
        client,
        [{"insight": NEWCOMER_TEXT, "source": SNAPSHOT, "category": "requirement"}],
        onConflict="surface",
    )
    assert res.status_code == 200, res.text
    item = res.json()["results"][0]

    assert item["ok"] is True
    assert item["action"] == "merged"
    assert item["id"] == incumbent                      # an id the caller never wrote
    assert _rows(conn, org)[incumbent][0] != INCUMBENT_TEXT   # and a destroyed incumbent


# --------------------------------------------------------------------------- AC1 (batch)

def test_batched_ticket_lands_distinct_and_leaves_the_incumbent_alone(env):
    """A ticket inside a ``onConflict="surface"`` batch lands as a DISTINCT fact: a new id, the
    topically-similar incumbent byte-identical afterwards, and nothing else's state changed."""
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")
    bystander = _seed(graph, "unrelated ticket about the settings dialog", rid="R31")
    before = _rows(conn, org)

    res = _batch(client, [_ticket_item(NEWCOMER_TEXT, "R42")], onConflict="surface")
    assert res.status_code == 200, res.text
    item = res.json()["results"][0]

    assert item["ok"] is True
    assert item["action"] == "added"
    assert item["id"] not in before                     # never a caller-unknown id
    assert item["contradictionsSurfaced"] == 0
    assert item["retrievable"] is True

    after = _rows(conn, org)
    assert len(after) == len(before) + 1                # the ticket really landed
    assert after[item["id"]][0] == NEWCOMER_TEXT
    assert after[incumbent][0] == INCUMBENT_TEXT        # byte-identical content
    assert after[bystander][0] == before[bystander][0]
    assert {i: r[1] for i, r in after.items() if i in before} == {
        i: r[1] for i, r in before.items()
    }, "a batched ticket write changed the state of a pre-existing fact"


# --------------------------------------------------------------------------- AC2 (batch)

def test_batched_ticket_without_category_lands_distinct_and_is_stamped_requirement(env):
    """THE production shape, batched: no ``category`` at all, only ``meta.requirement_id`` +
    ``meta.build_state``. It must land distinct AND be stored as a requirement."""
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")
    before = _rows(conn, org)

    item = _ticket_item(NEWCOMER_TEXT, "R42")
    del item["category"]                                # <- the one difference that corrupted prod
    res = _batch(client, [item], onConflict="surface")
    assert res.status_code == 200, res.text
    out = res.json()["results"][0]

    assert out["ok"] is True
    assert out["action"] == "added"
    assert out["id"] not in before

    after = _rows(conn, org)
    assert after[incumbent][0] == INCUMBENT_TEXT        # incumbent untouched
    text, state, category, meta = after[out["id"]]
    assert text == NEWCOMER_TEXT
    assert state == "active"
    assert category == "requirement", (
        "an identity-carrying batched ticket must be stamped category='requirement' so it stays "
        "queryable as the ticket it always was"
    )
    assert meta.get("requirement_id") == "R42"

    # Visible through the categorical query the factory actually reads tickets with.
    read = client.get("/facts/by", params={"category": "requirement"}, headers=HEADERS)
    assert read.status_code == 200, read.text
    assert out["id"] in {f["id"] for f in read.json()["facts"]}


# --------------------------------------------------------------------------- AC4 (batch)

def test_batched_restatement_of_a_known_requirement_id_updates_in_place(env):
    """The other half of the identity contract, in bulk: a batched item whose ``requirement_id``
    already exists UPDATES that one fact — one fact, not two — and reports ``action="updated"``."""
    client, conn, org, graph = env

    first = _batch(
        client, [_ticket_item("pagination stops at the last page", "R7")], onConflict="surface"
    )
    assert first.status_code == 200, first.text
    second = _batch(
        client,
        [_ticket_item("pagination stops at the last page and never loops", "R7")],
        onConflict="surface",
    )
    assert second.status_code == 200, second.text

    added, updated = first.json()["results"][0], second.json()["results"][0]
    assert added["action"] == "added"
    assert updated["action"] == "updated"
    assert updated["id"] == added["id"]

    rows = _rows(conn, org)
    assert len(rows) == 1, "the batch lane minted a twin instead of upserting on the identity"
    assert rows[added["id"]][0] == "pagination stops at the last page and never loops"
    assert _active_rid_collisions(conn, org) == {}


# --------------------------------------------------------------------------- mixed batch

def test_mixed_batch_fills_every_result_at_its_own_input_index(env):
    """Tickets interleaved with ordinary insights: every result comes back at the correct index.

    Tickets are filled in during a DIFFERENT pass than the batch-writer items (inline in pass 1 vs.
    scattered back through ``decide_index`` in pass 2), so an off-by-one would silently
    mis-attribute results — a caller correlating ids to its own input would then update the wrong
    ticket. The index is verified against ground truth: the STORED text at each returned id must be
    the text submitted at that index.
    """
    client, conn, org, graph = env
    incumbent = _seed(graph, INCUMBENT_TEXT, rid="R30")

    # The ordinary items are mutually disjoint under the TopicEmbedder (pairwise cosine <= 0.13,
    # well under the 0.45 recall floor) and share nothing with the incumbent, so THEY land distinct
    # and each id maps to exactly one input text. The tickets deliberately overlap the incumbent,
    # so if one ever reached the reconciled writer the Augmenter would have a real target.
    items = [
        {"insight": "avatar upload accepts png images", "source": SNAPSHOT},
        _ticket_item(NEWCOMER_TEXT, "R42"),
        {"insight": "invoice emails include pdf attachment", "source": SNAPSHOT},
        _ticket_item(NEWCOMER_TEXT + " and by sku", "R43"),
        {"insight": "session cookie expires after two weeks", "source": SNAPSHOT},
    ]
    ticket_indexes = {1, 3}

    res = _batch(client, items, onConflict="surface")
    assert res.status_code == 200, res.text
    body = res.json()
    results = body["results"]
    assert body["count"] == len(items)
    assert len(results) == len(items)
    assert all(r["ok"] for r in results), results

    after = _rows(conn, org)
    for i, (item, result) in enumerate(zip(items, results)):
        assert result["id"] in after, f"item {i} reported an id that is not in the snapshot"
        text, _state, category, _meta = after[result["id"]]
        assert text == item["insight"], (
            f"result[{i}] points at {text!r} but item {i} submitted {item['insight']!r} — "
            f"the batch mis-attributed a result across the two fill passes"
        )
        if i in ticket_indexes:
            # Identity-routed: stamped requirement, and no conflict mode was honored.
            assert category == "requirement"
            assert result["onConflict"] == "n/a"
        else:
            # Reconciled: the batch-level conflict mode is reported back.
            assert result["onConflict"] == "surface"

    # Distinct ids all round, and the incumbent the tickets overlap is untouched.
    assert len({r["id"] for r in results}) == len(items)
    assert after[incumbent][0] == INCUMBENT_TEXT
    assert _active_rid_collisions(conn, org) == {}


# --------------------------------------------------------------------------- raw interaction

def test_raw_batch_does_not_stamp_forced_on_a_ticket(env):
    """``raw: true`` at the batch level must NOT reach a ticket item.

    Ticket routing deliberately runs BEFORE the raw meta stamp, mirroring ``POST /insights`` (which
    routes on category/meta before it ever reads ``raw``). The identity-keyed path is already
    pipeline-exempt, so ``meta['forced']`` would be a lie about how the fact was written and would
    make a ticket round-trip differently depending on how the batch happened to be flagged.

    The non-ticket item in the SAME batch is asserted to DO get ``forced: true`` — otherwise this
    would pass simply because the raw lane stopped stamping anything.
    """
    client, conn, org, graph = env

    res = _batch(
        client,
        [
            _ticket_item(NEWCOMER_TEXT, "R42"),
            {"insight": "avatar upload accepts png images", "source": SNAPSHOT},
        ],
        raw=True,
    )
    assert res.status_code == 200, res.text
    ticket_res, plain_res = res.json()["results"]
    assert ticket_res["ok"] and plain_res["ok"], res.text

    after = _rows(conn, org)
    ticket_meta = after[ticket_res["id"]][3]
    assert "forced" not in ticket_meta, (
        f"a raw batch stamped meta['forced'] on an identity-routed ticket: {ticket_meta!r}"
    )
    assert ticket_meta.get("requirement_id") == "R42"
    assert ticket_meta.get("build_state") == "incomplete"
    assert after[ticket_res["id"]][2] == "requirement"

    assert after[plain_res["id"]][3].get("forced") is True, (
        "harness check: the raw lane stamped nothing at all, so the ticket assertion is vacuous"
    )


# --------------------------------------------------------------------------- AC3 (batch)

def test_batched_ticket_never_rejects_a_fact_the_caller_did_not_name(env):
    """A batched ticket write flips NOTHING to ``rejected``, and its per-item result says so."""
    client, conn, org, graph = env
    seeded = [
        _seed(graph, INCUMBENT_TEXT, rid="R30"),
        _seed(graph, "the source discriminator dedups scraped rows by sku on ingest", rid="R31"),
        _seed(graph, "the source discriminator dedups scraped rows by title on ingest", rid="R32"),
        _seed(graph, "scraped rows are written by the ingest worker", category="learning"),
    ]
    before = {i: r[1] for i, r in _rows(conn, org).items()}

    res = _batch(client, [_ticket_item(NEWCOMER_TEXT, "R42")], onConflict="surface")
    assert res.status_code == 200, res.text
    item = res.json()["results"][0]

    assert item["contradictionsSurfaced"] == 0
    assert item.get("factsRejected", []) == []

    after = _rows(conn, org)
    assert [after[f][1] for f in seeded] == ["active"] * len(seeded)
    assert {i: r[1] for i, r in after.items() if i in before} == before
    assert [after[f][0] for f in seeded] == [
        INCUMBENT_TEXT,
        "the source discriminator dedups scraped rows by sku on ingest",
        "the source discriminator dedups scraped rows by title on ingest",
        "scraped rows are written by the ingest worker",
    ]
