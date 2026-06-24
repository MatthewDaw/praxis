"""Migration test: legacy ``decayed`` rows read back as ``rejected`` (FR-002).

Needs a Postgres DSN (same gate as the other facts tests). Seeds a row at the
pre-rename value via raw SQL (``facts.state`` is bare text), runs the idempotent
rename migration, and asserts the new value. Re-running the migration is a no-op.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db
from migrations.m2026_06_23_reject_rename import _rename_state

pytestmark = pytest.mark.skipif(
    db.resolve_dsn() is None,
    reason="no Postgres DSN available (set PRAXIS_DB_URL or configure AWS secret)",
)

USER = "dev-user"


@pytest.fixture
def conn(unique_org):
    db.bootstrap()
    c = db.connect()
    org = unique_org
    c.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    c.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    yield c, org
    c.execute("DELETE FROM fact_edges WHERE org_id = %s", (org,))
    c.execute("DELETE FROM facts WHERE org_id = %s", (org,))
    c.close()


def _state_of(c, org, fid):
    row = c.execute(
        "SELECT state FROM facts WHERE org_id = %s AND id = %s", (org, fid)
    ).fetchone()
    return row[0] if row else None


def test_decayed_row_reads_back_as_rejected(conn):
    c, org = conn
    c.execute(
        "INSERT INTO facts (id, org_id, user_id, text, state) VALUES (%s, %s, %s, %s, %s)",
        ("legacy1", org, USER, "A retired fact.", "decayed"),
    )
    assert _state_of(c, org, "legacy1") == "decayed"  # seeded at the legacy value

    _rename_state(c, "facts")
    assert _state_of(c, org, "legacy1") == "rejected"

    # Idempotent: a second run changes nothing.
    _rename_state(c, "facts")
    assert _state_of(c, org, "legacy1") == "rejected"
