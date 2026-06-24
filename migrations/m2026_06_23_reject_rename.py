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


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    with connect() as conn:
        _rename_state(conn, "facts")
        _rename_state(conn, "cached_facts")
    print("migration complete.")


if __name__ == "__main__":
    main()
