"""Chess-API.com client — remote Stockfish 18 analysis (free tier).

POST https://chess-api.com/v1 with a FEN and get back the best move,
eval, continuation, etc. No API key required for the free tier.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

log = logging.getLogger("coach.engine")

CHESS_API_URL = "https://chess-api.com/v1"
_FREE_MAX_DEPTH = 18
_FREE_MAX_THINK_MS = 100
_REQUEST_TIMEOUT_S = 25
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 3.0)  # waits before attempts 2 and 3


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

    Retries on transient failures (timeouts, 5xx, network errors) so a
    single flaky chess-api response doesn't blow up the whole game
    analysis.
    """
    payload = {
        "fen": fen,
        "depth": min(depth, _FREE_MAX_DEPTH),
        "maxThinkingTime": min(think_ms, _FREE_MAX_THINK_MS),
        "variants": 1,
    }

    last_exc: Optional[BaseException] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with session.post(
                CHESS_API_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S),
            ) as r:
                if r.status == 429:
                    log.warning("chess-api rate limited (429)")
                    return None
                if r.status >= 500:
                    body = ""
                    try:
                        body = (await r.text())[:200]
                    except Exception:
                        pass
                    log.warning(
                        "chess-api %s (attempt %d/%d) body=%s",
                        r.status, attempt, _MAX_ATTEMPTS, body,
                    )
                    # treat as transient -> retry
                    raise aiohttp.ClientResponseError(
                        request_info=r.request_info,
                        history=r.history,
                        status=r.status,
                        message=f"chess-api {r.status}",
                    )
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

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_exc = e
            if attempt < _MAX_ATTEMPTS:
                wait = _RETRY_BACKOFF_S[attempt - 1]
                log.warning(
                    "chess-api transient failure (attempt %d/%d) for fen=%s: "
                    "%s — retrying in %.1fs",
                    attempt, _MAX_ATTEMPTS, fen[:40], type(e).__name__, wait,
                )
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    raise
                continue
            log.error(
                "chess-api fetch failed for fen=%s after %d attempts: %s",
                fen[:40], _MAX_ATTEMPTS, type(e).__name__,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("chess-api fetch failed for fen=%s", fen[:40])
            return None

    if last_exc is not None:
        log.warning("chess-api gave up for fen=%s (%s)", fen[:40], type(last_exc).__name__)
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
