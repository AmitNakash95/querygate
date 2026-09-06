# Pitch demo database

A dedicated, isolated Postgres 16 instance for the QueryGate partner-demo
(`demo/SPEC.md`). Fully independent of the repo root `docker-compose.yml` /
`querygate-querygate-demo-db-1` (port 5433) — different compose project name
(`querygate-pitch`), different container name (`querygate-pitch-db`),
different named volume (`querygate_pitch_db_data`), different port
(**127.0.0.1:5544**), different database name (`querygate_demo_pitch`).

## Bring it up

From the repo root, once `demo/db/Makefile.include` is wired into the root
Makefile:

```bash
make pitch-db-up      # start + wait for healthy
make pitch-db-seed    # deterministic, idempotent seed (~250k customers, ~600k
                       # orders, ~1.2M order_items, 500 products, 12 employees)
```

Or directly, without the root Makefile:

```bash
docker compose -f demo/db/docker-compose.demo.yml up -d
.venv/bin/python demo/db/seed.py
```

## Verify

```bash
# pg_stat_statements is enabled and queryable
docker compose -f demo/db/docker-compose.demo.yml exec querygate-pitch-db \
  psql -U pitch_owner -d querygate_demo_pitch \
  -c "SELECT count(*) FROM pg_stat_statements;"

# row counts
docker compose -f demo/db/docker-compose.demo.yml exec querygate-pitch-db \
  psql -U pitch_owner -d querygate_demo_pitch -c "
    SELECT 'customers', count(*) FROM customers
    UNION ALL SELECT 'orders', count(*) FROM orders
    UNION ALL SELECT 'order_items', count(*) FROM order_items
    UNION ALL SELECT 'products', count(*) FROM products
    UNION ALL SELECT 'employees', count(*) FROM employees;"

# agent_ro is genuinely read-only (all of these must fail)
psql "postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch" \
  -c "INSERT INTO products (name, category, price) VALUES ('x','x',1);"
```

## Connection strings

```
pitch_owner: postgresql://pitch_owner:pitch_owner_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch
agent_ro:    postgresql://agent_ro:agent_ro_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch
probe_user:  postgresql://probe_user:probe_user_demo_pw_only@127.0.0.1:5544/querygate_demo_pitch
```

All three are throwaway, demo-only passwords, literal in `02_roles.sql`. This
database is bound to `127.0.0.1` only and holds no real data (see the header
comment in `seed.py` for exactly how the PII is kept obviously synthetic).

## Tear down

```bash
make pitch-db-down     # stops the container, keeps the volume (data survives)
make pitch-db-reset     # drops the volume too, then brings it back up and reseeds
```

or directly:

```bash
docker compose -f demo/db/docker-compose.demo.yml down       # keep volume
docker compose -f demo/db/docker-compose.demo.yml down -v    # drop volume too
```
