"""Local Stockfish engine pool — bounded concurrency, crash recovery, LRU cache.

Public API (used by `engine.py`):

    await init_pool()                # call once at startup
    await close_pool()               # call once at shutdown
    is_available()                   # True if at least one engine is alive
    await analyse(fen, depth, think_ms) -> dict | None
    game_scan_semaphore              # asyncio.Semaphore for game-ingestion bursts

Design notes:
- Uses python-chess's async UCI API (`chess.engine.popen_uci`), which is
  already a transitive dependency via `chess`.
- A fixed-size `asyncio.Queue` holds the engines. Callers check one out,
  run a single `analyse`, return it.
- Each call is wrapped in `asyncio.wait_for` so a stuck engine slot can't
  deadlock the pool.
- On any engine error / timeout: kill the process, spawn a replacement,
  put the new one back in the queue. The caller gets one retry.
- LRU cache keyed by (fen, depth, think_ms). Coroutine-safe because all
  reads/writes happen on the same event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections import OrderedDict
from typing import Optional

import chess
import chess.engine

log = logging.getLogger("coach.engine.pool")


# ---- configuration (env-driven, no dependency on config.py or app_config.py) ----

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _resolve_stockfish_path() -> Optional[str]:
    explicit = os.getenv("STOCKFISH_PATH")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    # Common Ubuntu location first, then PATH lookup.
    for candidate in ("/usr/games/stockfish", "/usr/bin/stockfish"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("stockfish")


POOL_SIZE             = _env_int("STOCKFISH_POOL_SIZE", 2)
THREADS_PER_ENGINE    = _env_int("STOCKFISH_THREADS", 1)
HASH_MB               = _env_int("STOCKFISH_HASH_MB", 16)
CALL_TIMEOUT_S        = _env_float("STOCKFISH_CALL_TIMEOUT_S", 5.0)
DEFAULT_DEPTH         = _env_int("STOCKFISH_DEFAULT_DEPTH", 14)
DEFAULT_MOVETIME_MS   = _env_int("STOCKFISH_DEFAULT_MOVETIME_MS", 100)
MAX_CONCURRENT_GAMES  = _env_int("MAX_CONCURRENT_GAMES", 2)
CACHE_SIZE            = _env_int("ENGINE_CACHE_SIZE", 1024)


# ---- state ----

_queue: Optional[asyncio.Queue] = None  # of chess.engine.UciProtocol
_stockfish_path: Optional[str] = None
_init_lock = asyncio.Lock()
_initialised = False

_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_cache_hits = 0
_cache_misses = 0

# Semaphore for ingestion bursts (used by board.find_worst_user_move).
# Created lazily so importing this module on Python 3.10+ without a running
# loop doesn't blow up.
_game_scan_semaphore: Optional[asyncio.Semaphore] = None


def game_scan_semaphore() -> asyncio.Semaphore:
    """Return a process-wide semaphore limiting concurrent game-scan loops."""
    global _game_scan_semaphore
    if _game_scan_semaphore is None:
        _game_scan_semaphore = asyncio.Semaphore(MAX_CONCURRENT_GAMES)
    return _game_scan_semaphore


def is_available() -> bool:
    # "Available" means the pool was initialised and has at least one engine.
    # An empty queue is fine — it just means all engines are currently in use.
    return _initialised and _queue is not None and _queue.maxsize > 0


# ---- engine lifecycle ----

async def _spawn_engine() -> chess.engine.UciProtocol:
    assert _stockfish_path is not None
    transport, engine = await chess.engine.popen_uci(_stockfish_path)
    # Best-effort configure; some builds may reject unknown options.
    try:
        await engine.configure({"Threads": THREADS_PER_ENGINE, "Hash": HASH_MB})
    except Exception:
        log.warning("engine.configure failed; using defaults", exc_info=True)
    # Stash transport so we can kill the process forcibly on shutdown.
    engine._coach_transport = transport  # type: ignore[attr-defined]
    return engine


async def init_pool() -> bool:
    """Spawn the pool. Returns True if at least one engine started.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _stockfish_path, _queue, _initialised
    async with _init_lock:
        if _initialised:
            return _queue is not None and _queue.maxsize > 0

        _stockfish_path = _resolve_stockfish_path()
        if not _stockfish_path:
            log.warning(
                "Stockfish binary not found (set STOCKFISH_PATH or install the 'stockfish' "
                "package). Falling back to chess-api.com remote engine."
            )
            _initialised = True  # mark so we don't keep retrying
            return False

        log.info(
            "engine_pool: starting %d Stockfish engines at %s (Threads=%d, Hash=%d MB)",
            POOL_SIZE, _stockfish_path, THREADS_PER_ENGINE, HASH_MB,
        )
        q: asyncio.Queue = asyncio.Queue(maxsize=POOL_SIZE)
        started = 0
        for i in range(POOL_SIZE):
            try:
                eng = await _spawn_engine()
                await q.put(eng)
                started += 1
            except Exception:
                log.exception("engine_pool: failed to spawn engine #%d", i)

        if started == 0:
            log.error("engine_pool: NO engines started; falling back to remote.")
            _initialised = True
            return False

        _queue = q
        _initialised = True
        log.info("engine_pool: ready with %d engines", started)
        return True


async def close_pool() -> None:
    global _queue, _initialised
    if _queue is None:
        _initialised = False
        return
    log.info("engine_pool: shutting down")
    while not _queue.empty():
        eng = _queue.get_nowait()
        try:
            await asyncio.wait_for(eng.quit(), timeout=2.0)
        except Exception:
            try:
                t = getattr(eng, "_coach_transport", None)
                if t is not None:
                    t.close()
            except Exception:
                pass
    _queue = None
    _initialised = False


# ---- analysis ----

def _info_to_response(board: chess.Board, info: chess.engine.InfoDict) -> dict:
    """Translate a python-chess InfoDict into the chess-api response shape
    used by board.py and llm.py callers."""
    pv = info.get("pv") or []
    if not pv:
        return {}

    best_move = pv[0]
    try:
        san = board.san(best_move)
    except Exception:
        san = None

    score = info.get("score")
    eval_pawns: Optional[float] = None
    mate: Optional[int] = None
    if score is not None:
        # Re-orient to white's perspective so callers get a stable sign.
        white_score = score.white()
        if white_score.is_mate():
            mate = white_score.mate()
        else:
            cp = white_score.score(mate_score=100000)
            if cp is not None:
                eval_pawns = round(cp / 100.0, 2)

    cont_uci = [m.uci() for m in pv]  # includes the best move at index 0;
    # downstream callers re-prepend pv[0] only if it isn't already there.

    depth = info.get("depth", 0)
    if mate is not None:
        text = f"Best move: {san or best_move.uci()} (mate in {abs(mate)})"
    elif eval_pawns is not None:
        sign = "+" if eval_pawns >= 0 else ""
        text = f"Best move: {san or best_move.uci()} ({sign}{eval_pawns:.2f})"
    else:
        text = f"Best move: {san or best_move.uci()}"

    return {
        "san": san,
        "move": best_move.uci(),
        "lan": best_move.uci(),
        "continuationArr": cont_uci,
        "eval": eval_pawns,
        "mate": mate,
        "depth": depth,
        "text": text,
    }


def _cache_get(key: tuple) -> Optional[dict]:
    global _cache_hits
    if key in _cache:
        _cache.move_to_end(key)
        _cache_hits += 1
        return _cache[key]
    return None


def _cache_put(key: tuple, value: dict) -> None:
    global _cache_misses
    _cache_misses += 1
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


async def _checkout() -> chess.engine.UciProtocol:
    assert _queue is not None
    return await _queue.get()


async def _replace_dead_engine() -> None:
    """Spawn a fresh engine and put it back in the queue."""
    assert _queue is not None
    try:
        eng = await _spawn_engine()
        await _queue.put(eng)
        log.info("engine_pool: respawned a stockfish engine")
    except Exception:
        log.exception("engine_pool: failed to respawn engine; pool capacity reduced")


async def _checkin(eng: chess.engine.UciProtocol) -> None:
    assert _queue is not None
    await _queue.put(eng)


async def analyse(
    fen: str,
    depth: Optional[int] = None,
    think_ms: Optional[int] = None,
) -> Optional[dict]:
    """Analyse a position with the local engine pool. Returns chess-api-shaped dict or None."""
    if not _initialised or _queue is None:
        return None

    d = depth if depth is not None else DEFAULT_DEPTH
    t = think_ms if think_ms is not None else DEFAULT_MOVETIME_MS
    key = (fen, d, t)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        board = chess.Board(fen)
    except Exception:
        log.warning("engine_pool: invalid FEN: %s", fen[:80])
        return None

    # python-chess Limit: depth caps depth, time caps wall time (seconds).
    limit = chess.engine.Limit(depth=d, time=max(0.01, t / 1000.0))

    for attempt in (1, 2):
        eng = await _checkout()
        try:
            info = await asyncio.wait_for(
                eng.analyse(board, limit), timeout=CALL_TIMEOUT_S
            )
        except (asyncio.TimeoutError, chess.engine.EngineTerminatedError, chess.engine.EngineError) as e:
            log.warning(
                "engine_pool: engine failed (%s) attempt %d/2 for fen=%s",
                type(e).__name__, attempt, fen[:40],
            )
            try:
                t_ = getattr(eng, "_coach_transport", None)
                if t_ is not None:
                    t_.close()
            except Exception:
                pass
            await _replace_dead_engine()
            if attempt == 2:
                return None
            continue
        except asyncio.CancelledError:
            await _checkin(eng)
            raise
        except Exception:
            log.exception("engine_pool: unexpected analyse error")
            await _checkin(eng)
            return None
        else:
            await _checkin(eng)
            if isinstance(info, list):
                info = info[0] if info else {}
            response = _info_to_response(board, info)
            if not response:
                return None
            _cache_put(key, response)
            return response

    return None


def stats() -> dict:
    return {
        "available": _initialised and _queue is not None,
        "pool_size": POOL_SIZE,
        "queue_free": _queue.qsize() if _queue is not None else 0,
        "cache_size": len(_cache),
        "cache_hits": _cache_hits,
        "cache_misses": _cache_misses,
        "stockfish_path": _stockfish_path,
    }
