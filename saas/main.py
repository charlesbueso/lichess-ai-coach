"""SaaS entry point: starts Postgres pool, FastAPI, Discord bot, poll loop — all on one asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import random
import signal

import aiohttp
import uvicorn

from saas import app_config, db
from saas.bot import bot, set_http
from saas.coach import process_tenant
from saas.web import app as fastapi_app

log = logging.getLogger("coach.main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _sentry_before_send(event, hint):
    # Drop benign asyncio.CancelledError noise from uvicorn lifespan shutdown.
    exc_info = hint.get("exc_info") if hint else None
    if exc_info:
        exc_type = exc_info[0]
        if exc_type is not None and issubclass(exc_type, (asyncio.CancelledError, GeneratorExit)):
            return None
    if event.get("logger") in ("uvicorn.error", "uvicorn.lifespan"):
        # Lifespan cancellation surfaces here as logger=uvicorn.error with a CancelledError trace.
        values = (event.get("exception") or {}).get("values") or []
        for v in values:
            if v.get("type") in ("CancelledError", "GeneratorExit"):
                return None
    return event


def _init_sentry():
    if not app_config.SENTRY_DSN:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=app_config.SENTRY_DSN,
            traces_sample_rate=0.1,
            profiles_sample_rate=0.0,
            before_send=_sentry_before_send,
        )
        log.info("Sentry initialised")
    except Exception:
        log.exception("Sentry init failed")


def _init_posthog():
    if not app_config.POSTHOG_KEY:
        return
    try:
        import posthog
        posthog.api_key = app_config.POSTHOG_KEY
        posthog.host    = app_config.POSTHOG_HOST
        log.info("PostHog initialised")
    except Exception:
        log.exception("PostHog init failed")


async def poll_loop(http: aiohttp.ClientSession):
    await bot.wait_until_ready()
    interval = max(1, app_config.POLL_INTERVAL_MINUTES) * 60
    while not bot.is_closed():
        try:
            tenants = await db.get_active_tenants()
            log.info("Poll cycle: %d active tenants", len(tenants))
            random.shuffle(tenants)  # cheap jitter so we don't hammer Lichess in id-order
            for t in tenants:
                try:
                    await process_tenant(http, bot, t)
                except Exception:
                    log.exception("[t=%s] process_tenant crashed", t["id"])
        except Exception:
            log.exception("poll_loop top-level error")
        # Periodic GC for expired install tokens.
        try:
            await db.gc_pending_installs()
        except Exception:
            log.exception("gc_pending_installs failed")
        await asyncio.sleep(interval)


async def run_uvicorn():
    cfg = uvicorn.Config(
        fastapi_app,
        host=app_config.HTTP_HOST,
        port=app_config.HTTP_PORT,
        log_level="info",
        loop="asyncio",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    await server.serve()


async def amain():
    _init_sentry()
    _init_posthog()
    await db.init_pool()

    http = aiohttp.ClientSession(
        headers={
            "User-Agent": (
                f"lichess-ai-coach/1.0 (+{app_config.BASE_URL}; {app_config.LICHESS_CONTACT})"
            ),
        }
    )
    set_http(http)

    bot_task    = asyncio.create_task(bot.start(app_config.DISCORD_TOKEN), name="bot")
    web_task    = asyncio.create_task(run_uvicorn(),                       name="web")
    poller_task = asyncio.create_task(poll_loop(http),                     name="poller")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows

    done_or_stop = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        [bot_task, web_task, poller_task, done_or_stop],
        return_when=asyncio.FIRST_COMPLETED,
    )

    log.info("Shutting down…")
    for t in (bot_task, web_task, poller_task):
        if not t.done():
            t.cancel()
    try:
        await bot.close()
    except Exception:
        pass
    await http.close()
    await db.close_pool()
    for t in (bot_task, web_task, poller_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
