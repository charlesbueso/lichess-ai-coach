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
LICHESS_CONTACT = os.getenv("LICHESS_CONTACT", "chessbrain.coach@gmail.com")

# --- LLM (Groq) ---
GROQ_API_KEY = _req("GROQ_API_KEY")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODELS  = os.getenv("GROQ_MODELS", "")

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
# Google Analytics 4 Measurement ID (e.g. G-XXXXXXXXXX). Set to empty to disable.
GA_ID        = os.getenv("GA_ID", "G-T77G930D0G") or None

# --- Misc ---
APP_NAME    = os.getenv("APP_NAME", "Lichess AI Coach")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "chessbrain.coach@gmail.com")

# --- Email (Resend) ---
# If RESEND_API_KEY is unset, all email sends become no-ops (handy for dev).
RESEND_API_KEY = os.getenv("RESEND_API_KEY") or None
EMAIL_FROM     = os.getenv("EMAIL_FROM", f"{APP_NAME} <noreply@matra.live>")
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", SUPPORT_EMAIL)


# --- Discord OAuth ---
# Permissions bitfield for the install URL. Discord grants these to a server-
# wide Chess Brain role at install time, so users don't need to grant per-channel
# unless they install into a private channel with overrides.
#
# Includes:
#   View Channel             (1 << 10)  =          1024
#   Send Messages            (1 << 11)  =          2048
#   Embed Links              (1 << 14)  =         16384
#   Attach Files             (1 << 15)  =         32768   ← required for GIFs / boards
#   Read Message History     (1 << 16)  =         65536
#   Use Application Commands (1 << 31)  =    2147483648
#   Create Public Threads    (1 << 35)  =   34359738368   ← required for /game blog threads
#   Send Messages in Threads (1 << 38)  =  274877906944
# Total                                 =  311385279488
INSTALL_PERMISSIONS = "311385279488"


def install_url(state: str) -> str:
    """Build the Discord 'Add to Server' OAuth URL."""
    from urllib.parse import urlencode
    qs = urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "scope": "bot applications.commands",
        "permissions": INSTALL_PERMISSIONS,
        "redirect_uri": f"{BASE_URL}/discord/callback",
        "response_type": "code",
        "state": state,
    })
    return f"https://discord.com/oauth2/authorize?{qs}"


def stripe_success_url() -> str:
    return f"{BASE_URL}/connect?session_id={{CHECKOUT_SESSION_ID}}"


def stripe_cancel_url() -> str:
    return f"{BASE_URL}/?canceled=1"
