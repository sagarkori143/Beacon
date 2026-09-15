-- Database bootstrap. Runs once, as superuser, before any migration.
--
-- The important part is the two-role split. Migrations run as the OWNER;
-- the application connects as APP_RW, which owns nothing and cannot bypass
-- Row Level Security.
--
-- This matters more than it looks. A table's owner is exempt from its own
-- policies unless FORCE ROW LEVEL SECURITY is set, so an application connecting
-- as the owner would silently see every tenant's rows the moment one table
-- missed its FORCE. Separating the roles means RLS binds even if that happens.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        -- Password is supplied by the environment; this default is only for a
        -- local Compose stack that is not reachable from outside the network.
        CREATE ROLE app_rw LOGIN PASSWORD 'app_rw'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE agentdb TO app_rw;
GRANT USAGE ON SCHEMA public TO app_rw;

-- Applies to tables the migrations have not created yet.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_rw;

-- Fail loudly rather than degrade quietly when a transaction is left open
-- across a slow provider call. See docs/tradeoffs.md.
ALTER ROLE app_rw SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE app_rw SET statement_timeout = '30s';
