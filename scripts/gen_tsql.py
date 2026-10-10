"""Regenerate sql/mssql/01_schema.sql and 02_seed.sql from src/nl2sql/sample.py.

python scripts/gen_tsql.py           # write the files
python scripts/gen_tsql.py --check   # exit 1 if they are out of date (CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nl2sql import sample

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    ROOT / "sql" / "mssql" / "01_schema.sql": sample.tsql_schema,
    ROOT / "sql" / "mssql" / "02_seed.sql": sample.tsql_seed,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if files are stale")
    args = parser.parse_args()
    stale = []
    for path, make in TARGETS.items():
        text = make()
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT)} (run python scripts/gen_tsql.py)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
