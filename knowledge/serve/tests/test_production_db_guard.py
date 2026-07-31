"""The test suite must refuse to run against a non-local database.

Covers ``db.require_local_dsn`` (the guard the repo-root ``conftest.py`` calls in
``pytest_configure``) directly, so it needs no database of its own: the whole
point is that the guard is what stands between ``pytest -q`` and production RDS.
"""

from __future__ import annotations

import pytest

from knowledge.serve import db

PROD_DSN = (
    "postgresql://praxis:hunter2@praxisknowledgegraphdbstack-kginstance6579381f"
    "-uscylfvcgzvp.c81egg0ga870.us-east-1.rds.amazonaws.com:5432/praxis_kg"
)
LOCAL_DSN = "postgresql://praxis:praxis@localhost:5433/praxis_kg"
CI_DSN = "postgresql://praxis:praxis@localhost:5432/praxis_kg"


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    monkeypatch.delenv(db.ALLOW_REMOTE_TESTS_ENV, raising=False)
    # The guard only fires on a resolved DSN, so the DB-disabled opt-out would
    # mask it — each test sets the DSN state it wants explicitly.
    monkeypatch.delenv("PRAXIS_DB_DISABLED", raising=False)


@pytest.mark.parametrize(
    "dsn",
    [
        LOCAL_DSN,
        CI_DSN,
        "postgresql://praxis:praxis@127.0.0.1:5433/praxis_kg",
        "postgresql://praxis:praxis@[::1]:5433/praxis_kg",
        "postgresql://praxis:praxis@LOCALHOST:5433/praxis_kg",
        "postgresql+psycopg://praxis:praxis@localhost:5433/praxis_kg",
        "host=localhost port=5433 dbname=praxis_kg user=praxis",
        "dbname=praxis_kg",  # keyword DSN, no host -> local unix socket
    ],
)
def test_local_dsns_pass(dsn):
    assert db.is_local_dsn(dsn)
    db.require_local_dsn(dsn)  # does not raise


@pytest.mark.parametrize(
    "dsn",
    [
        PROD_DSN,
        # A *new* RDS instance: a denylist of known prod hostnames would miss it.
        "postgresql://u:p@some-other-instance.us-west-2.rds.amazonaws.com:5432/praxis_kg",
        "postgresql://u:p@db.internal.example.com:5432/praxis_kg",
        "host=prod.example.com dbname=praxis_kg",
        # Loopback-looking userinfo must not fool the host parse.
        "postgresql://localhost:pw@evil.example.com:5432/praxis_kg",
        "postgresql://u:p@localhost.evil.example.com:5432/praxis_kg",
        # Unparseable -> fail closed.
        "not-a-dsn",
        "",
        None,
    ],
)
def test_remote_and_unparseable_dsns_are_refused(dsn):
    assert not db.is_local_dsn(dsn)
    with pytest.raises(db.RemoteDatabaseRefused):
        db.require_local_dsn(dsn)


def test_refusal_message_names_the_fix_and_never_leaks_the_password():
    with pytest.raises(db.RemoteDatabaseRefused) as exc:
        db.require_local_dsn(PROD_DSN, context="The pytest suite")
    msg = str(exc.value)
    assert "REFUSING TO RUN" in msg
    assert "The pytest suite" in msg
    assert "just db-up" in msg
    assert LOCAL_DSN in msg
    assert db.ALLOW_REMOTE_TESTS_ENV in msg
    assert "rds.amazonaws.com" in msg  # says which host it refused
    assert "hunter2" not in msg


def test_escape_hatch_allows_remote_and_announces_itself(monkeypatch, capsys):
    monkeypatch.setenv(db.ALLOW_REMOTE_TESTS_ENV, "1")
    db.require_local_dsn(PROD_DSN, context="The pytest suite")
    err = capsys.readouterr().err
    assert db.ALLOW_REMOTE_TESTS_ENV in err
    assert "REMOTE database" in err
    assert "hunter2" not in err


def test_escape_hatch_needs_exactly_one(monkeypatch):
    monkeypatch.setenv(db.ALLOW_REMOTE_TESTS_ENV, "true")
    with pytest.raises(db.RemoteDatabaseRefused):
        db.require_local_dsn(PROD_DSN)


def _root_conftest():
    """Load the repo-root conftest by path (``conftest`` in sys.modules is a local one)."""
    import importlib.util
    from pathlib import Path

    path = Path(db.__file__).resolve().parents[2] / "conftest.py"
    spec = importlib.util.spec_from_file_location("praxis_root_conftest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conftest_gate_refuses_a_production_dsn(monkeypatch):
    """The repo-root conftest hook itself, not just the helper it calls."""
    monkeypatch.setenv("PRAXIS_DB_URL", PROD_DSN)
    conftest = _root_conftest()  # its load_dotenv() must not override the above

    with pytest.raises(pytest.UsageError, match="REFUSING TO RUN"):
        conftest.pytest_configure(None)

    monkeypatch.setenv("PRAXIS_DB_URL", LOCAL_DSN)
    conftest.pytest_configure(None)  # does not raise

    monkeypatch.setenv("PRAXIS_DB_DISABLED", "1")
    monkeypatch.setenv("PRAXIS_DB_URL", PROD_DSN)
    conftest.pytest_configure(None)  # no DB resolved at all -> nothing to guard
