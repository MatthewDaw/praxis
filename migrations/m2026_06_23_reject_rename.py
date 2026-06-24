"""One-off, idempotent rename of the fact retirement state ``decayed`` -> ``rejected``.

Run once against the local Postgres (5433):

    .venv/Scripts/python.exe -m migrations.m2026_06_23_reject_rename

The fact lifecycle state was renamed in specs/003-fact-rejection-lifecycle; the
``facts.state`` / ``cached_facts.state`` columns are bare ``text`` (no enum/CHECK),
so the rename is a pure data update. Idempotent: a second run matches no rows.
Safe to re-run.
"""

from __future__ import annotations

from knowledge.serve.db import connect


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)
    ).fetchone()
    return bool(row and row[0])


def _rename_state(conn, table: str) -> int:
    """Set state='rejected' on every row currently state='decayed' in ``table``."""
    if not _table_exists(conn, table):
        print(f"{table}: table absent — renamed 0")
        return 0
    result = conn.execute(
        f"UPDATE {table} SET state = 'rejected' WHERE state = 'decayed'"
    )
    print(f"{table}: renamed {result.rowcount} row(s) decayed -> rejected")
    return result.rowcount


def _apply(conn) -> None:
    """yoyo apply step. ``conn`` is the psycopg3 backend connection.

    The pure ``_rename_state`` helper works on either the project's connection
    wrapper or a raw psycopg3 connection (both expose ``execute().rowcount`` /
    ``execute().fetchone()``), so the same code path serves yoyo and the CLI.
    """
    _rename_state(conn, "facts")
    _rename_state(conn, "cached_facts")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    with connect() as conn:
        _apply(conn)
    print("migration complete.")


# yoyo's loader execs this file with a "current migration" context bound, so the
# module-level ``step()`` call registers ``_apply`` as this migration's step.
# Importing the module any other way (e.g. a unit test pulling in
# ``_rename_state``) has no such context — ``step()`` then raises, which we
# swallow: the step list is only needed when yoyo actually applies the file.
try:
    from yoyo import step

    steps = [step(_apply)]
except (ImportError, AttributeError):  # pragma: no cover - no yoyo migration context
    pass


if __name__ == "__main__":
    main()
