"""Write-path reliability (H13.2 concurrency + H13.3 membership durability).

The server hands each worker thread its own autocommit connection (see
``app._ConnProxy``) instead of sharing one process-wide. These tests pin the two
properties that buys us:

- **Isolation (H13.2):** concurrent threads get *distinct* connections, so a
  stuck/erroring request can't wedge the connection every other request shares.
- **Self-heal (H13.3):** a thread whose connection died (DB restart / dropped
  socket) transparently gets a fresh one on its next use.
- **Durability (H13.3):** org membership lives in Postgres, so it survives a
  process restart — a brand-new app over fresh connections still sees it.

These need a real Postgres DSN; auth is bypassed via conftest (dev-user).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

from knowledge.serve import db  # noqa: E402
from knowledge.serve.app import create_app  # noqa: E402
from knowledge.serve.orgs_store import OrgsStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="needs a Postgres DSN (PRAXIS_DB_URL / AWS secret)",
)

USER = "dev-user"  # the PRAXIS_AUTH_DISABLED=1 dev principal (see conftest)


def _wipe(org: str) -> None:
    conn = db.connect()
    for tbl in ("fact_edges", "facts", "cached_fact_edges", "cached_facts",
                "mounted_snapshots", "org_members", "orgs"):
        conn.execute(f"DELETE FROM {tbl} WHERE org_id = %s", (org,))
    conn.close()


@pytest.fixture
def org(request):
    name = "rel_" + request.node.name
    _wipe(name)
    yield name
    _wipe(name)


def test_per_thread_connections_are_isolated_and_reopen():
    # TLS mode (no conn passed) — each thread resolves its own connection.
    app = create_app()
    get_conn = app.state.get_conn

    grabbed: dict[str, object] = {}

    def grab(key: str) -> None:
        grabbed[key] = get_conn()

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    try:
        # H13.2: two threads never share one connection.
        assert grabbed["a"] is not grabbed["b"]

        # H13.3: after the DB drops a thread's connection, the next use reopens it.
        c1 = get_conn()
        c1.close()
        c2 = get_conn()
        assert c2 is not c1
        assert not c2.closed
        assert c2.execute("SELECT 1").fetchone()[0] == 1
    finally:
        for c in (*grabbed.values(), get_conn()):
            try:
                c.close()
            except Exception:
                pass


def test_membership_survives_process_restart(org):
    # "Process 1" creates the org (dev-user becomes owner) and exits.
    setup = db.connect()
    OrgsStore(setup).create_org(org, org, "pw", USER)
    setup.close()

    # "Process 2": a brand-new app over fresh per-thread connections — membership
    # is durable in Postgres, so the org is still there and member-gated reads pass.
    client = TestClient(create_app(), headers={"X-Praxis-Org": org})
    me = client.get("/me").json()
    assert any(o["org_id"] == org for o in me["orgs"]), me
    assert client.get("/candidates").status_code == 200  # 403 if membership lost


def test_concurrent_reads_do_not_cascade(org):
    setup = db.connect()
    OrgsStore(setup).create_org(org, org, "pw", USER)
    setup.close()

    client = TestClient(create_app(), headers={"X-Praxis-Org": org})

    def hit(_):
        r = client.get("/me")
        return r.status_code

    # A burst of concurrent requests must all succeed — with one shared connection
    # they serialized on (and could wedge) it; per-thread connections don't.
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(hit, range(48)))
    assert codes == [200] * 48, codes
