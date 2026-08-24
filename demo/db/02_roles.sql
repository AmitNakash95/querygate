-- QueryGate partner-demo pitch database roles (demo/SPEC.md, "Roles" section).
-- Executed once at container init, immediately after 01_schema.sql, as
-- pitch_owner (the POSTGRES_USER superuser created by the postgres image from
-- docker-compose.demo.yml's environment block).
--
-- DEMO-ONLY THROWAWAY PASSWORDS. These are not real credentials, are never
-- used outside this local, 127.0.0.1-only pitch database, and are safe to
-- commit in plaintext. Do not reuse them anywhere else.

-- pitch_owner already exists (created by the postgres image from
-- POSTGRES_USER=pitch_owner in docker-compose.demo.yml) and owns every table
-- created by 01_schema.sql. It is used only for schema migrations and
-- seeding, never connected to by either MCP server.

-- ---------------------------------------------------------------------------
-- agent_ro: the role BOTH the baseline (unsafe) MCP server and QueryGate
-- connect as. The whole point of the demo is that identical database
-- privileges (genuinely read-only) give opposite outcomes depending on
-- whether a policy gate sits in front of the connection.
-- ---------------------------------------------------------------------------
CREATE ROLE agent_ro WITH LOGIN PASSWORD 'agent_ro_demo_pw_only';

GRANT CONNECT ON DATABASE querygate_demo_pitch TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agent_ro;
ALTER ROLE agent_ro SET default_transaction_read_only = on;

-- ---------------------------------------------------------------------------
-- probe_user: read-only, used only by the control UI's health/latency probe
-- (GET /api/probe) so probe traffic is never confused with agent traffic in
-- pg_stat_statements counts.
-- ---------------------------------------------------------------------------
CREATE ROLE probe_user WITH LOGIN PASSWORD 'probe_user_demo_pw_only';

GRANT CONNECT ON DATABASE querygate_demo_pitch TO probe_user;
GRANT USAGE ON SCHEMA public TO probe_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO probe_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO probe_user;
ALTER ROLE probe_user SET default_transaction_read_only = on;
