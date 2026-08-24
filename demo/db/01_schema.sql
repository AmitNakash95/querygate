-- QueryGate partner-demo pitch database schema.
-- Deliberately boring and instantly readable on a projector (demo/SPEC.md,
-- "Database" section). Executed once at container init by the Postgres
-- entrypoint (docker-entrypoint-initdb.d), as pitch_owner, against
-- querygate_demo_pitch.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ---------------------------------------------------------------------------
-- customers: the table the "campaign export" and "bulk export" scenarios
-- target. email/phone/national_id are the column-level PII targets.
-- ---------------------------------------------------------------------------
CREATE TABLE customers (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL,
    national_id TEXT NOT NULL,
    country     TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_customers_country ON customers (country);
CREATE INDEX idx_customers_created_at ON customers (created_at);
CREATE INDEX idx_customers_is_active ON customers (is_active);

-- ---------------------------------------------------------------------------
-- orders: one row per order, FK to customers.
-- ---------------------------------------------------------------------------
CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    customer_id  BIGINT NOT NULL REFERENCES customers (id),
    status       TEXT NOT NULL,
    total_amount NUMERIC(12, 2) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_orders_customer_id ON orders (customer_id);
CREATE INDEX idx_orders_status ON orders (status);
CREATE INDEX idx_orders_created_at ON orders (created_at);

-- ---------------------------------------------------------------------------
-- order_items: line items, FK to orders. This is the table that makes the
-- "overload" cross-join scenario genuinely destructive (customers x orders x
-- order_items with no join predicate).
-- ---------------------------------------------------------------------------
CREATE TABLE order_items (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders (id),
    product_name TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    unit_price   NUMERIC(12, 2) NOT NULL
);

CREATE INDEX idx_order_items_order_id ON order_items (order_id);

-- ---------------------------------------------------------------------------
-- products: standalone catalog table.
-- ---------------------------------------------------------------------------
CREATE TABLE products (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    price      NUMERIC(12, 2) NOT NULL,
    in_stock   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_products_category ON products (category);

-- ---------------------------------------------------------------------------
-- employees: the walled-off HR table. Never exposed through the
-- allowed_tables policy in the "gate ON" path (demo/SPEC.md scenario
-- pii_table). Only 12 rows, but present so the "gate OFF" baseline can prove
-- it happily returns SSNs and salaries.
-- ---------------------------------------------------------------------------
CREATE TABLE employees (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    ssn        TEXT NOT NULL,
    department TEXT NOT NULL,
    salary     NUMERIC(12, 2) NOT NULL,
    manager    TEXT,
    hired_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_employees_department ON employees (department);
