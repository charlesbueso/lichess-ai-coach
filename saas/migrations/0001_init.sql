-- Initial multi-tenant schema for Lichess AI Coach SaaS.
-- Apply with: psql "$DATABASE_URL" -f saas/migrations/0001_init.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
    CREATE TYPE tenant_status AS ENUM (
        'pending',     -- Stripe checkout completed, Discord install not yet finished
        'trialing',    -- in 7-day trial
        'active',      -- paying
        'past_due',    -- payment failed, still in grace
        'canceled'     -- soft-deleted, poll skipped
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_customer_id        TEXT UNIQUE NOT NULL,
    stripe_subscription_id    TEXT,
    discord_guild_id          BIGINT UNIQUE,
    discord_installer_user_id BIGINT,
    discord_channel_id        BIGINT,
    lichess_username          TEXT,
    status                    tenant_status NOT NULL DEFAULT 'pending',
    trial_end                 TIMESTAMPTZ,
    last_game_ms              BIGINT NOT NULL DEFAULT 0,
    last_poll_at              TIMESTAMPTZ,
    install_email             TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tenants_status_idx
    ON tenants(status);
CREATE INDEX IF NOT EXISTS tenants_active_idx
    ON tenants(id) WHERE status IN ('trialing','active');

CREATE TABLE IF NOT EXISTS games (
    tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    game_id       TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL,
    stored_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    pgn           TEXT NOT NULL,
    raw_json      JSONB NOT NULL,
    summary       TEXT,
    feedback      TEXT,
    sections      JSONB,
    key_moments   JSONB,
    thread_id     BIGINT,
    PRIMARY KEY (tenant_id, game_id)
);

CREATE INDEX IF NOT EXISTS games_tenant_thread_idx
    ON games(tenant_id, thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS games_tenant_created_idx
    ON games(tenant_id, created_at_ms DESC);

CREATE TABLE IF NOT EXISTS usage_daily (
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day            DATE NOT NULL,
    games_analyzed INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

CREATE TABLE IF NOT EXISTS ask_usage (
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    game_id    TEXT NOT NULL,
    ask_count  INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, game_id)
);

CREATE TABLE IF NOT EXISTS pending_installs (
    token                  TEXT PRIMARY KEY,
    stripe_customer_id     TEXT NOT NULL,
    stripe_subscription_id TEXT,
    install_email          TEXT,
    expires_at             TIMESTAMPTZ NOT NULL,
    consumed_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS pending_installs_expires_idx
    ON pending_installs(expires_at);
