"""Wait until SQL Server accepts connections, then optionally create a database.

    python scripts/wait_for_mssql.py "Server=localhost,1433;UID=sa;PWD=...;..." \\
        --create-db nl2sql_sample --timeout 180

Used in CI, where the SQL Server container takes a while to start.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from nl2sql.db.mssql import connect


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("conn", help="connection string to the server (master)")
    parser.add_argument("--create-db", default=None)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    if args.create_db and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", args.create_db):
        parser.error("database name must be a plain identifier")

    deadline = time.monotonic() + args.timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn, driver = connect(args.conn, autocommit=True)
        except Exception as e:
            last = e
            time.sleep(3)
            continue
        cur = conn.cursor()
        cur.execute("SELECT @@VERSION")
        print(f"connected via {driver}: {str(cur.fetchone()[0]).splitlines()[0]}")
        if args.create_db:
            cur.execute(f"IF DB_ID(N'{args.create_db}') IS NULL CREATE DATABASE [{args.create_db}]")
            print(f"database {args.create_db} ready")
        conn.close()
        return 0
    print(f"SQL Server not reachable after {args.timeout}s: {last}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
