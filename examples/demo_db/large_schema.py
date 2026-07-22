"""A larger, richer, deliberately real-world-shaped demo domain.

This is the *volume/complexity* dataset — a companion to ``schema.py``'s five
small frozen fixture tables, not a replacement. ``schema.py`` stays tiny and
its first rows stay byte-frozen because the test suite asserts exact values
against them; nothing here is asserted on, so it is free to be large, varied,
and NULL-laden.

Where ``schema.py``'s data is hand-written literals kept in sync across two
files by hand, this domain is **procedurally generated** by
``generate_large.py`` from a fixed RNG seed, so:

- it is reproducible (same seed -> byte-identical rows), and
- there is no second hand-maintained ``init_postgres.sql`` to drift — the same
  generator seeds a local SQLite file *and* the docker Postgres over the live
  engine (``make seed-large``).

The point is to give you something you can actually *sit down and stress* over
MCP/REST: exercise the security walls, the feature surface, and the
StructuredQuery engine against data that behaves like production rather than a
toy. The shape is chosen to hit the things the tiny fixture set can't:

- **A real FK graph with multi-hop and self joins** — order_lines ->
  orders -> accounts -> regions, order_lines -> catalog_products ->
  suppliers -> regions, payments -> orders, web_events -> accounts/products,
  and staff.manager_id -> staff (self-join).
- **NULLs on purpose** — accounts.last_login_at/phone, catalog_products.
  discontinued_at/weight_grams, orders.shipped_at/cancelled_at,
  staff.manager_id/terminated_at, web_events.account_id/product_id/revenue —
  so NULLS FIRST/LAST ordering, COALESCE, and null-in-aggregate behavior have
  something to bite on.
- **Multi-year timestamps with a time-of-day** — placed_at/occurred_at span
  ~4 years and are not midnight-aligned, so date_bucket at every granularity
  (incl. year) and finer buckets are meaningful.
- **High cardinality and realistic skew** — enough accounts/orders/lines per
  group that top_n-per-partition, percentile_cont, and stddev/variance return
  something other than a single-row group; status/channel/tier distributions
  are skewed, not uniform.
- **Negative and zero money** — payments.amount is negative for refunds.
- **PII-shaped columns to wall off / mask** — staff (ssn, salary,
  date_of_birth) is a "deny the whole table" target; accounts.national_id /
  email are "allow the table, mask/deny the column" targets.

All values are synthetic. Table names are distinct from ``schema.py``'s five
tables so both domains can live in the same physical database at once (the
``demo`` connection exposes the frozen five; the ``retail``/``analytics``
connections expose these — see connections.example.yaml).
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

# --- dimensions ------------------------------------------------------------

regions = sa.Table(
    "regions",
    metadata,
    sa.Column("region_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(60), nullable=False),
    sa.Column("country_code", sa.String(2), nullable=False),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

suppliers = sa.Table(
    "suppliers",
    metadata,
    sa.Column("supplier_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
    # Nullable on purpose: a freshly onboarded supplier has no rating yet.
    sa.Column("rating", sa.Numeric(3, 2), nullable=True),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("onboarded_at", sa.DateTime, nullable=False),
)

# --- customers -------------------------------------------------------------

accounts = sa.Table(
    "accounts",
    metadata,
    sa.Column("account_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
    # Low-cardinality categorical for group_by / top_n-per-partition.
    sa.Column("tier", sa.String(10), nullable=False),  # bronze|silver|gold|platinum
    sa.Column("status", sa.String(12), nullable=False),  # active|churned|suspended
    sa.Column("signup_at", sa.DateTime, nullable=False),
    # ~20% NULL: customers who have never logged in.
    sa.Column("last_login_at", sa.DateTime, nullable=True),
    # ~15% NULL: no phone on file.
    sa.Column("phone", sa.String(24), nullable=True),
    sa.Column("lifetime_value", sa.Numeric(12, 2), nullable=False),
    sa.Column("is_active", sa.Boolean, nullable=False),
    # PII-shaped: "allow the table, mask/deny this column" target.
    sa.Column("national_id", sa.String(20), nullable=False),
)

# --- catalog ---------------------------------------------------------------

catalog_products = sa.Table(
    "catalog_products",
    metadata,
    sa.Column("product_id", sa.Integer, primary_key=True),
    sa.Column("sku", sa.String(20), nullable=False),
    sa.Column("name", sa.String(140), nullable=False),
    sa.Column("category", sa.String(40), nullable=False),
    sa.Column("subcategory", sa.String(40), nullable=False),
    sa.Column("supplier_id", sa.Integer, sa.ForeignKey("suppliers.supplier_id"), nullable=False),
    sa.Column("cost", sa.Numeric(10, 2), nullable=False),
    sa.Column("list_price", sa.Numeric(10, 2), nullable=False),
    # Nullable: not every product records a shipping weight.
    sa.Column("weight_grams", sa.Integer, nullable=True),
    # Nullable: NULL means "still sold"; a date means "discontinued then".
    sa.Column("discontinued_at", sa.DateTime, nullable=True),
    sa.Column("tags", sa.Text, nullable=False),
    sa.Column("in_stock", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
)

# --- sensitive HR table (walled off by default) ----------------------------

staff = sa.Table(
    "staff",
    metadata,
    sa.Column("staff_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("work_email", sa.String(200), nullable=False),
    sa.Column("ssn", sa.String(20), nullable=False),
    sa.Column("date_of_birth", sa.Date, nullable=False),
    sa.Column("department", sa.String(40), nullable=False),
    sa.Column("title", sa.String(60), nullable=False),
    # Self-FK: NULL for the top of the org chart.
    sa.Column("manager_id", sa.Integer, sa.ForeignKey("staff.staff_id"), nullable=True),
    sa.Column("salary", sa.Numeric(10, 2), nullable=False),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
    sa.Column("hired_at", sa.DateTime, nullable=False),
    # Nullable: NULL means still employed.
    sa.Column("terminated_at", sa.DateTime, nullable=True),
)

# --- orders ----------------------------------------------------------------

orders_ext = sa.Table(
    "orders_ext",
    metadata,
    sa.Column("order_id", sa.Integer, primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.account_id"), nullable=False),
    # Skewed lifecycle distribution (mostly delivered/paid, few cancelled).
    sa.Column("status", sa.String(12), nullable=False),
    sa.Column("channel", sa.String(10), nullable=False),  # web|mobile|store|partner
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("order_total", sa.Numeric(12, 2), nullable=False),
    sa.Column("placed_at", sa.DateTime, nullable=False),
    # Nullable: only set once the order actually shipped.
    sa.Column("shipped_at", sa.DateTime, nullable=True),
    # Nullable: only set for cancelled/refunded orders.
    sa.Column("cancelled_at", sa.DateTime, nullable=True),
    sa.Column("region_id", sa.Integer, sa.ForeignKey("regions.region_id"), nullable=False),
)

order_lines = sa.Table(
    "order_lines",
    metadata,
    sa.Column("line_id", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders_ext.order_id"), nullable=False),
    sa.Column(
        "product_id", sa.Integer, sa.ForeignKey("catalog_products.product_id"), nullable=False
    ),
    sa.Column("quantity", sa.Integer, nullable=False),
    sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
    sa.Column("discount_pct", sa.Numeric(5, 2), nullable=False),
    sa.Column("line_total", sa.Numeric(12, 2), nullable=False),
)

payments = sa.Table(
    "payments",
    metadata,
    sa.Column("payment_id", sa.Integer, primary_key=True),
    sa.Column("order_id", sa.Integer, sa.ForeignKey("orders_ext.order_id"), nullable=False),
    sa.Column("method", sa.String(16), nullable=False),  # card|paypal|wire|store_credit
    # Negative for refunds/chargebacks; positive for captures.
    sa.Column("amount", sa.Numeric(12, 2), nullable=False),
    sa.Column("status", sa.String(12), nullable=False),  # captured|refunded|failed
    sa.Column("processed_at", sa.DateTime, nullable=False),
)

# --- high-volume event log (time-series) -----------------------------------

web_events = sa.Table(
    "web_events",
    metadata,
    # Anonymous events have a NULL account_id; product-less events (search,
    # login) have a NULL product_id; non-purchase events have NULL revenue.
    sa.Column("event_id", sa.Integer, primary_key=True),
    sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.account_id"), nullable=True),
    sa.Column("event_type", sa.String(16), nullable=False),
    sa.Column("session_id", sa.String(36), nullable=False),
    sa.Column(
        "product_id", sa.Integer, sa.ForeignKey("catalog_products.product_id"), nullable=True
    ),
    sa.Column("revenue", sa.Numeric(10, 2), nullable=True),
    sa.Column("occurred_at", sa.DateTime, nullable=False),
)

# Insert order respects FK dependencies (parents before children).
ORDERED_TABLES = [
    regions,
    suppliers,
    accounts,
    catalog_products,
    staff,
    orders_ext,
    order_lines,
    payments,
    web_events,
]

# Tables each logical connection exposes (mirrored in connections.example.yaml
# / policy.example.yaml). `staff` is intentionally absent from every list —
# it is the "wall off the sensitive table" target, known to the DB but not
# exposed by any connection's allowed_tables.
RETAIL_TABLES = [
    "regions",
    "suppliers",
    "accounts",
    "catalog_products",
    "orders_ext",
    "order_lines",
    "payments",
    "web_events",
]
ANALYTICS_TABLES = ["regions", "accounts", "orders_ext", "order_lines", "web_events"]
