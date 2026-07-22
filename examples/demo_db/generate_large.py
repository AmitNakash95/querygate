"""Deterministic generator for the larger ``large_schema`` demo domain.

Everything here is derived from a single fixed RNG seed, so a given
``--scale`` produces byte-identical rows every run — reproducible for
benchmarks, safe to reseed, and free of the two-file hand-sync problem the
small ``schema.py`` fixtures have (there is no ``init_postgres.sql`` twin to
keep aligned).

Usage
-----
    # Seed the running docker Postgres (matches QUERYGATE_DEMO_DB_URL / make seed-large)
    poetry run python -m examples.demo_db.generate_large --postgres --drop

    # Seed a throwaway local SQLite file for inspection
    poetry run python -m examples.demo_db.generate_large --sqlite /tmp/big.db --drop

    # Emit a Postgres INSERT script instead of touching a database
    poetry run python -m examples.demo_db.generate_large --dump-sql /tmp/big_seed.sql

    # Tune volume (default 1.0 -> ~400k rows; 0.1 for fast dev, 5 for stress)
    SEED_SCALE=0.1 poetry run python -m examples.demo_db.generate_large --postgres --drop

The three targets are mutually independent; pass exactly one of ``--postgres``,
``--sqlite``, or ``--dump-sql``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import random
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.demo_db.large_schema import (  # noqa: E402
    ORDERED_TABLES,
    accounts,
    catalog_products,
    metadata,
    order_lines,
    orders_ext,
    payments,
    regions,
    staff,
    suppliers,
    web_events,
)

# A fixed seed is the whole point — do not randomize this.
SEED = 1729

# The generation window: ~4 years, so date_bucket has real year-over-year
# spread and every finer granularity has many buckets.
WINDOW_START = dt.datetime(2022, 1, 1)
WINDOW_END = dt.datetime(2026, 1, 1)
_WINDOW_SECONDS = int((WINDOW_END - WINDOW_START).total_seconds())

_CENTS = Decimal("0.01")


def _money(value: Decimal | float) -> Decimal:
    return Decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _weighted(rng: random.Random, choices: List[tuple]) -> str:
    """choices: list of (value, weight)."""
    population = [c[0] for c in choices]
    weights = [c[1] for c in choices]
    return rng.choices(population, weights=weights, k=1)[0]


def _rand_dt(rng: random.Random, start: dt.datetime | None = None) -> dt.datetime:
    """A random timestamp with a real time-of-day, at or after ``start``."""
    lo = int(((start or WINDOW_START) - WINDOW_START).total_seconds())
    lo = max(0, min(lo, _WINDOW_SECONDS - 1))
    return WINDOW_START + dt.timedelta(seconds=rng.randint(lo, _WINDOW_SECONDS - 1))


# --- static content pools (kept small; combined for cardinality) -----------

_REGIONS = [
    ("North America East", "US", "USD"),
    ("North America West", "US", "USD"),
    ("United Kingdom", "GB", "GBP"),
    ("Western Europe", "FR", "EUR"),
    ("Central Europe", "DE", "EUR"),
    ("Nordics", "SE", "SEK"),
    ("Asia Pacific", "SG", "SGD"),
    ("Middle East", "AE", "AED"),
]

_FIRST_NAMES = [
    "Ada",
    "Grace",
    "Alan",
    "Katherine",
    "Marie",
    "Charles",
    "Hedy",
    "Radia",
    "Linus",
    "Dennis",
    "Barbara",
    "Margaret",
    "Tim",
    "Vint",
    "Donald",
    "Edsger",
    "Ken",
    "Bjarne",
    "Guido",
    "James",
    "Anita",
    "Shafi",
    "Leslie",
    "John",
    "Frances",
    "Adele",
    "Jean",
    "Sophie",
    "Omar",
    "Yara",
    "Mateo",
    "Ingrid",
]
_LAST_NAMES = [
    "Nakamura",
    "Okafor",
    "Silva",
    "Kowalski",
    "Haddad",
    "Rossi",
    "Andersson",
    "Nguyen",
    "Kim",
    "Patel",
    "Mbeki",
    "Johansson",
    "Duarte",
    "Reyes",
    "Osei",
    "Nair",
    "Fischer",
    "Moreau",
    "Costa",
    "Novak",
    "Ivanov",
    "Cohen",
    "Tan",
    "Abara",
]
_TIERS = [("bronze", 50), ("silver", 30), ("gold", 15), ("platinum", 5)]
_ACCOUNT_STATUS = [("active", 78), ("churned", 15), ("suspended", 7)]

_CATEGORIES: Dict[str, List[str]] = {
    "Electronics": ["Peripherals", "Displays", "Audio", "Cables", "Storage"],
    "Office": ["Paper", "Writing", "Organization", "Desk"],
    "Furniture": ["Seating", "Desks", "Lighting", "Storage"],
    "Home": ["Kitchen", "Decor", "Cleaning"],
    "Outdoor": ["Camping", "Garden", "Sports"],
}
_TAG_POOL = [
    "bestseller",
    "clearance",
    "new",
    "eco",
    "premium",
    "bulk",
    "fragile",
    "heavy",
    "seasonal",
    "refurbished",
]

_ORDER_STATUS = [
    ("delivered", 55),
    ("paid", 18),
    ("shipped", 12),
    ("pending", 8),
    ("cancelled", 5),
    ("refunded", 2),
]
_CHANNELS = [("web", 45), ("mobile", 35), ("store", 15), ("partner", 5)]
_PAY_METHODS = [("card", 60), ("paypal", 22), ("wire", 10), ("store_credit", 8)]
_EVENT_TYPES = [
    ("page_view", 55),
    ("search", 20),
    ("add_to_cart", 12),
    ("login", 8),
    ("checkout", 5),
]
_DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Operations", "Marketing"]
_TITLES = ["Associate", "Senior Associate", "Lead", "Manager", "Director", "VP"]


def _base_counts(scale: float) -> Dict[str, int]:
    def s(n: int) -> int:
        return max(1, int(round(n * scale)))

    return {
        "regions": len(_REGIONS),  # a fixed small dimension, never scaled
        "suppliers": s(25),
        "accounts": s(2000),
        "catalog_products": s(500),
        "staff": s(120),
        "orders_ext": s(30000),
        "web_events": s(250000),
    }


def generate(scale: float) -> Dict[str, List[dict]]:
    """Build every table's rows deterministically for the given scale.

    order_lines and payments counts are derived per-order, so their totals
    vary with the data but are fully determined by ``SEED`` + ``scale``.
    """
    rng = random.Random(SEED)
    counts = _base_counts(scale)

    # --- regions ---
    regions_rows = [
        {
            "region_id": i + 1,
            "name": name,
            "country_code": cc,
            "currency": cur,
            "created_at": WINDOW_START,
        }
        for i, (name, cc, cur) in enumerate(_REGIONS)
    ]
    region_ids = [r["region_id"] for r in regions_rows]

    # --- suppliers ---
    suppliers_rows = []
    for sid in range(1, counts["suppliers"] + 1):
        onboarded = _rand_dt(rng)
        suppliers_rows.append(
            {
                "supplier_id": sid,
                "name": f"{rng.choice(_LAST_NAMES)} Supply Co.",
                "region_id": rng.choice(region_ids),
                # ~10% unrated (freshly onboarded).
                "rating": None if rng.random() < 0.10 else _money(rng.uniform(2.5, 5.0)),
                "active": rng.random() > 0.15,
                "onboarded_at": onboarded,
            }
        )
    supplier_ids = [s["supplier_id"] for s in suppliers_rows]

    # --- accounts ---
    accounts_rows = []
    for aid in range(1, counts["accounts"] + 1):
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        signup = _rand_dt(rng)
        status = _weighted(rng, _ACCOUNT_STATUS)
        # A never-logged-in account has NULL last_login_at (~20%).
        last_login = None if rng.random() < 0.20 else _rand_dt(rng, start=signup)
        accounts_rows.append(
            {
                "account_id": aid,
                "name": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}{aid}@example.com",
                "region_id": rng.choice(region_ids),
                "tier": _weighted(rng, _TIERS),
                "status": status,
                "signup_at": signup,
                "last_login_at": last_login,
                "phone": None if rng.random() < 0.15 else f"+1-555-{rng.randint(1000, 9999)}",
                "lifetime_value": _money(rng.uniform(0, 25000)),
                "is_active": status == "active",
                "national_id": f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
            }
        )
    account_ids = [a["account_id"] for a in accounts_rows]
    account_region = {a["account_id"]: a["region_id"] for a in accounts_rows}

    # --- catalog_products ---
    products_rows = []
    for pid in range(1, counts["catalog_products"] + 1):
        category = rng.choice(list(_CATEGORIES))
        subcategory = rng.choice(_CATEGORIES[category])
        cost = _money(rng.uniform(1.5, 400))
        markup = Decimal(str(rng.uniform(1.15, 2.6)))
        created = _rand_dt(rng)
        # ~12% discontinued (records a date); the rest NULL ("still sold").
        discontinued = None if rng.random() > 0.12 else _rand_dt(rng, start=created)
        n_tags = rng.randint(0, 3)
        tags = ",".join(rng.sample(_TAG_POOL, n_tags)) if n_tags else ""
        products_rows.append(
            {
                "product_id": pid,
                "sku": f"SKU-{pid:05d}",
                "name": f"{subcategory} {category} Model {pid}",
                "category": category,
                "subcategory": subcategory,
                "supplier_id": rng.choice(supplier_ids),
                "cost": cost,
                "list_price": _money(cost * markup),
                # ~18% have no recorded weight.
                "weight_grams": None if rng.random() < 0.18 else rng.randint(20, 15000),
                "discontinued_at": discontinued,
                "tags": tags,
                "in_stock": rng.random() > 0.2,
                "created_at": created,
            }
        )
    product_ids = [p["product_id"] for p in products_rows]
    product_price = {p["product_id"]: p["list_price"] for p in products_rows}

    # --- staff (self-referential manager chain) ---
    staff_rows = []
    manager_pool: List[int] = []
    for sid in range(1, counts["staff"] + 1):
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        hired = _rand_dt(rng)
        # First few are top of the org (NULL manager); the rest report to an
        # already-created staff member, guaranteeing an acyclic chain.
        manager_id = None if sid <= 5 else rng.choice(manager_pool)
        terminated = None if rng.random() > 0.12 else _rand_dt(rng, start=hired)
        staff_rows.append(
            {
                "staff_id": sid,
                "name": f"{first} {last}",
                "work_email": f"{first.lower()}.{last.lower()}@querygate-demo.internal",
                "ssn": f"{rng.randint(100, 899)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
                "date_of_birth": (
                    dt.date(1965, 1, 1) + dt.timedelta(days=rng.randint(0, 365 * 40))
                ),
                "department": rng.choice(_DEPARTMENTS),
                "title": rng.choice(_TITLES),
                "manager_id": manager_id,
                "salary": _money(rng.uniform(55000, 240000)),
                "region_id": rng.choice(region_ids),
                "hired_at": hired,
                "terminated_at": terminated,
            }
        )
        manager_pool.append(sid)

    # --- orders + lines + payments (generated together for consistency) ---
    orders_rows: List[dict] = []
    order_lines_rows: List[dict] = []
    payments_rows: List[dict] = []
    line_id = 1
    payment_id = 1
    for oid in range(1, counts["orders_ext"] + 1):
        account_id = rng.choice(account_ids)
        status = _weighted(rng, _ORDER_STATUS)
        placed = _rand_dt(rng)

        # 1-5 lines per order; order_total is the sum of its line totals.
        n_lines = rng.randint(1, 5)
        order_total = Decimal("0.00")
        for _ in range(n_lines):
            pid = rng.choice(product_ids)
            qty = rng.randint(1, 8)
            unit = product_price[pid]
            discount = _money(rng.choice([0, 0, 0, 5, 10, 15, 25]))
            line_total = _money(Decimal(qty) * unit * (Decimal(1) - discount / Decimal(100)))
            order_total += line_total
            order_lines_rows.append(
                {
                    "line_id": line_id,
                    "order_id": oid,
                    "product_id": pid,
                    "quantity": qty,
                    "unit_price": unit,
                    "discount_pct": discount,
                    "line_total": line_total,
                }
            )
            line_id += 1

        shipped_at = None
        cancelled_at = None
        if status in ("shipped", "delivered"):
            shipped_at = _rand_dt(rng, start=placed)
        if status in ("cancelled", "refunded"):
            cancelled_at = _rand_dt(rng, start=placed)

        orders_rows.append(
            {
                "order_id": oid,
                "account_id": account_id,
                "status": status,
                "channel": _weighted(rng, _CHANNELS),
                "currency": "USD",
                "order_total": _money(order_total),
                "placed_at": placed,
                "shipped_at": shipped_at,
                "cancelled_at": cancelled_at,
                "region_id": account_region[account_id],
            }
        )

        # Payments: paid-through statuses capture the total; refunded adds a
        # negative refund row; cancelled/pending may have a failed/no payment.
        if status in ("paid", "shipped", "delivered", "refunded"):
            payments_rows.append(
                {
                    "payment_id": payment_id,
                    "order_id": oid,
                    "method": _weighted(rng, _PAY_METHODS),
                    "amount": _money(order_total),
                    "status": "captured",
                    "processed_at": _rand_dt(rng, start=placed),
                }
            )
            payment_id += 1
            if status == "refunded":
                payments_rows.append(
                    {
                        "payment_id": payment_id,
                        "order_id": oid,
                        "method": "store_credit",
                        "amount": _money(-order_total),  # negative: refund
                        "status": "refunded",
                        "processed_at": _rand_dt(rng, start=placed),
                    }
                )
                payment_id += 1
        elif status == "cancelled" and rng.random() < 0.4:
            payments_rows.append(
                {
                    "payment_id": payment_id,
                    "order_id": oid,
                    "method": _weighted(rng, _PAY_METHODS),
                    "amount": _money(0),  # zero: authorized then voided
                    "status": "failed",
                    "processed_at": _rand_dt(rng, start=placed),
                }
            )
            payment_id += 1

    # --- web_events (high-volume time-series with intentional NULLs) ---
    web_events_rows = []
    for eid in range(1, counts["web_events"] + 1):
        event_type = _weighted(rng, _EVENT_TYPES)
        # Anonymous (~30%) events have no account; search/login have no product;
        # only checkout carries revenue.
        acct = None if rng.random() < 0.30 else rng.choice(account_ids)
        prod = (
            rng.choice(product_ids)
            if event_type in ("page_view", "add_to_cart", "checkout")
            else None
        )
        revenue = _money(rng.uniform(5, 900)) if event_type == "checkout" else None
        web_events_rows.append(
            {
                "event_id": eid,
                "account_id": acct,
                "event_type": event_type,
                "session_id": f"sess-{rng.randint(0, 10**9):09d}",
                "product_id": prod,
                "revenue": revenue,
                "occurred_at": _rand_dt(rng),
            }
        )

    return {
        "regions": regions_rows,
        "suppliers": suppliers_rows,
        "accounts": accounts_rows,
        "catalog_products": products_rows,
        "staff": staff_rows,
        "orders_ext": orders_rows,
        "order_lines": order_lines_rows,
        "payments": payments_rows,
        "web_events": web_events_rows,
    }


# --- loaders ---------------------------------------------------------------

_INSERT_BATCH = 5000


async def _seed_engine(url: str, data: Dict[str, List[dict]], *, drop: bool) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if drop:
                await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)
            for table in ORDERED_TABLES:
                rows = data[table.name]
                for start in range(0, len(rows), _INSERT_BATCH):
                    await conn.execute(table.insert(), rows[start : start + _INSERT_BATCH])
    finally:
        await engine.dispose()


def _dump_sql(data: Dict[str, List[dict]], path: Path) -> None:
    """Emit a Postgres-dialect INSERT script (no DB connection needed)."""
    from sqlalchemy.dialects import postgresql

    lines: List[str] = [
        "-- Generated by examples/demo_db/generate_large.py. Do not edit by hand;",
        "-- regenerate with the same --scale for byte-identical output.",
        "",
    ]
    for table in ORDERED_TABLES:
        rows = data[table.name]
        for start in range(0, len(rows), _INSERT_BATCH):
            batch = rows[start : start + _INSERT_BATCH]
            stmt = table.insert().values(batch)
            compiled = stmt.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
            lines.append(f"{compiled};")
    path.write_text("\n".join(lines) + "\n")


def _row_summary(data: Dict[str, List[dict]]) -> str:
    total = sum(len(v) for v in data.values())
    parts = ", ".join(f"{k}={len(v):,}" for k, v in data.items())
    return f"{parts}  (total={total:,})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--postgres",
        nargs="?",
        const="",
        metavar="URL",
        help="Seed Postgres. URL defaults to $QUERYGATE_DEMO_DB_URL.",
    )
    target.add_argument("--sqlite", metavar="PATH", help="Seed a local SQLite file.")
    target.add_argument("--dump-sql", metavar="PATH", help="Write a Postgres INSERT script.")
    parser.add_argument(
        "--scale",
        type=float,
        default=float(os.environ.get("SEED_SCALE", "1.0")),
        help="Volume multiplier (default from $SEED_SCALE, else 1.0).",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Drop and recreate the large-domain tables first (idempotent reseed).",
    )
    args = parser.parse_args()

    print(f"Generating large demo domain at scale={args.scale} (seed={SEED}) ...")
    data = generate(args.scale)
    print(f"  {_row_summary(data)}")

    if args.dump_sql is not None:
        out = Path(args.dump_sql)
        _dump_sql(data, out)
        print(f"Wrote Postgres INSERT script to {out}")
        return

    if args.sqlite is not None:
        url = f"sqlite+aiosqlite:///{Path(args.sqlite).resolve()}"
    else:
        url = args.postgres or os.environ.get("QUERYGATE_DEMO_DB_URL", "")
        if not url:
            parser.error("--postgres needs a URL or $QUERYGATE_DEMO_DB_URL set.")

    asyncio.run(_seed_engine(url, data, drop=args.drop))
    print(f"Seeded {'SQLite' if args.sqlite else 'Postgres'} at {url.split('@')[-1]}")


if __name__ == "__main__":
    main()
