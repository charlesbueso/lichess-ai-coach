"""Engine analysis facade.

Primary path: local Stockfish pool (`engine_pool`).
Fallback path: chess-api.com remote (`engine_remote`) — used only when the
local pool isn't available (binary missing, all engines crashed at startup,
etc.) and `ENGINE_USE_REMOTE_FALLBACK` is truthy (default: true).

The `best_move(session, fen, depth, think_ms)` signature and return shape
are preserved so callers in `board.py` and `llm.py` don't need to change.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

import engine_pool
import engine_remote

log = logging.getLogger("coach.engine")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


_USE_REMOTE_FALLBACK = _env_bool("ENGINE_USE_REMOTE_FALLBACK", True)


async def best_move(
    session: aiohttp.ClientSession,
    fen: str,
    depth: int = 14,
    think_ms: int = 100,
) -> Optional[dict]:
    """Return best-move analysis for `fen` or None on failure.

    Returns a dict with chess-api.com-compatible keys (`san`, `lan`, `move`,
    `eval` in pawns white-positive, `mate`, `depth`, `continuationArr`,
    `text`). `session` is accepted for backwards compatibility with the
    remote fallback path; the local pool ignores it.
    """
    if engine_pool.is_available():
        result = await engine_pool.analyse(fen, depth=depth, think_ms=think_ms)
        if result is not None:
            return result
        if not _USE_REMOTE_FALLBACK:
            return None
        log.warning("local pool returned None for fen=%s; trying remote", fen[:40])
        return await engine_remote.best_move(session, fen, depth=depth, think_ms=think_ms)

    if _USE_REMOTE_FALLBACK:
        return await engine_remote.best_move(session, fen, depth=depth, think_ms=think_ms)
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
