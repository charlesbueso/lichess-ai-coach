"""SaaS-wide environment configuration. Loaded once at startup."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


# --- DB ---
DATABASE_URL = _req("DATABASE_URL")  # e.g. postgresql://coach:pass@127.0.0.1:5432/coach

# --- Discord ---
DISCORD_TOKEN          = _req("DISCORD_TOKEN")
DISCORD_CLIENT_ID      = _req("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET  = _req("DISCORD_CLIENT_SECRET")
DISCORD_PUBLIC_KEY     = os.getenv("DISCORD_PUBLIC_KEY", "")  # only needed for HTTP interactions; gateway doesn't use it

# --- Lichess (shared token) ---
LICHESS_TOKEN  = os.getenv("LICHESS_TOKEN") or None
LICHESS_CONTACT = os.getenv("LICHESS_CONTACT", "voxcentra@gmail.com")

# --- LLM (Groq) ---
GROQ_API_KEY = _req("GROQ_API_KEY")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# --- Stripe ---
STRIPE_SECRET_KEY     = _req("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _req("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID       = _req("STRIPE_PRICE_ID")  # price_xxx for the $5/mo plan

# --- Web app ---
BASE_URL       = _req("BASE_URL").rstrip("/")  # https://chesscoach.gg
SESSION_SECRET = _req("SESSION_SECRET")        # any 32+ char random string
HTTP_HOST      = os.getenv("HTTP_HOST", "127.0.0.1")
HTTP_PORT      = int(os.getenv("HTTP_PORT", "8000"))

# --- Limits ---
GAMES_PER_DAY_PER_TENANT = int(os.getenv("GAMES_PER_DAY_PER_TENANT", "20"))
ASKS_PER_GAME            = int(os.getenv("ASKS_PER_GAME", "10"))
POLL_INTERVAL_MINUTES    = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))

# --- Observability ---
SENTRY_DSN   = os.getenv("SENTRY_DSN") or None
POSTHOG_KEY  = os.getenv("POSTHOG_KEY") or None
POSTHOG_HOST = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# --- Misc ---
APP_NAME    = os.getenv("APP_NAME", "Lichess AI Coach")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "voxcentra@gmail.com")


# --- Discord OAuth ---
def install_url(state: str) -> str:
    """Build the Discord 'Add to Server' OAuth URL."""
    from urllib.parse import urlencode
    qs = urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "scope": "bot applications.commands",
        "permissions": "274877991936",  # View Channel + Send Messages + Embed Links + Attach Files + Create Public Threads + Send Messages In Threads + Use Slash Commands
        "redirect_uri": f"{BASE_URL}/discord/callback",
        "response_type": "code",
        "state": state,
    })
    return f"https://discord.com/oauth2/authorize?{qs}"


def stripe_success_url() -> str:
    return f"{BASE_URL}/connect?session_id={{CHECKOUT_SESSION_ID}}"


def stripe_cancel_url() -> str:
    return f"{BASE_URL}/?canceled=1"
