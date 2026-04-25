"""Chess-API.com client — remote Stockfish 18 analysis (free tier).

POST https://chess-api.com/v1 with a FEN and get back the best move,
eval, continuation, etc. No API key required for the free tier.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp

log = logging.getLogger("coach.engine")

CHESS_API_URL = "https://chess-api.com/v1"
_FREE_MAX_DEPTH = 18
_FREE_MAX_THINK_MS = 100


async def best_move(
    session: aiohttp.ClientSession,
    fen: str,
    depth: int = 14,
    think_ms: int = 100,
) -> Optional[dict]:
    """Query chess-api.com for the best move in a position.

    Returns a dict (the 'bestmove' response) with keys such as:
        san, eval, mate, continuationArr, text, depth, winChance
    Returns None on any failure so callers can degrade gracefully.
    """
    payload = {
        "fen": fen,
        "depth": min(depth, _FREE_MAX_DEPTH),
        "maxThinkingTime": min(think_ms, _FREE_MAX_THINK_MS),
        "variants": 1,
    }
    try:
        async with session.post(
            CHESS_API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status == 429:
                log.warning("chess-api rate limited (429)")
                return None
            if r.status != 200:
                body = ""
                try:
                    body = (await r.text())[:200]
                except Exception:
                    pass
                log.warning("chess-api %s body=%s", r.status, body)
                return None

            data = await r.json(content_type=None)

            # Response may be a list (streaming-style) or a single dict.
            if isinstance(data, list):
                # Prefer the 'bestmove' type entry; fall back to last item.
                best = next(
                    (item for item in reversed(data) if isinstance(item, dict) and item.get("type") == "bestmove"),
                    None,
                ) or (data[-1] if data else None)
                return best
            return data if isinstance(data, dict) else None

    except Exception:
        log.exception("chess-api fetch failed for fen=%s", fen[:40])
        return None


def format_result(result: Optional[dict]) -> str:
    """Human-readable one-liner for logging / fallback text."""
    if not result:
        return "(no engine result)"
    san = result.get("san") or result.get("move") or "?"
    ev = result.get("eval")
    mate = result.get("mate")
    depth = result.get("depth", "?")
    if mate is not None:
        return f"Best: {san} (#{mate}) depth {depth}"
    if ev is not None:
        sign = "+" if float(ev) > 0 else ""
        return f"Best: {san} ({sign}{ev:.2f}) depth {depth}"
    return f"Best: {san} depth {depth}"
