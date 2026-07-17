"""Demo/seed schema shared by the standalone seed script, docker-compose's
Postgres init script (`init_postgres.sql` — kept in sync by hand), and the
test suite's regression tests.

Five deliberately generic, obviously-synthetic tables — not tied to any
specific customer's domain — chosen to exercise the things QueryGate's core
guarantees actually depend on:

- ``customers`` / ``orders`` / ``order_items`` — a small join graph (one
  foreign key hop each) with enough rows and enough date spread (mid-2024
  through mid-2025) to meaningfully test joins, group_by/aggregates, top_n,
  and date_bucket at every granularity (day/week/month/quarter/year).
- ``products`` — an independent catalog table (no FK to the order graph) for
  testing queries against an unrelated table, and numeric/boolean columns.
- ``employees`` — contains obviously-fake PII-shaped columns (ssn, salary)
  specifically so policy examples/tests have a realistic "this table/these
  columns should be walled off from agents by default" scenario to point at.
  Every value here is synthetic (see the .internal email domain and the
  placeholder SSNs) — do not mistake this for real personal data.

The first 4 customers / 5 orders / 6 order_items rows (ids 1-4 / 1-5 / 1-6)
are the original seed set and are intentionally never modified — several
tests assert exact values against them. Everything else was added
additively; if you extend this file further, prefer appending over editing
existing rows so those tests keep working.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = sa.MetaData()

customers = sa.Table(
    "customers",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("country", sa.String(2), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

orders = sa.Table(
    "orders",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("total_amount", sa.Numeric(10, 2), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

order_items = sa.Table(
    "order_items",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
    sa.Column("product_name", sa.String(100), nullable=False),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
)

products = sa.Table(
    "products",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("category", sa.String(50), nullable=False),
    sa.Column("price", sa.Numeric(10, 2), nullable=False),
    sa.Column("in_stock", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

employees = sa.Table(
    "employees",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(100), nullable=False),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("ssn", sa.String(20), nullable=False),
    sa.Column("department", sa.String(50), nullable=False),
    sa.Column("salary", sa.Numeric(10, 2), nullable=False),
    sa.Column("hired_at", sa.DateTime, nullable=False),
)

# --- customers -------------------------------------------------------------
# ids 1-4 are the original seed set; never modify their values (see
# module docstring). ids 5-8 extend the date/country spread for later tests.
CUSTOMERS_DATA = [
    {
        "id": 1,
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "country": "GB",
        "created_at": dt.datetime(2025, 1, 5),
    },
    {
        "id": 2,
        "name": "Grace Hopper",
        "email": "grace@example.com",
        "country": "US",
        "created_at": dt.datetime(2025, 1, 12),
    },
    {
        "id": 3,
        "name": "Alan Turing",
        "email": "alan@example.com",
        "country": "GB",
        "created_at": dt.datetime(2025, 2, 2),
    },
    {
        "id": 4,
        "name": "Katherine Johnson",
        "email": "katherine@example.com",
        "country": "US",
        "created_at": dt.datetime(2025, 2, 20),
    },
    {
        "id": 5,
        "name": "Marie Curie",
        "email": "marie@example.com",
        "country": "FR",
        "created_at": dt.datetime(2024, 11, 3),
    },
    {
        "id": 6,
        "name": "Charles Babbage",
        "email": "charles@example.com",
        "country": "GB",
        "created_at": dt.datetime(2024, 12, 1),
    },
    {
        "id": 7,
        "name": "Hedy Lamarr",
        "email": "hedy@example.com",
        "country": "US",
        "created_at": dt.datetime(2025, 1, 20),
    },
    {
        "id": 8,
        "name": "Radia Perlman",
        "email": "radia@example.com",
        "country": "US",
        "created_at": dt.datetime(2025, 4, 2),
    },
]

# --- orders ------------------------------------------------------------
# ids 1-5 are the original seed set (3 "completed", spanning Jan-Mar 2025;
# Ada Lovelace/customer 1 has exactly 2 orders) — never modify their values.
# ids 6-20 extend coverage back to mid-2024 and forward to mid-2025, across
# every status value, so date_bucket has enough spread to test month/
# quarter/year granularity and group_by/top_n have enough per-customer
# variance to be meaningful.
ORDERS_DATA = [
    {
        "id": 1,
        "customer_id": 1,
        "status": "completed",
        "total_amount": 129.99,
        "created_at": dt.datetime(2025, 1, 10),
    },
    {
        "id": 2,
        "customer_id": 1,
        "status": "completed",
        "total_amount": 59.50,
        "created_at": dt.datetime(2025, 3, 1),
    },
    {
        "id": 3,
        "customer_id": 2,
        "status": "pending",
        "total_amount": 249.00,
        "created_at": dt.datetime(2025, 2, 15),
    },
    {
        "id": 4,
        "customer_id": 3,
        "status": "completed",
        "total_amount": 15.00,
        "created_at": dt.datetime(2025, 2, 5),
    },
    {
        "id": 5,
        "customer_id": 4,
        "status": "cancelled",
        "total_amount": 89.90,
        "created_at": dt.datetime(2025, 3, 10),
    },
    {
        "id": 6,
        "customer_id": 2,
        "status": "completed",
        "total_amount": 75.00,
        "created_at": dt.datetime(2024, 11, 15),
    },
    {
        "id": 7,
        "customer_id": 2,
        "status": "completed",
        "total_amount": 310.25,
        "created_at": dt.datetime(2025, 5, 2),
    },
    {
        "id": 8,
        "customer_id": 3,
        "status": "completed",
        "total_amount": 42.00,
        "created_at": dt.datetime(2024, 9, 10),
    },
    {
        "id": 9,
        "customer_id": 3,
        "status": "cancelled",
        "total_amount": 18.50,
        "created_at": dt.datetime(2025, 1, 5),
    },
    {
        "id": 10,
        "customer_id": 4,
        "status": "completed",
        "total_amount": 120.00,
        "created_at": dt.datetime(2024, 8, 22),
    },
    {
        "id": 11,
        "customer_id": 4,
        "status": "completed",
        "total_amount": 95.75,
        "created_at": dt.datetime(2025, 6, 1),
    },
    {
        "id": 12,
        "customer_id": 5,
        "status": "completed",
        "total_amount": 220.00,
        "created_at": dt.datetime(2024, 11, 10),
    },
    {
        "id": 13,
        "customer_id": 5,
        "status": "pending",
        "total_amount": 60.00,
        "created_at": dt.datetime(2025, 2, 14),
    },
    {
        "id": 14,
        "customer_id": 6,
        "status": "completed",
        "total_amount": 15.99,
        "created_at": dt.datetime(2024, 12, 5),
    },
    {
        "id": 15,
        "customer_id": 6,
        "status": "completed",
        "total_amount": 340.00,
        "created_at": dt.datetime(2025, 3, 20),
    },
    {
        "id": 16,
        "customer_id": 7,
        "status": "refunded",
        "total_amount": 88.00,
        "created_at": dt.datetime(2025, 1, 25),
    },
    {
        "id": 17,
        "customer_id": 7,
        "status": "completed",
        "total_amount": 199.99,
        "created_at": dt.datetime(2025, 4, 18),
    },
    {
        "id": 18,
        "customer_id": 8,
        "status": "completed",
        "total_amount": 55.50,
        "created_at": dt.datetime(2025, 4, 10),
    },
    {
        "id": 19,
        "customer_id": 8,
        "status": "completed",
        "total_amount": 410.00,
        "created_at": dt.datetime(2025, 5, 30),
    },
    {
        "id": 20,
        "customer_id": 1,
        "status": "completed",
        "total_amount": 275.00,
        "created_at": dt.datetime(2024, 7, 4),
    },
]

# --- order_items ---------------------------------------------------------
# ids 1-6 are the original seed set — never modify their values. ids 7-20
# add one item per new order (ids 6-20 above) for coverage; item totals are
# not required to reconcile with orders.total_amount (this schema doesn't
# enforce that relationship, matching the original seed set's design).
ORDER_ITEMS_DATA = [
    {
        "id": 1,
        "order_id": 1,
        "product_name": "Mechanical Keyboard",
        "quantity": 1,
        "unit_price": 99.99,
    },
    {"id": 2, "order_id": 1, "product_name": "USB Cable", "quantity": 3, "unit_price": 10.00},
    {"id": 3, "order_id": 2, "product_name": "Notebook", "quantity": 5, "unit_price": 11.90},
    {"id": 4, "order_id": 3, "product_name": "Monitor Stand", "quantity": 1, "unit_price": 249.00},
    {"id": 5, "order_id": 4, "product_name": "Sticker Pack", "quantity": 3, "unit_price": 5.00},
    {"id": 6, "order_id": 5, "product_name": "Desk Lamp", "quantity": 1, "unit_price": 89.90},
    {"id": 7, "order_id": 6, "product_name": "Wireless Mouse", "quantity": 2, "unit_price": 25.00},
    {"id": 8, "order_id": 7, "product_name": "4K Monitor", "quantity": 1, "unit_price": 310.25},
    {"id": 9, "order_id": 8, "product_name": "Notebook", "quantity": 2, "unit_price": 11.90},
    {"id": 10, "order_id": 9, "product_name": "Sticker Pack", "quantity": 2, "unit_price": 5.00},
    {
        "id": 11,
        "order_id": 10,
        "product_name": "Standing Desk",
        "quantity": 1,
        "unit_price": 120.00,
    },
    {
        "id": 12,
        "order_id": 11,
        "product_name": "Ergonomic Chair",
        "quantity": 1,
        "unit_price": 95.75,
    },
    {"id": 13, "order_id": 12, "product_name": "4K Monitor", "quantity": 1, "unit_price": 220.00},
    {"id": 14, "order_id": 13, "product_name": "USB Cable", "quantity": 6, "unit_price": 10.00},
    {"id": 15, "order_id": 14, "product_name": "Sticker Pack", "quantity": 3, "unit_price": 5.00},
    {
        "id": 16,
        "order_id": 15,
        "product_name": "Standing Desk",
        "quantity": 1,
        "unit_price": 340.00,
    },
    {"id": 17, "order_id": 16, "product_name": "Desk Lamp", "quantity": 1, "unit_price": 88.00},
    {
        "id": 18,
        "order_id": 17,
        "product_name": "Mechanical Keyboard",
        "quantity": 2,
        "unit_price": 99.99,
    },
    {
        "id": 19,
        "order_id": 18,
        "product_name": "Wireless Mouse",
        "quantity": 2,
        "unit_price": 25.00,
    },
    {
        "id": 20,
        "order_id": 19,
        "product_name": "Ergonomic Chair",
        "quantity": 1,
        "unit_price": 410.00,
    },
]

# --- products --------------------------------------------------------------
# Independent catalog table — not FK'd to orders/order_items — for testing
# queries against an unrelated table with numeric/boolean columns.
PRODUCTS_DATA = [
    {
        "id": 1,
        "name": "Mechanical Keyboard",
        "category": "Electronics",
        "price": 99.99,
        "in_stock": True,
        "created_at": dt.datetime(2024, 1, 10),
    },
    {
        "id": 2,
        "name": "USB Cable",
        "category": "Electronics",
        "price": 10.00,
        "in_stock": True,
        "created_at": dt.datetime(2024, 1, 10),
    },
    {
        "id": 3,
        "name": "Notebook",
        "category": "Office",
        "price": 11.90,
        "in_stock": True,
        "created_at": dt.datetime(2024, 2, 1),
    },
    {
        "id": 4,
        "name": "Monitor Stand",
        "category": "Furniture",
        "price": 249.00,
        "in_stock": False,
        "created_at": dt.datetime(2024, 2, 15),
    },
    {
        "id": 5,
        "name": "Sticker Pack",
        "category": "Office",
        "price": 5.00,
        "in_stock": True,
        "created_at": dt.datetime(2024, 3, 1),
    },
    {
        "id": 6,
        "name": "Desk Lamp",
        "category": "Furniture",
        "price": 89.90,
        "in_stock": True,
        "created_at": dt.datetime(2024, 3, 10),
    },
    {
        "id": 7,
        "name": "Wireless Mouse",
        "category": "Electronics",
        "price": 25.00,
        "in_stock": True,
        "created_at": dt.datetime(2024, 4, 1),
    },
    {
        "id": 8,
        "name": "4K Monitor",
        "category": "Electronics",
        "price": 310.25,
        "in_stock": False,
        "created_at": dt.datetime(2024, 5, 1),
    },
    {
        "id": 9,
        "name": "Standing Desk",
        "category": "Furniture",
        "price": 450.00,
        "in_stock": True,
        "created_at": dt.datetime(2024, 6, 1),
    },
    {
        "id": 10,
        "name": "Ergonomic Chair",
        "category": "Furniture",
        "price": 275.00,
        "in_stock": True,
        "created_at": dt.datetime(2024, 7, 1),
    },
]

# --- employees ---------------------------------------------------------
# Entirely synthetic PII-shaped data (fake SSNs, .internal email domain) —
# exists specifically to give policy examples/tests a realistic "sensitive
# table" to wall off. See examples/policy.example.yaml: `employees` is
# deliberately left out of `allowed_tables`.
EMPLOYEES_DATA = [
    {
        "id": 1,
        "name": "Grace Han",
        "email": "grace.han@querygate-demo.internal",
        "ssn": "111-22-3333",
        "department": "Engineering",
        "salary": 145000.00,
        "hired_at": dt.datetime(2022, 3, 1),
    },
    {
        "id": 2,
        "name": "Liam Osei",
        "email": "liam.osei@querygate-demo.internal",
        "ssn": "222-33-4444",
        "department": "Engineering",
        "salary": 132000.00,
        "hired_at": dt.datetime(2023, 6, 15),
    },
    {
        "id": 3,
        "name": "Priya Nair",
        "email": "priya.nair@querygate-demo.internal",
        "ssn": "333-44-5555",
        "department": "Sales",
        "salary": 98000.00,
        "hired_at": dt.datetime(2021, 11, 1),
    },
    {
        "id": 4,
        "name": "Tom Reyes",
        "email": "tom.reyes@querygate-demo.internal",
        "ssn": "444-55-6666",
        "department": "Sales",
        "salary": 101000.00,
        "hired_at": dt.datetime(2020, 1, 20),
    },
    {
        "id": 5,
        "name": "Ines Duarte",
        "email": "ines.duarte@querygate-demo.internal",
        "ssn": "555-66-7777",
        "department": "Support",
        "salary": 78000.00,
        "hired_at": dt.datetime(2023, 9, 5),
    },
    {
        "id": 6,
        "name": "Sam Okafor",
        "email": "sam.okafor@querygate-demo.internal",
        "ssn": "666-77-8888",
        "department": "Support",
        "salary": 82000.00,
        "hired_at": dt.datetime(2024, 2, 10),
    },
]


def create_and_seed(engine: sa.engine.Engine) -> None:
    """Sync variant — used by examples/demo_db/seed.py (SQLite file)."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(customers.insert(), CUSTOMERS_DATA)
        conn.execute(orders.insert(), ORDERS_DATA)
        conn.execute(order_items.insert(), ORDER_ITEMS_DATA)
        conn.execute(products.insert(), PRODUCTS_DATA)
        conn.execute(employees.insert(), EMPLOYEES_DATA)


async def create_and_seed_async(engine: AsyncEngine) -> None:
    """Async variant — used by the test suite's in-memory SQLite fixtures."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.execute(customers.insert(), CUSTOMERS_DATA)
        await conn.execute(orders.insert(), ORDERS_DATA)
        await conn.execute(order_items.insert(), ORDER_ITEMS_DATA)
        await conn.execute(products.insert(), PRODUCTS_DATA)
        await conn.execute(employees.insert(), EMPLOYEES_DATA)
