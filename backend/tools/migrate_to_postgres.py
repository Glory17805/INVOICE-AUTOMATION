"""Copy an existing SQLite store.db into PostgreSQL.

Run once, when moving a working installation to the deployment. It reads the
SQLite file directly and writes through the application's own schema, so the
destination is created exactly as the app expects rather than by a hand-written
DDL script that can drift from db.py.

    python -m tools.migrate_to_postgres \\
        --sqlite backend/data/store.db \\
        --postgres "postgresql://user:pass@host:5432/gst?sslmode=require"

It refuses to write into a database that already holds rows, because the
plausible mistake here is running it twice and silently doubling the audit
trail. Pass --replace to empty the destination first, which is the only
supported way to re-run it.

Nothing is written until every table has been read and counted, and the whole
copy is one transaction: a failure halfway leaves the destination as it was
rather than half-populated.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# The order matters only for readability - there are no foreign keys between
# these - but it keeps the printed summary in the same order as db.SCHEMA.
TABLES = [
    "documents",
    "users",
    "sessions",
    "password_resets",
    "settings",
    "login_failures",
    "activity",
]

# Serial columns are omitted from the copy so Postgres assigns its own, and the
# identity sequence stays consistent with the rows that exist.
SKIP_COLUMNS = {"login_failures": {"id"}, "activity": {"id"}}


def read_sqlite(path: Path) -> dict[str, list[dict]]:
    if not path.exists():
        sys.exit(f"No SQLite database at {path}")

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        present = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        data = {}
        for table in TABLES:
            if table not in present:
                print(f"  {table:16} - not in source, skipped")
                data[table] = []
                continue
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for row in rows:
                for column in SKIP_COLUMNS.get(table, set()):
                    row.pop(column, None)
            data[table] = rows
            print(f"  {table:16} {len(rows):>6} rows")
        return data
    finally:
        connection.close()


def write_postgres(data: dict[str, list[dict]], *, replace: bool) -> None:
    # Imported here so the app's own schema is the one applied, and so this
    # script fails loudly if DATABASE_URL was not picked up.
    from app import db

    if db.dialect() != "postgres":
        sys.exit("DATABASE_URL is not set to a PostgreSQL URL; refusing to run.")

    with db.connect() as connection:
        existing = {
            table: connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in TABLES
        }
        occupied = {table: n for table, n in existing.items() if n}

        if occupied and not replace:
            listing = ", ".join(f"{table}={n}" for table, n in occupied.items())
            sys.exit(
                f"Destination already holds rows ({listing}).\n"
                "Re-running would duplicate them. Pass --replace to empty it first."
            )

        if occupied:
            print("\nEmptying destination:")
            for table in reversed(TABLES):
                connection.execute(f"DELETE FROM {table}")
                print(f"  {table:16} cleared")

        print("\nWriting:")
        for table in TABLES:
            rows = data[table]
            if not rows:
                continue
            columns = list(rows[0])
            statement = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})"
            )
            for row in rows:
                connection.execute(statement, row)
            print(f"  {table:16} {len(rows):>6} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", type=Path, default=Path("backend/data/store.db"))
    parser.add_argument("--postgres", default=os.environ.get("DATABASE_URL", ""),
                        help="Destination URL. Defaults to DATABASE_URL.")
    parser.add_argument("--replace", action="store_true",
                        help="Empty the destination first. The only supported way to re-run.")
    args = parser.parse_args()

    if not args.postgres:
        sys.exit("No destination: pass --postgres or set DATABASE_URL.")
    os.environ["DATABASE_URL"] = args.postgres

    print(f"Reading {args.sqlite}:")
    data = read_sqlite(args.sqlite)

    write_postgres(data, replace=args.replace)

    total = sum(len(rows) for rows in data.values())
    print(f"\nDone. {total} rows copied.")
    print("Sign in and confirm the queue and the user list look right before "
          "pointing anyone at the deployment.")


if __name__ == "__main__":
    main()
