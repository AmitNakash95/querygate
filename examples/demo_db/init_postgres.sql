-- Seeds the example demo database (customers/orders/order_items/products/
-- employees) on first container start via docker-compose.yml's
-- postgres-init-scripts mount. Mirrors examples/demo_db/schema.py exactly —
-- keep both in sync by hand if you change one.

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(200) NOT NULL,
    country VARCHAR(2) NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status VARCHAR(20) NOT NULL,
    total_amount NUMERIC(10, 2) NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_name VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    category VARCHAR(50) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    in_stock BOOLEAN NOT NULL,
    created_at TIMESTAMP NOT NULL
);

-- Entirely synthetic PII-shaped data (fake SSNs, .internal email domain) —
-- exists so policy examples have a realistic "sensitive table" to wall off.
-- See examples/policy.example.yaml: `employees` is deliberately left out of
-- allowed_tables.
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(200) NOT NULL,
    ssn VARCHAR(20) NOT NULL,
    department VARCHAR(50) NOT NULL,
    salary NUMERIC(10, 2) NOT NULL,
    hired_at TIMESTAMP NOT NULL
);

INSERT INTO customers (id, name, email, country, created_at) VALUES
    (1, 'Ada Lovelace', 'ada@example.com', 'GB', '2025-01-05'),
    (2, 'Grace Hopper', 'grace@example.com', 'US', '2025-01-12'),
    (3, 'Alan Turing', 'alan@example.com', 'GB', '2025-02-02'),
    (4, 'Katherine Johnson', 'katherine@example.com', 'US', '2025-02-20'),
    (5, 'Marie Curie', 'marie@example.com', 'FR', '2024-11-03'),
    (6, 'Charles Babbage', 'charles@example.com', 'GB', '2024-12-01'),
    (7, 'Hedy Lamarr', 'hedy@example.com', 'US', '2025-01-20'),
    (8, 'Radia Perlman', 'radia@example.com', 'US', '2025-04-02')
ON CONFLICT DO NOTHING;

INSERT INTO orders (id, customer_id, status, total_amount, created_at) VALUES
    (1, 1, 'completed', 129.99, '2025-01-10'),
    (2, 1, 'completed', 59.50, '2025-03-01'),
    (3, 2, 'pending', 249.00, '2025-02-15'),
    (4, 3, 'completed', 15.00, '2025-02-05'),
    (5, 4, 'cancelled', 89.90, '2025-03-10'),
    (6, 2, 'completed', 75.00, '2024-11-15'),
    (7, 2, 'completed', 310.25, '2025-05-02'),
    (8, 3, 'completed', 42.00, '2024-09-10'),
    (9, 3, 'cancelled', 18.50, '2025-01-05'),
    (10, 4, 'completed', 120.00, '2024-08-22'),
    (11, 4, 'completed', 95.75, '2025-06-01'),
    (12, 5, 'completed', 220.00, '2024-11-10'),
    (13, 5, 'pending', 60.00, '2025-02-14'),
    (14, 6, 'completed', 15.99, '2024-12-05'),
    (15, 6, 'completed', 340.00, '2025-03-20'),
    (16, 7, 'refunded', 88.00, '2025-01-25'),
    (17, 7, 'completed', 199.99, '2025-04-18'),
    (18, 8, 'completed', 55.50, '2025-04-10'),
    (19, 8, 'completed', 410.00, '2025-05-30'),
    (20, 1, 'completed', 275.00, '2024-07-04')
ON CONFLICT DO NOTHING;

INSERT INTO order_items (id, order_id, product_name, quantity, unit_price) VALUES
    (1, 1, 'Mechanical Keyboard', 1, 99.99),
    (2, 1, 'USB Cable', 3, 10.00),
    (3, 2, 'Notebook', 5, 11.90),
    (4, 3, 'Monitor Stand', 1, 249.00),
    (5, 4, 'Sticker Pack', 3, 5.00),
    (6, 5, 'Desk Lamp', 1, 89.90),
    (7, 6, 'Wireless Mouse', 2, 25.00),
    (8, 7, '4K Monitor', 1, 310.25),
    (9, 8, 'Notebook', 2, 11.90),
    (10, 9, 'Sticker Pack', 2, 5.00),
    (11, 10, 'Standing Desk', 1, 120.00),
    (12, 11, 'Ergonomic Chair', 1, 95.75),
    (13, 12, '4K Monitor', 1, 220.00),
    (14, 13, 'USB Cable', 6, 10.00),
    (15, 14, 'Sticker Pack', 3, 5.00),
    (16, 15, 'Standing Desk', 1, 340.00),
    (17, 16, 'Desk Lamp', 1, 88.00),
    (18, 17, 'Mechanical Keyboard', 2, 99.99),
    (19, 18, 'Wireless Mouse', 2, 25.00),
    (20, 19, 'Ergonomic Chair', 1, 410.00)
ON CONFLICT DO NOTHING;

INSERT INTO products (id, name, category, price, in_stock, created_at) VALUES
    (1, 'Mechanical Keyboard', 'Electronics', 99.99, TRUE, '2024-01-10'),
    (2, 'USB Cable', 'Electronics', 10.00, TRUE, '2024-01-10'),
    (3, 'Notebook', 'Office', 11.90, TRUE, '2024-02-01'),
    (4, 'Monitor Stand', 'Furniture', 249.00, FALSE, '2024-02-15'),
    (5, 'Sticker Pack', 'Office', 5.00, TRUE, '2024-03-01'),
    (6, 'Desk Lamp', 'Furniture', 89.90, TRUE, '2024-03-10'),
    (7, 'Wireless Mouse', 'Electronics', 25.00, TRUE, '2024-04-01'),
    (8, '4K Monitor', 'Electronics', 310.25, FALSE, '2024-05-01'),
    (9, 'Standing Desk', 'Furniture', 450.00, TRUE, '2024-06-01'),
    (10, 'Ergonomic Chair', 'Furniture', 275.00, TRUE, '2024-07-01')
ON CONFLICT DO NOTHING;

INSERT INTO employees (id, name, email, ssn, department, salary, hired_at) VALUES
    (1, 'Grace Han', 'grace.han@querygate-demo.internal', '111-22-3333', 'Engineering', 145000.00, '2022-03-01'),
    (2, 'Liam Osei', 'liam.osei@querygate-demo.internal', '222-33-4444', 'Engineering', 132000.00, '2023-06-15'),
    (3, 'Priya Nair', 'priya.nair@querygate-demo.internal', '333-44-5555', 'Sales', 98000.00, '2021-11-01'),
    (4, 'Tom Reyes', 'tom.reyes@querygate-demo.internal', '444-55-6666', 'Sales', 101000.00, '2020-01-20'),
    (5, 'Ines Duarte', 'ines.duarte@querygate-demo.internal', '555-66-7777', 'Support', 78000.00, '2023-09-05'),
    (6, 'Sam Okafor', 'sam.okafor@querygate-demo.internal', '666-77-8888', 'Support', 82000.00, '2024-02-10')
ON CONFLICT DO NOTHING;
