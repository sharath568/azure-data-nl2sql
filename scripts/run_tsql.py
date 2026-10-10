"""Run a GO-separated T-SQL script with sqlcmd-style $(VAR) substitution.

No sqlcmd needed, just the Python driver nl2sql already uses.

    python scripts/run_tsql.py "Server=...;Database=...;UID=...;PWD=..." \\
        sql/mssql/03_login_sqlserver.sql -v READER_PASSWORD='...'

Use an admin connection for setup scripts. Variables can also come from the
environment as NL2SQL_VAR_<NAME>.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from nl2sql.db.mssql import MSSQLBackend


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("conn", help="ODBC-style connection string (admin for setup scripts)")
    parser.add_argument("scripts", nargs="+", help="one or more .sql files, run in order")
    parser.add_argument("-v", "--var", action="append", default=[], metavar="NAME=VALUE")
    args = parser.parse_args()

    variables = {
        k[len("NL2SQL_VAR_") :]: v for k, v in os.environ.items() if k.startswith("NL2SQL_VAR_")
    }
    for item in args.var:
        name, sep, value = item.partition("=")
        if not sep:
            parser.error(f"bad -v {item!r}; use NAME=VALUE")
        variables[name] = value

    backend = MSSQLBackend(args.conn)
    try:
        for script in args.scripts:
            n = backend.run_script(Path(script).read_text(encoding="utf-8"), variables)
            print(f"{script}: {n} batch(es) ok")
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
