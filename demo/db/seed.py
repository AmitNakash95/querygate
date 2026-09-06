#!/usr/bin/env python3
"""Seed the QueryGate partner-demo pitch database (querygate_demo_pitch).

ALL PII IN THIS SCRIPT IS OBVIOUSLY, DELIBERATELY SYNTHETIC. This data is
shown live to business partners on a projector during a sales demo, so a
screenshot of it must never be mistakable for a real data leak:

  - SSNs / national IDs use the reserved-invalid Social Security Administration
    area-number range 900-999 (format 9XX-XX-XXXX), which the SSA has never
    issued and never will.
  - Emails use the `@example.invalid` domain, which RFC 2606 reserves
    specifically so it can never resolve to a real mailbox.
  - Phone numbers use the North American Numbering Plan's reserved fictional
    "555-01XX" block (format AAA-555-01NN), the same range used in films/TV.
  - Employee names, departments, managers, and salaries are drawn from small
    fixed rosters of placeholder demo values — plausible-looking but not
    modeled on, or matching, any real person.

Run with the repo's own virtualenv (has asyncpg):

    .venv/bin/python demo/db/seed.py

Deterministic: the RNG is seeded with a fixed constant (see SEED below), so
every run produces byte-identical data and any example values quoted in a
runbook stay true across reseeds. Idempotent: truncates and reseeds every
table on each run, safe to re-run any number of times. Uses COPY (via
asyncpg's binary copy_records_to_table), not row-by-row INSERTs, to seed
~1.85M rows in well under two minutes on a laptop.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

# demo/ is deliberately NOT a Python package (no top-level __init__.py), so
# the shared DSN-validation module (demo/_dsn_guard.py) can't be reached via
# a normal package-relative import from this file, which lives one
# directory below it (demo/db/). Insert demo/ itself onto sys.path rather
# than adding an __init__.py anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _dsn_guard import validate_dsn  # noqa: E402

# Fixed seed -> identical data on every run, so runbook example values
# ("employee id 4 is ...", "customer 118203 lives in ...") stay true.
SEED = 20260823

DSN_ENV_VAR = "QUERYGATE_PITCH_DB_DSN"
DEFAULT_DSN = (
    "postgresql://pitch_owner:pitch_owner_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch"
)
DSN = os.environ.get(DSN_ENV_VAR, DEFAULT_DSN)

# This script TRUNCATEs five tables outright (see truncate_all below) and is
# reached both directly (`make pitch-db-seed`) and automatically by
# `make pitch-up` whenever the row count looks low. A stale or mistyped
# QUERYGATE_PITCH_DB_DSN in a presenter's shell must not be able to make
# this script destroy data in whatever database that env var happens to
# name — remote databases included. Validated the same way, and with the
# same shared implementation, as demo/baseline_mcp/server.py's startup
# guard (demo/_dsn_guard.py) — see that module's docstring for the full
# rationale, including why the required role here (pitch_owner, the
# table-owning role that legitimately needs TRUNCATE/COPY privileges) is
# deliberately different from the MCP servers' read-only agent_ro.
REQUIRED_DB_USER = "pitch_owner"

N_CUSTOMERS = 250_000
N_ORDERS = 600_000
N_ORDER_ITEMS = 1_200_000
N_PRODUCTS = 500
N_EMPLOYEES = 12

BATCH = 50_000  # rows per COPY batch, keeps peak memory bounded

# ---------------------------------------------------------------------------
# Fixed rosters for plausible-but-synthetic data.
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Alex",
    "Jordan",
    "Taylor",
    "Morgan",
    "Casey",
    "Riley",
    "Jamie",
    "Avery",
    "Quinn",
    "Drew",
    "Sam",
    "Reese",
    "Cameron",
    "Rowan",
    "Skyler",
    "Peyton",
    "Dakota",
    "Emerson",
    "Finley",
    "Harper",
    "Kendall",
    "Logan",
    "Parker",
    "Sage",
    "Blake",
    "Charlie",
    "Devon",
    "Elliot",
    "Frankie",
    "Gray",
]
LAST_NAMES = [
    "Whitfield",
    "Alderton",
    "Marsh",
    "Kestrel",
    "Novak",
    "Renner",
    "Osei",
    "Bramwell",
    "Castellan",
    "Dunmore",
    "Ekwueme",
    "Farrow",
    "Gable",
    "Hallow",
    "Ionescu",
    "Jarrow",
    "Kowalczyk",
    "Larkspur",
    "Meakin",
    "Norrell",
    "Oduya",
    "Pemberton",
    "Quimby",
    "Rathbone",
    "Sylvane",
    "Thistlewood",
    "Underhay",
    "Vantree",
    "Woodrow",
    "Yarrow",
]
COUNTRIES = [
    "United States",
    "Canada",
    "United Kingdom",
    "Germany",
    "France",
    "Ireland",
    "Netherlands",
    "Sweden",
    "Australia",
    "Japan",
    "Brazil",
    "Mexico",
    "Spain",
    "Italy",
    "Poland",
]
ORDER_STATUSES = ["pending", "processing", "completed", "cancelled", "refunded"]
ORDER_STATUS_WEIGHTS = [10, 15, 60, 10, 5]

PRODUCT_CATEGORIES = [
    "Electronics",
    "Home & Kitchen",
    "Sporting Goods",
    "Office Supplies",
    "Toys & Games",
    "Garden",
    "Automotive",
    "Books",
    "Apparel",
    "Pet Supplies",
]
PRODUCT_ADJECTIVES = [
    "Compact",
    "Deluxe",
    "Portable",
    "Wireless",
    "Heavy-Duty",
    "Eco",
    "Premium",
    "Classic",
    "Modular",
    "All-Weather",
    "Ergonomic",
    "Rapid",
]
PRODUCT_NOUNS = [
    "Blender",
    "Desk Lamp",
    "Backpack",
    "Tool Kit",
    "Water Bottle",
    "Charger",
    "Notebook",
    "Chair",
    "Speaker",
    "Trimmer",
    "Cooler",
    "Router",
    "Grill",
    "Tent",
    "Monitor Stand",
    "Kettle",
    "Doormat",
    "Planter",
    "Leash",
    "Bin",
]

EMPLOYEE_ROSTER = [
    # (name, department, salary, manager)
    ("Priya Ashworth", "Executive", 245000, None),
    ("Marcus Delacroix", "Engineering", 190000, "Priya Ashworth"),
    ("Nadia Thorsen", "Sales", 165000, "Priya Ashworth"),
    ("Owen Falkirk", "Finance", 172000, "Priya Ashworth"),
    ("Yuki Hattersley", "Engineering", 158000, "Marcus Delacroix"),
    ("Leila Sorensen", "Engineering", 149000, "Marcus Delacroix"),
    ("Grant Pemberly", "Sales", 121000, "Nadia Thorsen"),
    ("Sofia Reindeer", "Sales", 118000, "Nadia Thorsen"),
    ("Tobias Wrenfield", "Finance", 132000, "Owen Falkirk"),
    ("Amara Okonkwo-Lyle", "HR", 108000, "Priya Ashworth"),
    ("Declan Ashcombe", "Support", 87000, "Priya Ashworth"),
    ("Ines Marchetti", "Product", 142000, "Priya Ashworth"),
]

EMAIL_DOMAIN = "example.invalid"


def make_ssn(rng: random.Random) -> str:
    """SSN-shaped string in the SSA's reserved-invalid 900-999 area range."""
    area = rng.randint(900, 999)
    group = rng.randint(1, 99)
    serial = rng.randint(1, 9999)
    return f"{area:03d}-{group:02d}-{serial:04d}"


def make_phone(rng: random.Random) -> str:
    """NANP-shaped phone number in the reserved fictional 555-01XX block."""
    area_code = rng.randint(200, 999)
    line = rng.randint(0, 99)
    return f"{area_code:03d}-555-01{line:02d}"


def make_email(first: str, last: str, unique: int) -> str:
    local = f"{first}.{last}.{unique}".lower().replace(" ", "")
    return f"{local}@{EMAIL_DOMAIN}"


def random_dt(rng: random.Random, start: datetime, end: datetime) -> datetime:
    delta = end - start
    seconds = rng.uniform(0, delta.total_seconds())
    return start + timedelta(seconds=seconds)


async def truncate_all(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "TRUNCATE TABLE order_items, orders, products, customers, employees "
        "RESTART IDENTITY CASCADE"
    )


async def seed_products(conn: asyncpg.Connection, rng: random.Random) -> int:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1500)
    records = []
    for _ in range(N_PRODUCTS):
        name = f"{rng.choice(PRODUCT_ADJECTIVES)} {rng.choice(PRODUCT_NOUNS)}"
        category = rng.choice(PRODUCT_CATEGORIES)
        price = round(rng.uniform(4.99, 899.99), 2)
        in_stock = rng.random() > 0.08
        created_at = random_dt(rng, start, now)
        records.append((name, category, price, in_stock, created_at))
    await conn.copy_records_to_table(
        "products",
        records=records,
        columns=["name", "category", "price", "in_stock", "created_at"],
    )
    return len(records)


async def seed_customers(conn: asyncpg.Connection, rng: random.Random) -> int:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=2000)
    total = 0
    batch: list[tuple] = []
    for i in range(N_CUSTOMERS):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        email = make_email(first, last, i)
        phone = make_phone(rng)
        national_id = make_ssn(rng)
        country = rng.choice(COUNTRIES)
        is_active = rng.random() > 0.12
        created_at = random_dt(rng, start, now)
        batch.append((name, email, phone, national_id, country, is_active, created_at))
        if len(batch) >= BATCH:
            await conn.copy_records_to_table(
                "customers",
                records=batch,
                columns=[
                    "name",
                    "email",
                    "phone",
                    "national_id",
                    "country",
                    "is_active",
                    "created_at",
                ],
            )
            total += len(batch)
            batch = []
    if batch:
        await conn.copy_records_to_table(
            "customers",
            records=batch,
            columns=[
                "name",
                "email",
                "phone",
                "national_id",
                "country",
                "is_active",
                "created_at",
            ],
        )
        total += len(batch)
    return total


async def seed_employees(conn: asyncpg.Connection, rng: random.Random) -> int:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=2500)
    records = []
    for i, (name, department, salary, manager) in enumerate(EMPLOYEE_ROSTER):
        first, last = name.split(" ", 1)
        email = make_email(first, last.replace(" ", "-"), i)
        ssn = make_ssn(rng)
        hired_at = random_dt(rng, start, now)
        records.append((name, email, ssn, department, salary, manager, hired_at))
    await conn.copy_records_to_table(
        "employees",
        records=records,
        columns=[
            "name",
            "email",
            "ssn",
            "department",
            "salary",
            "manager",
            "hired_at",
        ],
    )
    return len(records)


async def seed_orders(conn: asyncpg.Connection, rng: random.Random) -> int:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1800)
    total = 0
    batch: list[tuple] = []
    for _ in range(N_ORDERS):
        customer_id = rng.randint(1, N_CUSTOMERS)
        status = rng.choices(ORDER_STATUSES, weights=ORDER_STATUS_WEIGHTS)[0]
        total_amount = round(rng.uniform(9.99, 2499.99), 2)
        created_at = random_dt(rng, start, now)
        batch.append((customer_id, status, total_amount, created_at))
        if len(batch) >= BATCH:
            await conn.copy_records_to_table(
                "orders",
                records=batch,
                columns=["customer_id", "status", "total_amount", "created_at"],
            )
            total += len(batch)
            batch = []
    if batch:
        await conn.copy_records_to_table(
            "orders",
            records=batch,
            columns=["customer_id", "status", "total_amount", "created_at"],
        )
        total += len(batch)
    return total


async def seed_order_items(conn: asyncpg.Connection, rng: random.Random) -> int:
    total = 0
    batch: list[tuple] = []
    remaining = N_ORDER_ITEMS
    # Spread items across all N_ORDERS orders (average 2 items/order), not
    # strictly 1:1, so some orders have 1 item and some have several -
    # realistic and still deterministic under the fixed seed.
    for order_id in range(1, N_ORDERS + 1):
        if remaining <= 0:
            break
        # Most orders get 1-3 items; last order absorbs any remainder so the
        # total exactly matches N_ORDER_ITEMS.
        if order_id == N_ORDERS:
            n_items = remaining
        else:
            n_items = min(remaining, rng.choices([1, 2, 3, 4], weights=[35, 35, 20, 10])[0])
        for _ in range(n_items):
            product_name = f"{rng.choice(PRODUCT_ADJECTIVES)} {rng.choice(PRODUCT_NOUNS)}"
            quantity = rng.randint(1, 6)
            unit_price = round(rng.uniform(4.99, 899.99), 2)
            batch.append((order_id, product_name, quantity, unit_price))
            remaining -= 1
        if len(batch) >= BATCH:
            await conn.copy_records_to_table(
                "order_items",
                records=batch,
                columns=["order_id", "product_name", "quantity", "unit_price"],
            )
            total += len(batch)
            batch = []
    if batch:
        await conn.copy_records_to_table(
            "order_items",
            records=batch,
            columns=["order_id", "product_name", "quantity", "unit_price"],
        )
        total += len(batch)
    return total


async def main() -> None:
    t0 = time.monotonic()

    # S2 hard safety constraint: refuse to run at all against a DSN that
    # isn't unambiguously the local throwaway pitch-demo database, as
    # pitch_owner, on port 5544 — this is the ONLY thing standing between a
    # stale/mistyped QUERYGATE_PITCH_DB_DSN and truncate_all() destroying
    # five tables in whatever database that env var actually names.
    reason = validate_dsn(DSN, required_user=REQUIRED_DB_USER, dsn_env_var=DSN_ENV_VAR)
    if reason is not None:
        print(f"seed.py {reason}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Connecting to {DSN.split('@')[-1]} ...")
    conn = await asyncpg.connect(DSN)
    try:
        print("Truncating existing rows (idempotent reseed) ...")
        await truncate_all(conn)

        rng = random.Random(SEED)

        print(f"Seeding products (~{N_PRODUCTS:,}) ...")
        n_products = await seed_products(conn, rng)
        print(f"  -> {n_products:,} rows")

        print(f"Seeding customers (~{N_CUSTOMERS:,}) ...")
        n_customers = await seed_customers(conn, rng)
        print(f"  -> {n_customers:,} rows")

        print(f"Seeding employees ({N_EMPLOYEES}) ...")
        n_employees = await seed_employees(conn, rng)
        print(f"  -> {n_employees:,} rows")

        print(f"Seeding orders (~{N_ORDERS:,}) ...")
        n_orders = await seed_orders(conn, rng)
        print(f"  -> {n_orders:,} rows")

        print(f"Seeding order_items (~{N_ORDER_ITEMS:,}) ...")
        n_order_items = await seed_order_items(conn, rng)
        print(f"  -> {n_order_items:,} rows")

        # Refresh planner statistics so EXPLAIN estimates in the demo (the
        # 'overload' scenario) are realistic rather than default guesses.
        print("Running ANALYZE ...")
        await conn.execute("ANALYZE")

    finally:
        await conn.close()

    elapsed = time.monotonic() - t0
    print()
    print("=" * 60)
    print("Seed complete.")
    print(f"  customers:    {n_customers:>10,}")
    print(f"  orders:       {n_orders:>10,}")
    print(f"  order_items:  {n_order_items:>10,}")
    print(f"  products:     {n_products:>10,}")
    print(f"  employees:    {n_employees:>10,}")
    print(f"  elapsed:      {elapsed:>10.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - top-level CLI entry point
        print(f"seed.py failed: {exc}", file=sys.stderr)
        sys.exit(1)
