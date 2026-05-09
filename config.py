"""Configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


LICHESS_USERNAME = _req("LICHESS_USERNAME")
LICHESS_TOKEN = os.getenv("LICHESS_TOKEN") or None

GROQ_API_KEY = _req("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Optional comma-separated fallback list. Empty => use built-in defaults.
GROQ_MODELS = os.getenv("GROQ_MODELS", "")

DISCORD_TOKEN = _req("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(_req("DISCORD_CHANNEL_ID"))

POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
WEEKLY_DAY = int(os.getenv("WEEKLY_DAY", "6"))   # 0=Mon..6=Sun
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "18"))

DB_PATH = os.getenv("DB_PATH", "data.db")
