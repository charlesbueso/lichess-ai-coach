"""Board / position helpers: pick key moments, build FENs, fetch board images, build sparkline."""
import io
import logging
import re
from typing import Optional

import aiohttp
import chess
import chess.pgn

log = logging.getLogger("coach.board")

# Lichess board image endpoint — single-frame board GIF (no auth, public CDN).
BOARD_URL = "https://lichess1.org/export/fen.gif"
# Public animated full-game GIF endpoint (by game id).
FULL_GAME_GIF_URL = "https://lichess1.org/game/export/gif/{game_id}.gif"

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _last_move_uci(node: chess.pgn.GameNode) -> Optional[str]:
    mv = node.move
    return mv.uci() if mv else None


def _walk(game: chess.pgn.Game):
    """Yield (ply_index, board_after_move, last_move_uci, node)."""
    node = game
    ply = 0
    while node.variations:
        node = node.variation(0)
        ply += 1
        yield ply, node.board(), _last_move_uci(node), node


def parse_pgn(pgn_text: str) -> Optional[chess.pgn.Game]:
    try:
        return chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:
        log.exception("PGN parse failed")
        return None


# Lichess comments look like:
#   "Inaccuracy. Bd3 was best."
#   "Mistake. The position was equal after Nf6."
#   "Blunder. Checkmate is now unavoidable. Qxf7# was best."
_BEST_MOVE_RE = re.compile(
    r"\b([NBRQK]?[a-h]?[1-8]?x?[a-h][1-8](?:=[NBRQ])?[+#]?|O-O(?:-O)?)\b\s+was best",
    re.IGNORECASE,
)


def extract_best_san_from_comment(comment: Optional[str]) -> Optional[str]:
    """Pull a best-move SAN out of a Lichess judgment comment, if present."""
    if not comment:
        return None
    m = _BEST_MOVE_RE.search(comment)
    return m.group(1) if m else None


def board_before_ply(game: chess.pgn.Game, target_ply: int) -> Optional[chess.Board]:
    """Return the board position BEFORE move `target_ply` is played (1-based)."""
    if target_ply < 1:
        return None
    node = game
    ply = 0
    while node.variations:
        if ply == target_ply - 1:
            return node.board()
        node = node.variation(0)
        ply += 1
    if ply == target_ply - 1:
        return node.board()
    return None


def validate_san(board: chess.Board, san: str) -> Optional[str]:
    """Return canonical SAN if `san` is legal in `board`, else None."""
    try:
        mv = board.parse_san(san)
        return board.san(mv)
    except Exception:
        return None


def total_plies(game: chess.pgn.Game) -> int:
    n, count = game, 0
    while n.variations:
        n = n.variation(0)
        count += 1
    return count


def position_at_ply(game: chess.pgn.Game, target_ply: int) -> Optional[dict]:
    """Return {fen, last_move, ply, move_number, side} at the given ply (1-indexed)."""
    for ply, board, last_move, _ in _walk(game):
        if ply == target_ply:
            return {
                "fen": board.fen(),
                "last_move": last_move,
                "ply": ply,
                "move_number": (ply + 1) // 2,
                # color who JUST moved:
                "side": "white" if ply % 2 == 1 else "black",
            }
    return None


def _user_color(raw_summary: dict, username: str) -> Optional[str]:
    for c in ("white", "black"):
        name = (raw_summary.get(c) or {}).get("name", "")
        if name and name.lower() == username.lower():
            return c
    return None


def pick_key_moments(
    game: chess.pgn.Game,
    raw_summary: dict,
    username: str,
    lichess_analysis: Optional[list] = None,
) -> dict:
    """Pick opening / midgame / endgame positions.

    `lichess_analysis` is the per-ply analysis array from the Lichess game JSON
    (each entry may contain 'eval', 'mate', 'judgment' with name in
    {Inaccuracy, Mistake, Blunder}).
    """
    plies = total_plies(game)
    user_color = _user_color(raw_summary, username)

    # --- Opening: end of book or move ~10 ---
    opening_ply = min(20, plies)  # ~10 full moves
    opening = position_at_ply(game, opening_ply)
    opening_label = "After the opening"

    # --- Midgame: worst blunder by user (or biggest swing) ---
    mid_ply = None
    mid_label = "Critical moment"
    mid_eval_before = None
    mid_eval_after = None
    mid_comment = None
    mid_san = None
    worst_entry: dict = {}

    if lichess_analysis:
        # judgment names ordered by severity
        severity = {"Blunder": 3, "Mistake": 2, "Inaccuracy": 1}
        worst = None  # (severity, ply_index_1based, name, comment, full_entry)
        for i, entry in enumerate(lichess_analysis):
            judg = (entry or {}).get("judgment") or {}
            name = judg.get("name")
            if not name:
                continue
            ply_idx = i + 1  # analysis is 0-indexed by half-move applied
            mover = "white" if ply_idx % 2 == 1 else "black"
            if user_color and mover != user_color:
                continue
            sev = severity.get(name, 0)
            if not worst or sev > worst[0]:
                worst = (sev, ply_idx, name, judg.get("comment", ""), entry or {})
        if worst:
            mid_ply = worst[1]
            mid_label = f"{worst[2]} on move {(mid_ply + 1) // 2}"
            mid_comment = worst[3] or None
            worst_entry = worst[4]            # SAN of the move actually played at that ply (from raw_summary["moves"])
            sans = (raw_summary.get("moves") or "").split()
            if 0 < mid_ply <= len(sans):
                mid_san = sans[mid_ply - 1]

            def _ev(idx):
                if idx < 0 or idx >= len(lichess_analysis):
                    return None
                e = lichess_analysis[idx] or {}
                if "eval" in e:
                    return e["eval"] / 100.0
                if "mate" in e:
                    return f"#{e['mate']}"
                return None
            mid_eval_before = _ev(mid_ply - 2)
            mid_eval_after = _ev(mid_ply - 1)

    # Validate the best-move SAN against the actual pre-move position.
    # Priority: (1) variation field first token, (2) comment regex fallback.
    mid_best_san = None
    if mid_ply:
        pre_board = board_before_ply(game, mid_ply)
        candidates = []
        # variation: e.g. "Qd3 exd4 ..." — first token is the best move in SAN
        first_var_tokens = (worst_entry.get("variation") or "").split()
        if first_var_tokens:
            candidates.append(first_var_tokens[0])
        if mid_comment:
            from_comment = extract_best_san_from_comment(mid_comment)
            if from_comment:
                candidates.append(from_comment)
        for candidate in candidates:
            if pre_board is not None:
                validated = validate_san(pre_board, candidate)
                if validated:
                    mid_best_san = validated
                    break
                log.info(
                    "Best-move candidate '%s' not legal in position; trying next.",
                    candidate,
                )

    if mid_ply is None:
        # Fallback: middle of the game
        mid_ply = max(opening_ply + 1, plies // 2)
    midgame = position_at_ply(game, mid_ply) if plies else None

    # Pre-move FEN (position just before the critical move) — used by engine.
    mid_pre_board = board_before_ply(game, mid_ply) if mid_ply else None
    mid_pre_fen = mid_pre_board.fen() if mid_pre_board else None

    # --- Endgame: final position + last few SANs as ground truth ---
    endgame = position_at_ply(game, plies) if plies else None
    endgame_label = "Final position"
    all_sans = (raw_summary.get("moves") or "").split()
    last_sans = all_sans[-8:] if all_sans else []
    end_status = raw_summary.get("status")
    end_winner = raw_summary.get("winner")

    return {
        "user_color": user_color,
        "opening": {"label": opening_label, **(opening or {})} if opening else None,
        "midgame": (
            {
                "label": mid_label,
                "eval_before": mid_eval_before,
                "eval_after": mid_eval_after,
                "played_san": mid_san,
                "best_san": mid_best_san,
                "lichess_comment": mid_comment,
                "pre_fen": mid_pre_fen,
                **(midgame or {}),
            }
            if midgame
            else None
        ),
        "endgame": (
            {
                "label": endgame_label,
                "last_moves": last_sans,
                "status": end_status,
                "winner": end_winner,
                **(endgame or {}),
            }
            if endgame
            else None
        ),
    }


def build_sparkline(lichess_analysis: Optional[list], max_len: int = 40) -> Optional[str]:
    """Render a tiny ASCII sparkline of evaluation across the game."""
    if not lichess_analysis:
        return None
    evals = []
    for e in lichess_analysis:
        if not e:
            continue
        if "eval" in e:
            evals.append(max(-800, min(800, e["eval"])))  # clamp ±8 pawns
        elif "mate" in e:
            evals.append(800 if e["mate"] > 0 else -800)
    if len(evals) < 4:
        return None
    # downsample
    if len(evals) > max_len:
        step = len(evals) / max_len
        evals = [evals[int(i * step)] for i in range(max_len)]
    lo, hi = -800, 800
    out = []
    for v in evals:
        norm = (v - lo) / (hi - lo)  # 0..1
        idx = max(0, min(len(SPARK_CHARS) - 1, int(norm * (len(SPARK_CHARS) - 1))))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


async def fetch_board_image(
    session: aiohttp.ClientSession,
    fen: str,
    last_move: Optional[str] = None,
    color: str = "white",
) -> Optional[bytes]:
    """Download a single-frame board GIF from Lichess. Returns bytes or None on error."""
    params = {"fen": fen, "color": color}
    if last_move:
        params["lastMove"] = last_move
    try:
        async with session.get(BOARD_URL, params=params, timeout=20) as r:
            if r.status != 200:
                log.warning("Board image %s for fen=%s", r.status, fen[:30])
                return None
            return await r.read()
    except Exception:
        log.exception("Board image fetch failed")
        return None


async def fetch_full_game_gif(
    session: aiohttp.ClientSession,
    game_id: str,
    orientation: str = "white",
    theme: str = "brown",
    piece: str = "cburnett",
) -> Optional[bytes]:
    """Fetch the public animated full-game GIF for a given Lichess game id."""
    if not game_id:
        return None
    url = FULL_GAME_GIF_URL.format(game_id=game_id)
    params = {}
    if orientation == "black":
        params["orientation"] = "black"
    if theme and theme != "brown":
        params["theme"] = theme
    if piece and piece != "cburnett":
        params["piece"] = piece
    try:
        async with session.get(url, params=params or None, timeout=30) as r:
            if r.status != 200:
                body = ""
                try:
                    body = (await r.text())[:200]
                except Exception:
                    pass
                log.warning("Full-game gif %s for %s body=%s", r.status, game_id, body)
                return None
            return await r.read()
    except Exception:
        log.exception("Full-game gif fetch failed")
        return None


def fmt_eval(v) -> str:
    if v is None:
        return "?"
    if isinstance(v, str):  # mate string already
        return v
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}"


async def enrich_with_engine(
    session: aiohttp.ClientSession,
    key_moments: dict,
) -> dict:
    """Query chess-api.com for the best move at the critical moment.

    Updates key_moments['midgame'] in-place with:
        best_san       — validated SAN of engine's top move (overrides Lichess heuristic)
        engine_eval    — centipawn eval (float, white-positive) or mate string
        engine_continuation — list of next SAN/UCI moves from engine
        engine_text    — human-readable description from chess-api

    Returns the (mutated) key_moments dict.
    """
    import engine as engine_mod  # local import to avoid circular at module level

    midgame = key_moments.get("midgame")
    if not midgame:
        return key_moments

    pre_fen = midgame.get("pre_fen")
    if not pre_fen:
        return key_moments

    log.info("Querying engine for critical moment FEN: %s", pre_fen[:60])
    result = await engine_mod.best_move(session, pre_fen, depth=14)
    if not result:
        log.info("Engine returned no result for midgame FEN.")
        return key_moments

    log.info("Engine result: %s", engine_mod.format_result(result))

    # Validate returned SAN is actually legal in the pre-move position.
    san = result.get("san")
    validated = None
    if san:
        import chess
        try:
            b = chess.Board(pre_fen)
            validated = validate_san(b, san)
        except Exception:
            pass

    if validated:
        midgame["best_san"] = validated
    elif san:
        log.info("Engine SAN '%s' failed validation; keeping previous best_san.", san)

    # Store engine extras for the LLM / display.
    ev = result.get("eval")
    mate = result.get("mate")
    # chess-api.com returns eval already in pawns (e.g. 0.29), NOT centipawns.
    midgame["engine_eval"] = f"#{mate}" if mate is not None else ev
    # The continuationArr starts AFTER the best move was played. Prepend the
    # best move's UCI (lan) so render_variation can animate it from pre_fen.
    cont = result.get("continuationArr") or []
    best_lan = result.get("lan") or result.get("move")
    if best_lan and (not cont or cont[0] != best_lan):
        cont = [best_lan] + list(cont)
    midgame["engine_continuation"] = cont
    midgame["engine_text"] = result.get("text") or ""

    return key_moments


async def find_worst_user_move(
    session: aiohttp.ClientSession,
    game: chess.pgn.Game,
    raw_summary: dict,
    username: str,
    max_scan: int = 18,
) -> Optional[dict]:
    """Scan the user's moves with Stockfish to find the one with the biggest eval swing.

    Returns a partial midgame dict with pre_fen, played_san, ply, eval_before,
    eval_after (eval from engine perspective at the next user move), or None.

    Uses at most `max_scan` engine calls (subsamples for very long games).
    """
    import asyncio
    import engine as engine_mod
    import engine_pool

    user_color = _user_color(raw_summary, username)
    sans = (raw_summary.get("moves") or "").split()

    # Collect all user-ply positions (ply, pre_fen, played_san).
    board = chess.Board()
    node = game
    ply = 0
    user_plies: list[tuple] = []
    while node.variations:
        node = node.variation(0)
        ply += 1
        pre_fen = board.fen()
        board.push(node.move)
        mover = "white" if ply % 2 == 1 else "black"
        if mover == user_color:
            san = sans[ply - 1] if ply - 1 < len(sans) else None
            user_plies.append((ply, pre_fen, san))

    if not user_plies:
        return None

    # Subsample if too many moves.
    if len(user_plies) > max_scan:
        step = max(1, len(user_plies) // max_scan)
        user_plies = user_plies[::step][:max_scan]

    log.info("find_worst_user_move: scanning %d user plies", len(user_plies))

    # Bound concurrent game scans across the process so a flood of incoming
    # games can't starve interactive /ask requests on the same engine pool.
    local_engine = engine_pool.is_available()
    evals: list[Optional[float]] = []
    results: list[Optional[dict]] = []
    async with engine_pool.game_scan_semaphore():
        for _, pre_fen, _ in user_plies:
            res = await engine_mod.best_move(session, pre_fen, depth=12, think_ms=50)
            ev = res.get("eval") if res else None
            evals.append(ev if ev is not None else None)
            results.append(res)
            if not local_engine:
                # Politeness throttle for the chess-api.com fallback only.
                await asyncio.sleep(0.08)

    # Find the move with the biggest negative eval swing between consecutive user moves.
    # Swing = eval_after_move - eval_before_move, from the user's perspective.
    worst_idx: Optional[int] = None
    worst_swing = 0.0
    for i in range(1, len(evals)):
        e_before = evals[i - 1]
        e_after = evals[i]
        if e_before is None or e_after is None:
            continue
        # For white: positive eval is good → big drop = blunder.
        # For black: negative eval is good → big rise = blunder.
        if user_color == "white":
            swing = e_after - e_before  # negative = got worse
        else:
            swing = e_before - e_after  # negative = got worse
        if swing < worst_swing:
            worst_swing = swing
            worst_idx = i - 1  # the move at index i-1 caused this swing

    if worst_idx is None:
        log.info("find_worst_user_move: no significant swing found — using move with lowest eval")
        # Fallback: pick the ply where the user's position is worst.
        min_ev = None
        for i, ev in enumerate(evals):
            if ev is None:
                continue
            adjusted = ev if user_color == "white" else -ev
            if min_ev is None or adjusted < min_ev:
                min_ev = adjusted
                worst_idx = i
        if worst_idx is None:
            return None

    ply, pre_fen, played_san = user_plies[worst_idx]
    ev_before = evals[worst_idx]
    ev_after = evals[worst_idx + 1] if worst_idx + 1 < len(evals) else None
    best_result = results[worst_idx]

    log.info(
        "find_worst_user_move: worst at ply %d (%s), swing %.2f → %.2f",
        ply, played_san, ev_before or 0, ev_after or 0,
    )

    # Validate engine's suggested best move.
    best_san = None
    engine_cont = []
    engine_eval_val = None
    engine_text = ""
    if best_result:
        raw_san = best_result.get("san")
        if raw_san:
            try:
                b = chess.Board(pre_fen)
                best_san = validate_san(b, raw_san)
            except Exception:
                pass
        engine_cont = best_result.get("continuationArr") or []
        # Prepend the engine's best move so the variation GIF shows it being played.
        best_lan = best_result.get("lan") or best_result.get("move")
        if best_lan and (not engine_cont or engine_cont[0] != best_lan):
            engine_cont = [best_lan] + list(engine_cont)
        ev_cp = best_result.get("eval")
        mate = best_result.get("mate")
        # chess-api.com returns eval already in pawns, NOT centipawns.
        engine_eval_val = f"#{mate}" if mate is not None else ev_cp
        engine_text = best_result.get("text") or ""

    return {
        "pre_fen": pre_fen,
        "played_san": played_san,
        "ply": ply,
        "move_number": (ply + 1) // 2,
        "eval_before": ev_before,
        "eval_after": ev_after,
        "best_san": best_san,
        "engine_eval": engine_eval_val,
        "engine_continuation": engine_cont,
        "engine_text": engine_text,
        "label": f"Biggest mistake on move {(ply + 1) // 2}",
    }
