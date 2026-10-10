"""The bundled sample database: a small coffee-and-tea retailer.

One deterministic generator feeds both targets:

* ``build_sqlite(path)`` writes a SQLite file for the offline quickstart and tests.
* ``tsql_schema()`` / ``tsql_seed()`` produce the T-SQL that ``sql/mssql/01_schema.sql``
  and ``sql/mssql/02_seed.sql`` are generated from (``scripts/gen_tsql.py``).

Same rows on both engines, so the gold set can be checked against either one.
Every name, email and SSN here is made up. SSNs use the 900 range, which is
never issued.
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SEED = 2026


@dataclass(frozen=True)
class ColumnDef:
    name: str
    tsql_type: str
    nullable: bool = False
    primary_key: bool = False


@dataclass(frozen=True)
class ForeignKeyDef:
    table: str
    column: str
    ref_table: str
    ref_column: str


def _c(name, tsql, nullable=False, pk=False) -> ColumnDef:
    return ColumnDef(name, tsql, nullable, pk)


TABLES: dict[str, list[ColumnDef]] = {
    "customers": [
        _c("customer_id", "INT", pk=True),
        _c("first_name", "NVARCHAR(50)"),
        _c("last_name", "NVARCHAR(50)"),
        _c("email", "NVARCHAR(120)"),
        _c("city", "NVARCHAR(50)"),
        _c("state", "CHAR(2)"),
        _c("region", "NVARCHAR(20)"),
        _c("signup_date", "DATE"),
        _c("loyalty_tier", "NVARCHAR(10)"),
    ],
    "products": [
        _c("product_id", "INT", pk=True),
        _c("name", "NVARCHAR(80)"),
        _c("category", "NVARCHAR(30)"),
        _c("list_price", "DECIMAL(10,2)"),
        _c("discontinued", "BIT"),
    ],
    "employees": [
        _c("employee_id", "INT", pk=True),
        _c("first_name", "NVARCHAR(50)"),
        _c("last_name", "NVARCHAR(50)"),
        _c("title", "NVARCHAR(60)"),
        _c("department", "NVARCHAR(30)"),
        _c("region", "NVARCHAR(20)", nullable=True),
        _c("hire_date", "DATE"),
        _c("manager_id", "INT", nullable=True),
        _c("salary", "DECIMAL(10,2)"),
        _c("ssn", "CHAR(11)"),
    ],
    "orders": [
        _c("order_id", "INT", pk=True),
        _c("customer_id", "INT"),
        _c("employee_id", "INT"),
        _c("order_date", "DATE"),
        _c("status", "NVARCHAR(12)"),
        _c("shipped_date", "DATE", nullable=True),
    ],
    "order_items": [
        _c("order_item_id", "INT", pk=True),
        _c("order_id", "INT"),
        _c("product_id", "INT"),
        _c("quantity", "INT"),
        _c("unit_price", "DECIMAL(10,2)"),
        _c("discount", "DECIMAL(4,2)"),
    ],
}

FOREIGN_KEYS = [
    ForeignKeyDef("employees", "manager_id", "employees", "employee_id"),
    ForeignKeyDef("orders", "customer_id", "customers", "customer_id"),
    ForeignKeyDef("orders", "employee_id", "employees", "employee_id"),
    ForeignKeyDef("order_items", "order_id", "orders", "order_id"),
    ForeignKeyDef("order_items", "product_id", "products", "product_id"),
]

# Load order respects the foreign keys; drop order is the reverse.
LOAD_ORDER = ["customers", "products", "employees", "orders", "order_items"]

CITIES = [
    ("New York", "NY", "East"),
    ("Boston", "MA", "East"),
    ("Philadelphia", "PA", "East"),
    ("Seattle", "WA", "West"),
    ("San Francisco", "CA", "West"),
    ("Denver", "CO", "West"),
    ("Austin", "TX", "South"),
    ("Atlanta", "GA", "South"),
    ("Miami", "FL", "South"),
    ("Chicago", "IL", "Midwest"),
    ("Minneapolis", "MN", "Midwest"),
    ("Detroit", "MI", "Midwest"),
]

FIRST_NAMES = [
    "Ava",
    "Liam",
    "Maya",
    "Noah",
    "Priya",
    "Ethan",
    "Sofia",
    "Lucas",
    "Aisha",
    "Mateo",
    "Chloe",
    "Omar",
    "Grace",
    "Ravi",
    "Hannah",
    "Diego",
    "Zoe",
    "Kenji",
    "Lena",
    "Samir",
    "Nora",
    "Tariq",
    "Elena",
    "Jonah",
    "Mei",
    "Isaac",
    "Leila",
    "Owen",
    "Anika",
    "Felix",
]
LAST_NAMES = [
    "Garcia",
    "Nguyen",
    "Patel",
    "Smith",
    "Kim",
    "Okafor",
    "Rossi",
    "Silva",
    "Cohen",
    "Haddad",
    "Johnson",
    "Tanaka",
    "Muller",
    "Reddy",
    "Lopez",
    "Brown",
    "Novak",
    "Ali",
    "Moreau",
    "Walsh",
]

PRODUCTS = [
    # name, category, list_price, discontinued
    ("Ethiopia Yirgacheffe 1kg", "Coffee", 34.50, 0),
    ("Colombia Huila 1kg", "Coffee", 29.00, 0),
    ("Sumatra Mandheling 1kg", "Coffee", 31.25, 0),
    ("House Espresso Blend 1kg", "Coffee", 26.00, 0),
    ("Decaf Swiss Water 500g", "Coffee", 17.75, 0),
    ("Kenya AA 500g", "Coffee", 21.50, 0),
    ("Guatemala Antigua 1kg", "Coffee", 28.40, 1),
    ("Darjeeling First Flush 100g", "Tea", 18.00, 0),
    ("Sencha Green 100g", "Tea", 12.50, 0),
    ("Earl Grey 250g", "Tea", 14.00, 0),
    ("Chamomile 100g", "Tea", 9.75, 0),
    ("Oolong Tie Guan Yin 100g", "Tea", 22.00, 0),
    ("Masala Chai 250g", "Tea", 13.25, 0),
    ("Pour-Over Dripper", "Brewing", 24.00, 0),
    ("French Press 1L", "Brewing", 39.00, 0),
    ("Burr Grinder", "Brewing", 149.00, 0),
    ("Gooseneck Kettle", "Brewing", 69.00, 0),
    ("Espresso Machine Compact", "Brewing", 449.00, 0),
    ("Cold Brew Jar", "Brewing", 32.00, 1),
    ("Paper Filters (100)", "Accessories", 6.50, 0),
    ("Ceramic Mug", "Accessories", 15.00, 0),
    ("Travel Tumbler", "Accessories", 27.00, 0),
    ("Digital Scale", "Accessories", 45.00, 0),
    ("Tea Infuser", "Accessories", 8.25, 0),
]
# Never sold, on purpose: gives "which products have never been sold?" an answer.
UNSOLD_PRODUCT_IDS = {7, 19}

EMPLOYEES = [
    # first, last, title, department, region, hire_date, manager index (1-based) or None, salary
    ("Dana", "Whitfield", "VP of Sales", "Sales", None, "2019-03-04", None, 182000),
    ("Marcus", "Bell", "Regional Sales Manager", "Sales", "East", "2020-06-15", 1, 128000),
    ("Ines", "Duarte", "Regional Sales Manager", "Sales", "West", "2020-09-01", 1, 131000),
    ("Kwame", "Asante", "Sales Representative", "Sales", "East", "2021-02-08", 2, 76000),
    ("Lucy", "Hart", "Sales Representative", "Sales", "East", "2022-04-18", 2, 71000),
    ("Rafael", "Ortiz", "Sales Representative", "Sales", "South", "2021-11-29", 2, 74000),
    ("Yuki", "Sato", "Sales Representative", "Sales", "West", "2022-01-10", 3, 78000),
    ("Ben", "Carver", "Sales Representative", "Sales", "West", "2023-05-22", 3, 69000),
    ("Amara", "Eze", "Sales Representative", "Sales", "Midwest", "2023-08-14", 3, 70500),
    ("Tom", "Lindqvist", "Support Lead", "Support", None, "2020-01-13", None, 98000),
    ("Rosa", "Mendes", "Support Specialist", "Support", None, "2024-03-04", 10, 58000),
    ("Gil", "Harper", "Operations Manager", "Operations", None, "2019-10-07", None, 115000),
]
SALES_REP_IDS = [4, 5, 6, 7, 8, 9]
REP_REGION = {4: "East", 5: "East", 6: "South", 7: "West", 8: "West", 9: "Midwest"}

N_CUSTOMERS = 60
N_ORDERS = 400
# Customers that never order (for the "never placed an order" question).
NO_ORDER_CUSTOMERS = {13, 27, 41, 52, 58}


@dataclass
class SampleData:
    rows: dict[str, list[tuple]]

    def count(self, table: str) -> int:
        return len(self.rows[table])


def generate(seed: int = SEED) -> SampleData:
    """Build every row deterministically from ``seed``."""
    rng = random.Random(seed)
    rows: dict[str, list[tuple]] = {t: [] for t in LOAD_ORDER}

    tiers = ["Bronze"] * 6 + ["Silver"] * 3 + ["Gold"]
    used_emails: set[str] = set()
    for cid in range(1, N_CUSTOMERS + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        city, state, region = rng.choice(CITIES)
        email = f"{first}.{last}".lower()
        while email + "@example.com" in used_emails:
            email += str(rng.randint(1, 9))
        email += "@example.com"
        used_emails.add(email)
        signup = dt.date(2023, 1, 1) + dt.timedelta(days=rng.randint(0, 1000))
        rows["customers"].append(
            (cid, first, last, email, city, state, region, signup.isoformat(), rng.choice(tiers))
        )

    for pid, (name, cat, price, disc) in enumerate(PRODUCTS, start=1):
        rows["products"].append((pid, name, cat, price, disc))

    for eid, (first, last, title, dept, region, hired, mgr, salary) in enumerate(
        EMPLOYEES, start=1
    ):
        ssn = f"9{eid:02d}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"
        rows["employees"].append(
            (eid, first, last, title, dept, region, hired, mgr, float(salary), ssn)
        )

    ordering_customers = [c for c in range(1, N_CUSTOMERS + 1) if c not in NO_ORDER_CUSTOMERS]
    # Make some customers much busier than others, like real life.
    weights = [rng.choice([1, 1, 1, 2, 2, 3, 5]) for _ in ordering_customers]
    sellable = [p for p in range(1, len(PRODUCTS) + 1) if p not in UNSOLD_PRODUCT_IDS]
    product_weights = [
        6 if PRODUCTS[p - 1][1] in ("Coffee", "Tea") else (3 if PRODUCTS[p - 1][2] < 50 else 1)
        for p in sellable
    ]
    region_of = {c[0]: c[6] for c in rows["customers"]}

    item_id = 0
    for oid in range(1, N_ORDERS + 1):
        cid = rng.choices(ordering_customers, weights)[0]
        region = region_of[cid]
        reps = [r for r in SALES_REP_IDS if REP_REGION[r] == region] or SALES_REP_IDS
        rep = rng.choice(reps)
        order_date = dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 364))
        roll = rng.random()
        if roll < 0.08:
            status, shipped = "cancelled", None
        elif order_date >= dt.date(2025, 12, 15) or roll < 0.14:
            status, shipped = "pending", None
        else:
            status = "shipped"
            shipped = (order_date + dt.timedelta(days=rng.randint(1, 9))).isoformat()
        rows["orders"].append((oid, cid, rep, order_date.isoformat(), status, shipped))

        n_items = rng.choice([1, 1, 2, 2, 2, 3, 3, 4])
        for pid in sorted(set(rng.choices(sellable, product_weights, k=n_items))):
            item_id += 1
            price = PRODUCTS[pid - 1][2]
            qty = rng.choice([1, 1, 1, 2, 2, 3, 4, 6])
            discount = rng.choice([0.0, 0.0, 0.0, 0.05, 0.10])
            rows["order_items"].append((item_id, oid, pid, qty, price, discount))

    return SampleData(rows)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def sqlite_ddl() -> list[str]:
    """SQLite DDL using the same declared types as SQL Server, so the schema card
    reads the same on both. SQLite maps them to its storage classes by affinity."""
    stmts = []
    for table in LOAD_ORDER:
        cols = []
        for c in TABLES[table]:
            line = f"{c.name} {c.tsql_type}"
            if c.primary_key:
                line += " PRIMARY KEY"
            elif not c.nullable:
                line += " NOT NULL"
            cols.append(line)
        for fk in FOREIGN_KEYS:
            if fk.table == table:
                cols.append(f"FOREIGN KEY ({fk.column}) REFERENCES {fk.ref_table}({fk.ref_column})")
        stmts.append(f"CREATE TABLE {table} (\n  " + ",\n  ".join(cols) + "\n)")
    return stmts


def build_sqlite(path: str | Path, *, force: bool = False, seed: int = SEED) -> SampleData:
    """Create the sample SQLite database at ``path``."""
    path = Path(path)
    if path.exists():
        if not force:
            raise FileExistsError(f"{path} already exists (use force=True to overwrite)")
        path.unlink()
    data = generate(seed)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for stmt in sqlite_ddl():
            conn.execute(stmt)
        for table in LOAD_ORDER:
            cols = TABLES[table]
            marks = ", ".join("?" for _ in cols)
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", data.rows[table])
        conn.commit()
    finally:
        conn.close()
    return data


# ---------------------------------------------------------------------------
# T-SQL
# ---------------------------------------------------------------------------

_HEADER = """\
-- {title}
-- Generated by scripts/gen_tsql.py from src/nl2sql/sample.py. Do not edit by hand.
-- Works on SQL Server 2019+ and Azure SQL Database. Batches are separated by GO.
"""


def tsql_schema() -> str:
    out = [_HEADER.format(title="nl2sql sample schema (retail)")]
    out.append("SET NOCOUNT ON;\nGO\n")
    for table in reversed(LOAD_ORDER):
        out.append(f"DROP TABLE IF EXISTS dbo.{table};\n")
    out.append("GO\n")
    for table in LOAD_ORDER:
        lines = []
        for c in TABLES[table]:
            null = "NULL" if c.nullable else "NOT NULL"
            lines.append(f"    {c.name} {c.tsql_type} {null}")
        pk = next(c.name for c in TABLES[table] if c.primary_key)
        lines.append(f"    CONSTRAINT pk_{table} PRIMARY KEY ({pk})")
        for fk in FOREIGN_KEYS:
            if fk.table == table:
                lines.append(
                    f"    CONSTRAINT fk_{table}_{fk.column} FOREIGN KEY ({fk.column}) "
                    f"REFERENCES dbo.{fk.ref_table} ({fk.ref_column})"
                )
        out.append(f"\nCREATE TABLE dbo.{table} (\n" + ",\n".join(lines) + "\n);\n")
    out.append("GO\n")
    out.append(
        "\nCREATE INDEX ix_orders_customer ON dbo.orders (customer_id);\n"
        "CREATE INDEX ix_orders_date ON dbo.orders (order_date);\n"
        "CREATE INDEX ix_order_items_order ON dbo.order_items (order_id);\n"
        "CREATE INDEX ix_order_items_product ON dbo.order_items (product_id);\n"
        "GO\n"
    )
    return "".join(out)


def _tsql_literal(value, col: ColumnDef) -> str:
    if value is None:
        return "NULL"
    if col.tsql_type.startswith(("NVARCHAR", "NCHAR")):
        return "N'" + str(value).replace("'", "''") + "'"
    if col.tsql_type.startswith(("CHAR", "VARCHAR", "DATE")):
        return "'" + str(value).replace("'", "''") + "'"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def tsql_seed(seed: int = SEED, batch: int = 200) -> str:
    data = generate(seed)
    out = [_HEADER.format(title="nl2sql sample data (retail)")]
    out.append("SET NOCOUNT ON;\nGO\n")
    for table in LOAD_ORDER:
        cols = TABLES[table]
        names = ", ".join(c.name for c in cols)
        rows = data.rows[table]
        # A self-referencing table (employees.manager_id) goes in one row per
        # statement, managers first, so no statement depends on its own rows.
        self_ref = any(fk.table == table and fk.ref_table == table for fk in FOREIGN_KEYS)
        step = 1 if self_ref else batch
        for start in range(0, len(rows), step):
            chunk = rows[start : start + step]
            values = ",\n".join(
                "    (" + ", ".join(_tsql_literal(v, c) for v, c in zip(r, cols, strict=True)) + ")"
                for r in chunk
            )
            out.append(f"\nINSERT INTO dbo.{table} ({names}) VALUES\n{values};\n")
        out.append("GO\n")
    return "".join(out)
