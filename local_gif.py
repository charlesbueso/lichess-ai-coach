"""Local board GIF renderer — pure Pillow, no SVG / no native libs.

Draws each frame from scratch:
  - 8x8 colored squares (lichess "brown" palette)
  - Last-move highlight (yellow tint on from/to squares)
  - Check highlight (red on king square)
  - Pieces drawn as Unicode chess glyphs from a TrueType font

Frames are then assembled into an animated GIF.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import chess
import chess.pgn

log = logging.getLogger("coach.local_gif")

_PIL_Image = None
_PIL_ImageDraw = None
_PIL_ImageFont = None
_init_attempted = False
_init_error: Optional[str] = None
_font_cache: dict = {}

# Lichess "brown" board colors
LIGHT = (240, 217, 181)
DARK = (181, 136, 99)
HL_LAST = (205, 210, 106)
HL_LAST_DARK = (170, 162, 58)
HL_CHECK = (235, 97, 80)
BG = (22, 21, 18)
LABEL = (140, 130, 110)

# Unicode chess glyphs (filled solid pieces; we recolor by side).
PIECE_GLYPH = {
    "K": "\u265A", "Q": "\u265B", "R": "\u265C",
    "B": "\u265D", "N": "\u265E", "P": "\u265F",
    "k": "\u265A", "q": "\u265B", "r": "\u265C",
    "b": "\u265D", "n": "\u265E", "p": "\u265F",
}

FONT_CANDIDATES = [
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "seguisym.ttf",
    "DejaVuSans.ttf",
    "FreeSerif.ttf",
]


def _ensure_imports() -> bool:
    global _PIL_Image, _PIL_ImageDraw, _PIL_ImageFont, _init_attempted, _init_error
    if _init_attempted:
        return _init_error is None
    _init_attempted = True
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
        _PIL_Image, _PIL_ImageDraw, _PIL_ImageFont = Image, ImageDraw, ImageFont
        return True
    except Exception as e:  # pragma: no cover
        _init_error = str(e)
        log.warning("Local GIF renderer disabled: %s", _init_error)
        return False


def is_available() -> bool:
    return _ensure_imports()


def _load_font(size_px: int):
    if size_px in _font_cache:
        return _font_cache[size_px]
    for path in FONT_CANDIDATES:
        try:
            f = _PIL_ImageFont.truetype(path, size_px)
            _font_cache[size_px] = f
            return f
        except Exception:
            continue
    log.warning("No suitable Unicode font found; using PIL default")
    f = _PIL_ImageFont.load_default()
    _font_cache[size_px] = f
    return f


def _square_to_xy(sq: int, sq_size: int, origin: tuple, orientation: str) -> tuple:
    file = chess.square_file(sq)
    rank = chess.square_rank(sq)
    if orientation == "white":
        col, row = file, 7 - rank
    else:
        col, row = 7 - file, rank
    return (origin[0] + col * sq_size, origin[1] + row * sq_size)


def _render_board(board: chess.Board, last_move: Optional[chess.Move],
                  check_sq: Optional[int], orientation: str, size_px: int):
    margin = max(12, size_px // 30)
    sq_size = (size_px - 2 * margin) // 8
    inner = sq_size * 8
    origin = (margin, margin)
    total = inner + 2 * margin

    img = _PIL_Image.new("RGB", (total, total), BG)
    draw = _PIL_ImageDraw.Draw(img)

    # Squares
    for sq in chess.SQUARES:
        x, y = _square_to_xy(sq, sq_size, origin, orientation)
        is_light = (chess.square_file(sq) + chess.square_rank(sq)) % 2 == 1
        color = LIGHT if is_light else DARK
        if last_move is not None and sq in (last_move.from_square, last_move.to_square):
            color = HL_LAST if is_light else HL_LAST_DARK
        if check_sq is not None and sq == check_sq:
            color = HL_CHECK
        draw.rectangle([x, y, x + sq_size, y + sq_size], fill=color)

    # Pieces
    glyph_size = int(sq_size * 0.82)
    font = _load_font(glyph_size)
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece:
            continue
        glyph = PIECE_GLYPH.get(piece.symbol())
        if not glyph:
            continue
        x, y = _square_to_xy(sq, sq_size, origin, orientation)
        try:
            bbox = draw.textbbox((0, 0), glyph, font=font)
            gw = bbox[2] - bbox[0]
            gh = bbox[3] - bbox[1]
            ox = x + (sq_size - gw) // 2 - bbox[0]
            oy = y + (sq_size - gh) // 2 - bbox[1]
        except Exception:
            ox = x + sq_size // 8
            oy = y + sq_size // 8
        # Draw outline + fill so both colors are clearly readable on either square color.
        if piece.color == chess.WHITE:
            outline, fill = (20, 20, 20), (250, 250, 250)
        else:
            outline, fill = (240, 240, 240), (15, 15, 15)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            draw.text((ox + dx, oy + dy), glyph, font=font, fill=outline)
        draw.text((ox, oy), glyph, font=font, fill=fill)

    # Rank / file labels
    label_size = max(9, margin - 2)
    label_font = _load_font(label_size)
    files = "abcdefgh"
    for i in range(8):
        file_label = files[i] if orientation == "white" else files[7 - i]
        rank_label = str(8 - i) if orientation == "white" else str(i + 1)
        fx = origin[0] + i * sq_size + sq_size // 2 - label_size // 2
        fy = origin[1] + inner + 1
        draw.text((fx, fy), file_label, font=label_font, fill=LABEL)
        rx = 2
        ry = origin[1] + i * sq_size + sq_size // 2 - label_size // 2
        draw.text((rx, ry), rank_label, font=label_font, fill=LABEL)

    return img


def render_slice(
    game: chess.pgn.Game,
    start_ply: int,
    end_ply: int,
    orientation: str = "white",
    size_px: int = 360,
    frame_ms: int = 900,
    final_hold_ms: int = 2500,
) -> Optional[bytes]:
    """Render plies [start_ply..end_ply] into an animated GIF. Returns bytes or None."""
    if not _ensure_imports():
        return None
    if start_ply < 1 or end_ply < start_ply:
        return None

    frames_pil = []
    durations = []

    pre_board = game.board()
    node = game
    cur_ply = 0
    pre_last_move = None
    while node.variations and cur_ply < start_ply - 1:
        node = node.variation(0)
        cur_ply += 1
        pre_last_move = node.move
        pre_board = node.board()

    check_sq = pre_board.king(pre_board.turn) if pre_board.is_check() else None
    frames_pil.append(_render_board(pre_board, pre_last_move, check_sq, orientation, size_px))
    durations.append(frame_ms)

    while node.variations and cur_ply < end_ply:
        node = node.variation(0)
        cur_ply += 1
        b = node.board()
        check_sq = b.king(b.turn) if b.is_check() else None
        frames_pil.append(_render_board(b, node.move, check_sq, orientation, size_px))
        durations.append(frame_ms)

    if not frames_pil:
        return None
    durations[-1] = final_hold_ms

    pal_frames = [f.convert("P", palette=_PIL_Image.ADAPTIVE, colors=64) for f in frames_pil]
    buf = io.BytesIO()
    pal_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=pal_frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return buf.getvalue()


def _frames_to_gif_bytes(frames_pil, durations, final_hold_ms: int) -> Optional[bytes]:
    if not frames_pil:
        return None
    durations[-1] = final_hold_ms
    pal_frames = [f.convert("P", palette=_PIL_Image.ADAPTIVE, colors=64) for f in frames_pil]
    buf = io.BytesIO()
    pal_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=pal_frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )
    return buf.getvalue()


def render_variation(
    start_fen: str,
    moves_uci_or_san: list,
    orientation: str = "white",
    size_px: int = 360,
    frame_ms: int = 1000,
    final_hold_ms: int = 2500,
    max_moves: int = 6,
) -> Optional[bytes]:
    """Render an animated GIF starting from `start_fen` and playing the given moves.

    `moves_uci_or_san` accepts either UCI tokens (e.g. 'e2e4', 'b7b8q') or SAN
    (e.g. 'Bd3'). Tries UCI first, then SAN. Stops at the first invalid move.
    """
    if not _ensure_imports():
        return None
    if not start_fen or not moves_uci_or_san:
        return None

    try:
        board = chess.Board(start_fen)
    except Exception:
        return None

    frames_pil = []
    durations = []

    # First frame: starting position (no last-move highlight)
    check_sq = board.king(board.turn) if board.is_check() else None
    frames_pil.append(_render_board(board, None, check_sq, orientation, size_px))
    durations.append(frame_ms)

    played = 0
    for tok in moves_uci_or_san:
        if played >= max_moves:
            break
        mv = None
        # Try UCI
        try:
            cand = chess.Move.from_uci(tok)
            if cand in board.legal_moves:
                mv = cand
        except Exception:
            pass
        # Fall back to SAN
        if mv is None:
            try:
                mv = board.parse_san(tok)
            except Exception:
                mv = None
        if mv is None:
            log.info("render_variation: skipping invalid move token %r", tok)
            break
        board.push(mv)
        check_sq = board.king(board.turn) if board.is_check() else None
        frames_pil.append(_render_board(board, mv, check_sq, orientation, size_px))
        durations.append(frame_ms)
        played += 1

    if played == 0:
        return None
    return _frames_to_gif_bytes(frames_pil, durations, final_hold_ms)
