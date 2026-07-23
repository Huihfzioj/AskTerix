DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_name}) THEN
        EXECUTE format('CREATE ROLE %I LOGIN', {role_name});
    END IF;
END
$$;

ALTER ROLE {role} WITH PASSWORD {password};

-- Secondary guard. The absent write grants below are the real boundary; this
-- refuses writes even if a grant is added by mistake.
ALTER ROLE {role} SET default_transaction_read_only = on;

-- The only protection against a generated query that is readable but ruinous.
-- Matters more on a single instance than it would against a replica.
ALTER ROLE {role} SET statement_timeout = {statement_timeout};

GRANT CONNECT ON DATABASE {database} TO {role};
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role};

-- Without FOR ROLE, default privileges apply only to tables created by whoever
-- runs this file. Naming the owner explicitly means tables created later by
-- that role are readable too -- otherwise F1 silently cannot see new tables,
-- which surfaces as "the model can't find my table" rather than as an error.
ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public
    GRANT SELECT ON TABLES TO {role};

-- No-op on PostgreSQL 15+, where PUBLIC no longer holds CREATE on the public
-- schema. Kept so this file is still correct against an older cluster.
REVOKE CREATE ON SCHEMA public FROM {role};
