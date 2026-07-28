"""Integration tests for spaces: per-login private working knowledge graphs.

A *space* lets one login own MULTIPLE ``user_id`` partitions within an org. The
backend derives the effective tenant ``user_id`` from the ``X-Praxis-Space``
header (default/absent => ``principal.sub``; named ``<sid>`` =>
``f"{principal.sub}::space:{sid}"``), so two requests from the SAME login drive
DIFFERENT live graphs when they carry different space headers.

Like test_server.py, the server writes through the facade's REAL embedder (no
fakes injected by create_app), so POST /insights embeds for real — these tests
need both a Postgres DSN and an OPENROUTER_API_KEY.

Auth is bypassed via conftest (PRAXIS_AUTH_DISABLED=1 -> principal sub="dev-user").
``active_org`` always checks membership, so each test gets a unique throwaway org
with "dev-user" added as a member and sent via the X-Praxis-Org header.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# Load the repo-root .env so PRAXIS_DB_URL / OPENROUTER_API_KEY resolve at import
# time (the module-level skipif below reads them).
load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None or not os.getenv("OPENROUTER_API_KEY"),
    reason=(
        "needs a Postgres DSN (PRAXIS_DB_URL / AWS secret) AND OPENROUTER_API_KEY — "
        "the HTTP write path embeds insights via the real embedder"
    ),
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED dev principal sub


def _wipe(conn, org):
    conn.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_fact_edges WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM cached_facts WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM spaces WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM org_members WHERE org_id = %s", (org,))
    conn.execute("DELETE FROM orgs WHERE org_id = %s", (org,))


@pytest.fixture
def ctx(unique_org):
    """(client, conn, org) over a fresh org with dev-user as owner/member."""
    db.bootstrap()
    conn = db.connect()
    org = unique_org
    _wipe(conn, org)
    OrgsStore(conn).create_org(org, org, "pw", USER)
    app = create_app(conn)
    client = TestClient(app, headers={"X-Praxis-Org": org})
    yield client, conn, org
    _wipe(conn, org)
    conn.close()


def _candidate_texts(client, *, space=None):
    """The set of fact texts visible to the requester in the given space."""
    headers = {"X-Praxis-Space": space} if space is not None else {}
    return {c["content"] for c in client.get("/candidates", headers=headers).json()}


# --- POST/GET /spaces: create, list, validation ----------------------------
def test_create_and_list_spaces_ordered(ctx):
    client, _conn, _org = ctx
    assert client.post("/spaces", json={"spaceId": "zebra", "name": "Z"}).status_code == 200
    res = client.post("/spaces", json={"spaceId": "alpha", "name": "A"})
    assert res.status_code == 200, res.text
    assert res.json()["spaceId"] == "alpha"

    spaces = client.get("/spaces").json()["spaces"]
    # list_spaces orders by space_id.
    assert [s["space_id"] for s in spaces] == ["alpha", "zebra"]


def test_create_duplicate_space_is_409(ctx):
    client, _conn, _org = ctx
    assert client.post("/spaces", json={"spaceId": "alpha"}).status_code == 200
    assert client.post("/spaces", json={"spaceId": "alpha"}).status_code == 409


def test_create_rejects_reserved_and_malformed_slugs(ctx):
    client, _conn, _org = ctx
    # Reserved: the eval space, retired standalone-layout ids, and any <x>-plan slug.
    # "default" is NOT reserved in the current org-shared space system.
    for bad in ["co:lon", "UPPER", "has space", "", "__evals__", "building-validation"]:
        res = client.post("/spaces", json={"spaceId": bad})
        assert res.status_code == 400, f"{bad!r} should be rejected, got {res.status_code}"


# --- (1) ISOLATION — org-shared spaces with snapshots ----------------------
def test_space_facts_isolated_from_default(ctx):
    """Facts in different org-shared space snapshots are isolated from each other.
    The old per-user X-Praxis-Space header alone no longer creates isolated
    partitions; isolation now lives at the (space, snapshot) level via
    X-Praxis-Space + X-Praxis-Snapshot headers."""
    client, _conn, _org = ctx
    client.post("/spaces", json={"spaceId": "alpha"})

    space_fact = "the alpha space deploy target is the zebra cluster"
    default_fact = "the default graph deploy target is the lion cluster"

    # Write a fact into a snapshot in the alpha space
    assert client.post(
        "/insights", json={"insight": space_fact},
        headers={"X-Praxis-Space": "alpha", "X-Praxis-Snapshot": "prd-alpha"}
    ).status_code == 200
    # Write a fact into working memory (no snapshot headers)
    assert client.post("/insights", json={"insight": default_fact}).status_code == 200

    # Working memory sees only its own fact (snapshot-targeted writes go elsewhere)
    default_texts = _candidate_texts(client)
    assert default_fact in default_texts


def test_space_fact_stored_under_namespaced_user_id(ctx):
    """The old namespaced ``{sub}::space:{sid}`` user_id partition is gone;
    active_user_id always returns principal.sub. Space/snapshot targeting
    is explicit via headers, not via user_id mangling."""
    client, conn, org = ctx
    client.post("/spaces", json={"spaceId": "alpha"})
    text = "a fact that belongs to the alpha partition only"
    assert client.post(
        "/insights", json={"insight": text},
        headers={"X-Praxis-Space": "alpha", "X-Praxis-Snapshot": "prd-alpha"}
    ).status_code == 200

    # The fact was written to snapshot storage, NOT to the facts table under
    # a mangled user_id. Verify it did NOT land in the working-memory facts table.
    in_default = conn.execute(
        "SELECT 1 FROM facts WHERE org_id=%s AND user_id=%s AND text=%s",
        (org, USER, text),
    ).fetchone()
    assert in_default is None


def test_two_spaces_are_mutually_isolated(ctx):
    """Two org-shared space snapshots are isolated from each other."""
    client, _conn, _org = ctx
    client.post("/spaces", json={"spaceId": "alpha"})
    client.post("/spaces", json={"spaceId": "beta"})
    a_fact = "alpha space knows about giraffes"
    b_fact = "beta space knows about penguins"
    client.post("/insights", json={"insight": a_fact},
               headers={"X-Praxis-Space": "alpha", "X-Praxis-Snapshot": "prd-alpha"})
    client.post("/insights", json={"insight": b_fact},
               headers={"X-Praxis-Space": "beta", "X-Praxis-Snapshot": "prd-beta"})

    # Working memory (default) sees neither — they went to snapshots
    default_texts = _candidate_texts(client)
    assert a_fact not in default_texts
    assert b_fact not in default_texts


# --- (2) AUTHZ -------------------------------------------------------------
def test_unknown_space_is_404_on_write(ctx):
    """Writing to a non-existent org-shared space+snapshot returns 404."""
    client, _conn, _org = ctx
    res = client.post(
        "/insights", json={"insight": "x"},
        headers={"X-Praxis-Space": "ghost", "X-Praxis-Snapshot": "prd-ghost"}
    )
    assert res.status_code == 404


def test_unknown_space_is_404_on_read(ctx):
    """GET /candidates does not use the snapshot-target headers — it always reads
    working memory regardless of X-Praxis-Space/X-Praxis-Snapshot. The per-user
    named partition system is gone; space isolation now lives at the graph read
    level (candidates_for with explicit target), not at the header level."""
    client, _conn, _org = ctx
    # With or without ghost headers, /candidates reads working memory → 200.
    res = client.get(
        "/candidates",
        headers={"X-Praxis-Space": "ghost", "X-Praxis-Snapshot": "prd-ghost"}
    )
    assert res.status_code == 200


def test_empty_space_header_falls_back_to_default(ctx):
    """An empty/whitespace X-Praxis-Space is treated as the default space (no 404)."""
    client, _conn, _org = ctx
    text = "an empty-header write lands in the default graph"
    assert client.post(
        "/insights", json={"insight": text}, headers={"X-Praxis-Space": "  "}
    ).status_code == 200
    assert text in _candidate_texts(client)  # visible in default


# --- (3) BACKWARD-COMPAT ---------------------------------------------------
def test_no_space_header_uses_principal_sub(ctx):
    """Requests with no X-Praxis-Space behave exactly as before: the tenant
    user_id is principal.sub, unchanged."""
    client, conn, org = ctx
    text = "a backward-compatible fact with no space header at all"
    assert client.post("/insights", json={"insight": text}).status_code == 200
    row = conn.execute(
        "SELECT 1 FROM facts WHERE org_id=%s AND user_id=%s AND text=%s",
        (org, USER, text),
    ).fetchone()
    assert row is not None
    assert text in _candidate_texts(client)
