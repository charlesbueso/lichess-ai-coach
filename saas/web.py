"""FastAPI web app: landing, Stripe checkout, Discord OAuth callback, webhooks."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saas import app_config, billing, db
from saas.rate_limit import (
    CHECKOUT_PER_IP,
    RECOVER_PER_EMAIL,
    RECOVER_PER_IP,
)

log = logging.getLogger("coach.web")

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title=app_config.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

if (ROOT / "static").exists():
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _ctx(request: Request, **extra) -> dict:
    # Canonical absolute URL for this request (path only, no query) — used for
    # <link rel="canonical"> and og:url so crawlers + LLMs see one URL per page.
    canonical_url = app_config.BASE_URL + request.url.path
    return {
        "request": request,
        "app_name": app_config.APP_NAME,
        "base_url": app_config.BASE_URL,
        "canonical_url": canonical_url,
        "support_email": app_config.SUPPORT_EMAIL,
        "posthog_key": app_config.POSTHOG_KEY,
        "posthog_host": app_config.POSTHOG_HOST,
        "ga_id": app_config.GA_ID,
        **extra,
    }


def _client_ip(request: Request) -> str:
    """Resolve the client IP, honoring Caddy's X-Forwarded-For / X-Real-IP."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First entry is the original client per RFC 7239.
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


# ---------------- public pages ----------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", _ctx(request))


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", _ctx(request))


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html", _ctx(request))


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------- SEO / LLMO ----------------

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /checkout\n"
        "Disallow: /connect\n"
        "Disallow: /discord/\n"
        "Disallow: /stripe/\n"
        "Disallow: /recover\n"
        f"Sitemap: {app_config.BASE_URL}/sitemap.xml\n"
    )
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/sitemap.xml")
async def sitemap_xml():
    base = app_config.BASE_URL
    urls = [
        (f"{base}/",        "1.0", "weekly"),
        (f"{base}/privacy", "0.3", "yearly"),
        (f"{base}/terms",   "0.3", "yearly"),
    ]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, prio, freq in urls:
        parts.append(
            f"<url><loc>{loc}</loc><changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
    parts.append("</urlset>")
    body = "".join(parts)
    return PlainTextResponse(
        body,
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    """LLM-friendly site summary (https://llmstxt.org/)."""
    base = app_config.BASE_URL
    body = (
        f"# {app_config.APP_NAME}\n\n"
        "> AI chess coach for Discord. Chess Brain dissects every Lichess "
        "game you play with Stockfish 18 and delivers engine-verified "
        "analysis — blunders, opening missteps, endgame breakdowns — "
        "straight to a Discord channel you control. $5/month, 7-day free "
        "trial, cancel any time.\n\n"
        "## What it does\n\n"
        "- Polls your public Lichess games every 10 minutes (no manual uploads).\n"
        "- Runs Stockfish 18 on every finished game to find the critical moment.\n"
        "- Posts a blog-style coached breakdown to your Discord channel.\n"
        "- Lets you ask follow-up questions with `/ask` — full game stays in context.\n"
        "- Renders the exact flagged position with `/board game_id move`.\n\n"
        "## Pricing\n\n"
        "- Single tier: $5 USD / month.\n"
        "- 7-day free trial, card required, cancel any time via `/billing`.\n"
        "- Up to 20 games analyzed per day. One Discord server per subscription.\n\n"
        "## Setup\n\n"
        "1. Subscribe via Stripe Checkout.\n"
        "2. Click Add to Discord on the success page and pick your server.\n"
        "3. Run `/setup lichess:<username> channel:#chess` in your server.\n"
        "4. Play on Lichess — your next game arrives as a coached post.\n\n"
        "## Key facts\n\n"
        "- Engine: Stockfish 18.\n"
        "- Source: read-only Lichess public games API (allowed for commercial use).\n"
        "- Only analyzes completed games — never assists during play, not cheating.\n"
        "- Open source: https://github.com/charlesbueso/lichess-ai-coach\n"
        "- Not affiliated with or endorsed by Lichess.\n\n"
        "## Pages\n\n"
        f"- [Home]({base}/): product overview, features, FAQ, pricing.\n"
        f"- [Privacy]({base}/privacy): what data is stored and why.\n"
        f"- [Terms]({base}/terms): terms of service.\n\n"
        f"## Contact\n\n- Support: {app_config.SUPPORT_EMAIL}\n"
    )
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=3600"})


# ---------------- checkout ----------------

@app.post("/checkout")
async def checkout_post(request: Request):
    return await _checkout(request)


@app.get("/checkout")
async def checkout_get(request: Request):
    return await _checkout(request)


async def _checkout(request: Request):
    if not await CHECKOUT_PER_IP.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many checkout attempts. Try again later.")
    try:
        url = await billing.create_checkout_session()
    except Exception:
        log.exception("Stripe checkout create failed")
        raise HTTPException(status_code=502, detail="Payments temporarily unavailable.")
    return RedirectResponse(url, status_code=303)


# ---------------- /connect (Stripe success → Discord install) ----------------

@app.get("/connect", response_class=HTMLResponse)
async def connect(request: Request, session_id: Optional[str] = None):
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    try:
        sess = await billing.retrieve_session(session_id)
    except Exception:
        log.exception("Stripe session retrieve failed for %s", session_id)
        raise HTTPException(status_code=400, detail="Invalid session")

    if sess.get("payment_status") not in ("paid", "no_payment_required"):
        # Still mid-checkout? Send back to landing.
        return RedirectResponse(app_config.BASE_URL + "/?pending=1", status_code=303)

    customer_id = sess.get("customer")
    sub_id      = sess.get("subscription")
    email       = (sess.get("customer_details") or {}).get("email")

    if not customer_id:
        raise HTTPException(status_code=500, detail="Missing customer on session")

    # Create a one-time install token tied to the Stripe customer.
    token = await db.create_pending_install(
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        install_email=email,
    )
    install = app_config.install_url(state=token)

    # The Stripe `checkout.session.completed` webhook handler is responsible
    # for emailing the install link. We deliberately don't send a second copy
    # here — that would double-mail every customer.

    return templates.TemplateResponse(
        request, "connect.html",
        _ctx(request, install_url=install, email=email),
    )


# ---------------- /recover (lost the install link) ----------------

@app.get("/recover", response_class=HTMLResponse)
async def recover_get(request: Request):
    return templates.TemplateResponse(request, "recover.html", _ctx(request))


@app.post("/recover", response_class=HTMLResponse)
async def recover_post(request: Request, email: str = Form(...)):
    # Always show the same confirmation, regardless of whether we found a
    # matching customer — prevents email enumeration.
    addr = (email or "").strip().lower()

    # Per-IP limit: protects Stripe API quota and stops scrapers cold.
    ip = _client_ip(request)
    if not await RECOVER_PER_IP.allow(ip):
        log.warning("recover: per-IP rate limit hit for %s", ip)
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    # Per-email limit: protects real customers from being mail-bombed via
    # repeated submissions. Applied BEFORE Stripe lookup so we don't even
    # confirm the address exists when the limit is hit.
    email_allowed = bool(addr) and await RECOVER_PER_EMAIL.allow(f"email:{addr}")

    if addr and email_allowed:
        try:
            session_id = await billing.find_latest_session_id_for_email(addr)
        except Exception:
            log.exception("Stripe lookup failed for %s during /recover", addr)
            session_id = None
        if session_id:
            from saas import email as mailer
            try:
                await mailer.send_install_link(addr, session_id)
            except Exception:
                log.exception("send_install_link failed in /recover for %s", addr)
    return templates.TemplateResponse(
        request, "recover.html",
        _ctx(request, sent=True, email=addr),
    )


# ---------------- Discord OAuth callback ----------------

@app.get("/discord/callback", response_class=HTMLResponse)
async def discord_callback(request: Request, code: Optional[str] = None,
                           state: Optional[str] = None,
                           guild_id: Optional[str] = None,
                           error: Optional[str] = None):
    if error:
        return templates.TemplateResponse(
            request, "error.html",
            _ctx(request, message=f"Discord said: {error}. You can retry from the email we sent."),
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    pending = await db.consume_pending_install(state)
    if not pending:
        return templates.TemplateResponse(
            request, "error.html",
            _ctx(request, message="This install link has expired or was already used. "
                                  "Open the original email link, or contact support."),
            status_code=410,
        )

    # Exchange the code for an access token to (a) verify the install and
    # (b) recover guild_id reliably (Discord includes it in the redirect query
    # for bot installs, but we double-check from the API).
    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                "https://discord.com/api/v10/oauth2/token",
                data={
                    "client_id": app_config.DISCORD_CLIENT_ID,
                    "client_secret": app_config.DISCORD_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{app_config.BASE_URL}/discord/callback",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            ) as r:
                token_data = await r.json()
                if r.status != 200:
                    log.warning("Discord token exchange failed: %s", token_data)
                    raise HTTPException(status_code=400, detail="Discord token exchange failed")
        except aiohttp.ClientError:
            log.exception("Discord token exchange error")
            raise HTTPException(status_code=502, detail="Discord unreachable")

    discord_guild = (token_data.get("guild") or {})
    resolved_guild_id = (
        int(guild_id) if guild_id else (int(discord_guild["id"]) if discord_guild.get("id") else None)
    )
    if not resolved_guild_id:
        return templates.TemplateResponse(
            request, "error.html",
            _ctx(request, message="Couldn't determine which Discord server you installed into. "
                                  "Try again from the success page."),
            status_code=400,
        )

    # Best-effort: identify the installer user.
    installer_user_id = 0
    access_token = token_data.get("access_token")
    if access_token:
        try:
            async with aiohttp.ClientSession() as s2:
                async with s2.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
                ) as r:
                    if r.status == 200:
                        u = await r.json()
                        installer_user_id = int(u.get("id") or 0)
        except Exception:
            log.exception("Discord users/@me fetch failed")

    tenant = await db.bind_discord_install(
        stripe_customer_id=pending["stripe_customer_id"],
        guild_id=resolved_guild_id,
        installer_user_id=installer_user_id,
    )
    if not tenant:
        return templates.TemplateResponse(
            request, "error.html",
            _ctx(request, message="Could not bind the install to your subscription. Please contact support."),
            status_code=500,
        )

    # DM the installer with setup instructions (best-effort).
    if installer_user_id:
        await _dm_setup_instructions(installer_user_id, tenant)

    return templates.TemplateResponse(
        request, "success.html",
        _ctx(request, guild_id=resolved_guild_id),
    )


async def _dm_setup_instructions(user_id: int, tenant: dict) -> None:
    """Send a DM to the installer with the next step."""
    from saas.bot import bot as _bot
    try:
        user = _bot.get_user(user_id) or await _bot.fetch_user(user_id)
        await user.send(
            f"Welcome to {app_config.APP_NAME}! 🎉\n\n"
            f"Your subscription is active (status: **{tenant['status']}**).\n\n"
            f"**Next step — connect your Lichess account**\n"
            f"In your server, run this slash command:\n"
            f"```\n/setup lichess:<your_lichess_username> channel:#some-channel\n```\n"
            f"_e.g._ `/setup lichess:DrNykterstein channel:#chess-coach`\n\n"
            f"Tip: when you start typing `/setup` Discord will autocomplete; "
            f"`channel:` accepts a `#channel` mention.\n\n"
            f"After that, **just play games on Lichess** — finished games are "
            f"analyzed automatically every "
            f"{app_config.POLL_INTERVAL_MINUTES} minutes and posted in the "
            f"channel you picked.\n\n"
            f"Useful commands once set up:\n"
            f"• `/help` — full guide\n"
            f"• `/game` — replay your latest analysis\n"
            f"• `/ask question:...` — Q&A inside an analysis thread\n"
            f"• `/billing` — manage your subscription\n\n"
            f"Questions? {app_config.SUPPORT_EMAIL}"
        )
    except Exception:
        log.exception("Could not DM installer %s", user_id)


# ---------------- Stripe webhook ----------------

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    sig = request.headers.get("stripe-signature", "")
    payload = await request.body()
    try:
        event = billing.verify_webhook(payload, sig)
    except Exception:
        log.exception("Stripe webhook verify failed")
        return PlainTextResponse("bad signature", status_code=400)
    try:
        await billing.handle_event(event)
    except Exception:
        log.exception("Stripe webhook handler crashed for event %s", event.get("type"))
        # Return 500 so Stripe retries.
        return PlainTextResponse("error", status_code=500)
    return JSONResponse({"received": True})
