-- Vault schema. Apply once at provisioning.
--
-- Roles (recommended; not created here because role naming is environment-specific):
--   putsch_vault_writer    -- INSERT on tokens + audit_log; SELECT on tokens (for vault-internal use)
--   putsch_vault_reader    -- SELECT on audit_log only (dashboards)
--   putsch_vault_auditor   -- SELECT on both tables (operators)
--   putsch_vault_owner     -- DDL only; not used by the application

CREATE SCHEMA IF NOT EXISTS putsch_vault;

-- ─────────────────────────────────────────────────────────────────────────────
-- tokens
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS putsch_vault.tokens (
    token         TEXT        PRIMARY KEY,
    category      TEXT        NOT NULL,
    ciphertext    BYTEA       NOT NULL,
    context_hint  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tokens_category_idx ON putsch_vault.tokens (category);
CREATE INDEX IF NOT EXISTS tokens_created_at_idx ON putsch_vault.tokens (created_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- audit_log — append-only, hash-chained
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS putsch_vault.audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    actor       TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    ticket      TEXT,
    token       TEXT        NOT NULL,
    category    TEXT        NOT NULL,
    outcome     TEXT        NOT NULL CHECK (outcome IN ('ok','not_found','decrypt_failed','denied')),
    prev_hash   CHAR(64)    NOT NULL,
    row_hash    CHAR(64)    NOT NULL,
    payload     JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_actor_idx     ON putsch_vault.audit_log (actor);
CREATE INDEX IF NOT EXISTS audit_token_idx     ON putsch_vault.audit_log (token);
CREATE INDEX IF NOT EXISTS audit_occurred_idx  ON putsch_vault.audit_log (occurred_at DESC);

-- WORM enforcement. The trigger raises on UPDATE / DELETE / TRUNCATE.
CREATE OR REPLACE FUNCTION putsch_vault.audit_worm()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only; %s is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_no_update ON putsch_vault.audit_log;
CREATE TRIGGER audit_no_update
    BEFORE UPDATE OR DELETE ON putsch_vault.audit_log
    FOR EACH ROW EXECUTE FUNCTION putsch_vault.audit_worm();

DROP TRIGGER IF EXISTS audit_no_truncate ON putsch_vault.audit_log;
CREATE TRIGGER audit_no_truncate
    BEFORE TRUNCATE ON putsch_vault.audit_log
    EXECUTE FUNCTION putsch_vault.audit_worm();

-- Reasonable default privileges. Replace role names per environment.
-- REVOKE ALL ON putsch_vault.audit_log FROM PUBLIC;
-- GRANT INSERT, SELECT ON putsch_vault.audit_log TO putsch_vault_writer;
-- GRANT SELECT ON putsch_vault.audit_log TO putsch_vault_reader, putsch_vault_auditor;
