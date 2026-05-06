"""asyncpg pool + multi-tenant data access for the SaaS."""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from typing import Optional, Any

import asyncpg

from saas import app_config

log = logging.getLogger("coach.db")

_pool: Optional[asyncpg.Pool] = None


# --- pool lifecycle -------------------------------------------------------

async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=app_config.DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        log.info("Postgres pool initialised")
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised; call init_pool() first")
    return _pool


# --- helpers --------------------------------------------------------------

def _row_to_dict(r: Optional[asyncpg.Record]) -> Optional[dict]:
    return dict(r) if r is not None else None


def _maybe_json(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


# --- tenants --------------------------------------------------------------

ACTIVE_STATUSES = ("trialing", "active")


async def get_active_tenants() -> list[dict]:
    """Tenants currently eligible for polling."""
    async with pool().acquire() as c:
        rows = await c.fetch(
            """
            SELECT * FROM tenants
            WHERE status = ANY($1::tenant_status[])
              AND lichess_username IS NOT NULL
              AND discord_channel_id IS NOT NULL
            ORDER BY id
            """,
            list(ACTIVE_STATUSES),
        )
    return [dict(r) for r in rows]


async def get_tenant_by_guild(guild_id: int) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            "SELECT * FROM tenants WHERE discord_guild_id = $1", guild_id
        )
    return _row_to_dict(r)


async def get_tenant_by_customer(stripe_customer_id: str) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            "SELECT * FROM tenants WHERE stripe_customer_id = $1", stripe_customer_id
        )
    return _row_to_dict(r)


async def upsert_tenant_from_stripe(
    stripe_customer_id: str,
    stripe_subscription_id: Optional[str],
    status: str,
    trial_end: Optional[dt.datetime],
    install_email: Optional[str] = None,
) -> dict:
    """Create or update the tenant row off Stripe webhook data."""
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            INSERT INTO tenants (stripe_customer_id, stripe_subscription_id, status, trial_end, install_email)
            VALUES ($1, $2, $3::tenant_status, $4, $5)
            ON CONFLICT (stripe_customer_id) DO UPDATE
                SET stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id, tenants.stripe_subscription_id),
                    status                 = EXCLUDED.status,
                    trial_end              = COALESCE(EXCLUDED.trial_end, tenants.trial_end),
                    install_email          = COALESCE(EXCLUDED.install_email, tenants.install_email),
                    updated_at             = now()
            RETURNING *
            """,
            stripe_customer_id, stripe_subscription_id, status, trial_end, install_email,
        )
    return dict(r)


async def set_tenant_status(stripe_customer_id: str, status: str,
                            trial_end: Optional[dt.datetime] = None) -> None:
    async with pool().acquire() as c:
        await c.execute(
            """
            UPDATE tenants
               SET status = $2::tenant_status,
                   trial_end = COALESCE($3, trial_end),
                   updated_at = now()
             WHERE stripe_customer_id = $1
            """,
            stripe_customer_id, status, trial_end,
        )


async def bind_discord_install(
    stripe_customer_id: str,
    guild_id: int,
    installer_user_id: int,
) -> Optional[dict]:
    """Attach a Discord guild + installer to an existing tenant.

    Returns the updated row, or None if no tenant exists for that customer.
    """
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            UPDATE tenants
               SET discord_guild_id          = $2,
                   discord_installer_user_id = $3,
                   updated_at                = now()
             WHERE stripe_customer_id = $1
             RETURNING *
            """,
            stripe_customer_id, guild_id, installer_user_id,
        )
    return _row_to_dict(r)


async def set_tenant_setup(
    guild_id: int,
    lichess_username: str,
    discord_channel_id: int,
) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            UPDATE tenants
               SET lichess_username   = $2,
                   discord_channel_id = $3,
                   updated_at         = now()
             WHERE discord_guild_id   = $1
             RETURNING *
            """,
            guild_id, lichess_username, discord_channel_id,
        )
    return _row_to_dict(r)


async def update_last_game_ms(tenant_id, ms: int) -> None:
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE tenants SET last_game_ms = $2, last_poll_at = now() WHERE id = $1",
            tenant_id, ms,
        )


async def touch_poll(tenant_id) -> None:
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE tenants SET last_poll_at = now() WHERE id = $1", tenant_id,
        )


# --- games ----------------------------------------------------------------

async def has_game(tenant_id, game_id: str) -> bool:
    async with pool().acquire() as c:
        r = await c.fetchval(
            "SELECT 1 FROM games WHERE tenant_id=$1 AND game_id=$2",
            tenant_id, game_id,
        )
    return r is not None


async def save_game(
    tenant_id,
    game_id: str,
    created_at_ms: int,
    pgn: str,
    raw: dict,
    summary: str,
    feedback: str,
    sections: Optional[dict],
    key_moments: Optional[dict],
) -> None:
    async with pool().acquire() as c:
        await c.execute(
            """
            INSERT INTO games (tenant_id, game_id, created_at_ms, pgn, raw_json,
                               summary, feedback, sections, key_moments)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8::jsonb,$9::jsonb)
            ON CONFLICT (tenant_id, game_id) DO UPDATE SET
                pgn         = EXCLUDED.pgn,
                raw_json    = EXCLUDED.raw_json,
                summary     = EXCLUDED.summary,
                feedback    = EXCLUDED.feedback,
                sections    = EXCLUDED.sections,
                key_moments = EXCLUDED.key_moments
            """,
            tenant_id, game_id, created_at_ms, pgn, json.dumps(raw),
            summary, feedback,
            json.dumps(sections) if sections is not None else None,
            json.dumps(key_moments) if key_moments is not None else None,
        )


async def get_game(tenant_id, game_id: str) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            "SELECT * FROM games WHERE tenant_id=$1 AND game_id=$2",
            tenant_id, game_id,
        )
    if not r:
        return None
    d = dict(r)
    d["raw_json"]    = _maybe_json(d.get("raw_json"))
    d["sections"]    = _maybe_json(d.get("sections"))
    d["key_moments"] = _maybe_json(d.get("key_moments"))
    return d


async def get_last_game(tenant_id) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            SELECT * FROM games WHERE tenant_id=$1
            ORDER BY created_at_ms DESC LIMIT 1
            """,
            tenant_id,
        )
    if not r:
        return None
    d = dict(r)
    d["raw_json"]    = _maybe_json(d.get("raw_json"))
    d["sections"]    = _maybe_json(d.get("sections"))
    d["key_moments"] = _maybe_json(d.get("key_moments"))
    return d


async def get_game_by_thread(tenant_id, thread_id: int) -> Optional[dict]:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            SELECT * FROM games
            WHERE tenant_id=$1 AND thread_id=$2
            ORDER BY created_at_ms DESC LIMIT 1
            """,
            tenant_id, thread_id,
        )
    if not r:
        return None
    d = dict(r)
    d["raw_json"]    = _maybe_json(d.get("raw_json"))
    d["sections"]    = _maybe_json(d.get("sections"))
    d["key_moments"] = _maybe_json(d.get("key_moments"))
    return d


async def set_thread_id(tenant_id, game_id: str, thread_id: int) -> None:
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE games SET thread_id=$3 WHERE tenant_id=$1 AND game_id=$2",
            tenant_id, game_id, int(thread_id),
        )


async def update_key_moments(tenant_id, game_id: str, key_moments: dict) -> None:
    async with pool().acquire() as c:
        await c.execute(
            "UPDATE games SET key_moments=$3::jsonb WHERE tenant_id=$1 AND game_id=$2",
            tenant_id, game_id, json.dumps(key_moments),
        )


# --- quotas ---------------------------------------------------------------

async def consume_game_quota(tenant_id, day: dt.date, cap: int) -> bool:
    """Atomically increment today's analyzed-games counter, return True if under cap."""
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            INSERT INTO usage_daily (tenant_id, day, games_analyzed)
            VALUES ($1, $2, 1)
            ON CONFLICT (tenant_id, day) DO UPDATE
                SET games_analyzed = usage_daily.games_analyzed + 1
            RETURNING games_analyzed
            """,
            tenant_id, day,
        )
    return int(r["games_analyzed"]) <= cap


async def games_used_today(tenant_id, day: dt.date) -> int:
    async with pool().acquire() as c:
        r = await c.fetchval(
            "SELECT games_analyzed FROM usage_daily WHERE tenant_id=$1 AND day=$2",
            tenant_id, day,
        )
    return int(r or 0)


async def consume_ask_quota(tenant_id, game_id: str, cap: int) -> bool:
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            INSERT INTO ask_usage (tenant_id, game_id, ask_count)
            VALUES ($1, $2, 1)
            ON CONFLICT (tenant_id, game_id) DO UPDATE
                SET ask_count = ask_usage.ask_count + 1
            RETURNING ask_count
            """,
            tenant_id, game_id,
        )
    return int(r["ask_count"]) <= cap


# --- pending installs (Stripe → Discord handoff) ---------------------------

async def create_pending_install(stripe_customer_id: str,
                                 stripe_subscription_id: Optional[str],
                                 install_email: Optional[str],
                                 ttl_minutes: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=ttl_minutes)
    async with pool().acquire() as c:
        await c.execute(
            """
            INSERT INTO pending_installs (token, stripe_customer_id, stripe_subscription_id,
                                          install_email, expires_at)
            VALUES ($1, $2, $3, $4, $5)
            """,
            token, stripe_customer_id, stripe_subscription_id, install_email, expires_at,
        )
    return token


async def consume_pending_install(token: str) -> Optional[dict]:
    """Single-use; returns the row and marks it consumed, or None if invalid/expired/used."""
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            UPDATE pending_installs
               SET consumed_at = now()
             WHERE token = $1
               AND consumed_at IS NULL
               AND expires_at > now()
             RETURNING *
            """,
            token,
        )
    return _row_to_dict(r)


async def peek_pending_install(token: str) -> Optional[dict]:
    """Read without consuming (for the /connect landing page)."""
    async with pool().acquire() as c:
        r = await c.fetchrow(
            """
            SELECT * FROM pending_installs
             WHERE token = $1
               AND consumed_at IS NULL
               AND expires_at > now()
            """,
            token,
        )
    return _row_to_dict(r)


async def gc_pending_installs() -> int:
    async with pool().acquire() as c:
        r = await c.execute(
            "DELETE FROM pending_installs WHERE expires_at < now() - interval '1 day'"
        )
    # "DELETE N"
    try:
        return int(str(r).split()[-1])
    except Exception:
        return 0
