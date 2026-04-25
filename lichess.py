"""Lichess API client. Async, minimal."""
import aiohttp
import chess
import json
import logging
from typing import AsyncIterator, Optional

import config

log = logging.getLogger("coach.lichess")

BASE = "https://lichess.org"


def _headers() -> dict:
    h = {"Accept": "application/x-ndjson"}
    if config.LICHESS_TOKEN:
        h["Authorization"] = f"Bearer {config.LICHESS_TOKEN}"
    return h


async def fetch_games(
    session: aiohttp.ClientSession,
    username: str,
    since_ms: Optional[int] = None,
    max_games: Optional[int] = None,
) -> list:
    """Fetch games for username, newest first.

    Uses the Lichess export endpoint with built-in analysis included
    (evals, opening, accuracy). Returns a list of dicts (not a generator,
    to keep things simple — game volumes are small).
    """
    params = {
        "evals": "true",
        "opening": "true",
        "accuracy": "true",
        "clocks": "false",
        "moves": "true",
        "pgnInJson": "true",
    }
    if since_ms is not None:
        params["since"] = str(since_ms)
    if max_games is not None:
        params["max"] = str(max_games)

    url = f"{BASE}/api/games/user/{username}"
    games = []
    async with session.get(url, headers=_headers(), params=params, timeout=60) as r:
        r.raise_for_status()
        async for raw_line in r.content:
            line = raw_line.strip()
            if not line:
                continue
            try:
                games.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return games


def extract_summary_fields(g: dict) -> dict:
    """Return a small dict suitable for sending to the LLM."""
    players = g.get("players", {})
    white = players.get("white", {})
    black = players.get("black", {})

    def _name(p):
        u = p.get("user") or {}
        return u.get("name") or p.get("aiLevel") and f"AI lvl {p['aiLevel']}" or "anon"

    accuracy = {
        "white": white.get("analysis", {}).get("accuracy") if white.get("analysis") else None,
        "black": black.get("analysis", {}).get("accuracy") if black.get("analysis") else None,
    }
    judgement = {
        "white": white.get("analysis"),
        "black": black.get("analysis"),
    }
    return {
        "id": g.get("id"),
        "speed": g.get("speed"),
        "perf": g.get("perf"),
        "rated": g.get("rated"),
        "status": g.get("status"),
        "winner": g.get("winner"),
        "white": {"name": _name(white), "rating": white.get("rating")},
        "black": {"name": _name(black), "rating": black.get("rating")},
        "opening": g.get("opening"),
        "moves": g.get("moves"),
        "accuracy": accuracy,
        "judgement": judgement,
        "ply_analysis": g.get("analysis"),  # per-ply [{eval|mate, judgment?}, ...]
        "clock": g.get("clock"),
        "createdAt": g.get("createdAt"),
    }


def extract_key_mistakes(g: dict, username: str) -> list:
    """Pull blunder/mistake/inaccuracy info for the user's side from Lichess analysis."""
    players = g.get("players", {})
    side = None
    for color in ("white", "black"):
        u = (players.get(color, {}).get("user") or {}).get("name", "")
        if u.lower() == username.lower():
            side = color
            break
    if not side:
        return []
    analysis = (players.get(side, {}) or {}).get("analysis") or {}
    return [
        {"type": k, "count": analysis.get(k, 0)}
        for k in ("inaccuracy", "mistake", "blunder")
        if analysis.get(k)
    ]


def user_color(summary: dict, username: str):
    """Return 'white' / 'black' / None for the username inside an extracted summary."""
    for c in ("white", "black"):
        name = (summary.get(c) or {}).get("name", "")
        if name and name.lower() == username.lower():
            return c
    return None


def build_move_table(summary: dict, max_rows: int = 80) -> list:
    """Produce a compact ground-truth table of moves with engine info.

    Each entry: {n, color, san, eval, judgment, comment, fen}
      - n        : full move number (1-based)
      - color    : 'white' | 'black' (who played this half-move)
      - san      : SAN of the move played (e.g. 'Nf3', 'O-O')
      - eval     : float (pawns) or '#N' string for mate, or None
      - judgment : 'Inaccuracy' | 'Mistake' | 'Blunder' | None
      - comment  : Lichess analysis comment (often contains the best move)
      - fen      : FEN of the position BEFORE this move was played
    """
    moves_str = summary.get("moves") or ""
    sans = moves_str.split()
    plies = summary.get("ply_analysis") or []

    # Walk a real chess board to compute the FEN before each move.
    board = chess.Board()
    fen_before: list[str | None] = []
    for san in sans:
        fen_before.append(board.fen())
        try:
            board.push_san(san)
        except Exception:
            log.warning("build_move_table: could not push SAN %r; remaining FENs will be guesses", san)
            # Fill rest with None so indices still align
            fen_before.extend([None] * (len(sans) - len(fen_before)))
            break

    rows = []
    for i, san in enumerate(sans):
        color = "white" if i % 2 == 0 else "black"
        full_n = (i // 2) + 1
        a = plies[i] if i < len(plies) else {}
        ev = None
        if a:
            if "eval" in a:
                ev = round(a["eval"] / 100.0, 2)
            elif "mate" in a:
                ev = f"#{a['mate']}"
        judg = (a or {}).get("judgment") or {}
        rows.append({
            "n": full_n,
            "color": color,
            "san": san,
            "eval": ev,
            "judgment": judg.get("name"),
            "comment": judg.get("comment"),
            "fen": fen_before[i] if i < len(fen_before) else None,
        })
    # Trim to keep the prompt small — keep first/last + all judged moves
    if len(rows) > max_rows:
        kept = set(range(min(20, len(rows))))                     # opening
        kept.update(range(max(0, len(rows) - 20), len(rows)))     # endgame
        for idx, r in enumerate(rows):
            if r["judgment"]:
                kept.add(idx)
        rows = [r for idx, r in enumerate(rows) if idx in kept]
    return rows
