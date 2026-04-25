"""SQLite storage for games and processing state. Single-file, sync (fast enough)."""
import sqlite3
import json
import time
from contextlib import contextmanager
from typing import Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    created_at   INTEGER NOT NULL,           -- lichess game createdAt (ms)
    stored_at    INTEGER NOT NULL,           -- when we processed it (s)
    pgn          TEXT NOT NULL,
    raw_json     TEXT NOT NULL,              -- full lichess game json
    summary      TEXT,                       -- LLM short summary
    mistakes     TEXT,                       -- JSON list of key mistakes
    feedback     TEXT,                       -- full LLM feedback markdown
    sections     TEXT,                       -- JSON: structured LLM sections
    key_moments  TEXT                        -- JSON: opening/midgame/endgame positions
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_columns(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(games)").fetchall()}
    if "sections" not in cols:
        c.execute("ALTER TABLE games ADD COLUMN sections TEXT")
    if "key_moments" not in cols:
        c.execute("ALTER TABLE games ADD COLUMN key_moments TEXT")
    if "thread_id" not in cols:
        c.execute("ALTER TABLE games ADD COLUMN thread_id INTEGER")


@contextmanager
def _conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with _conn() as c:
        c.executescript(SCHEMA)
        _ensure_columns(c)


def get_state(key: str, default: Optional[str] = None) -> Optional[str]:
    with _conn() as c:
        r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_state(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def has_game(game_id: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM games WHERE game_id=?", (game_id,)).fetchone() is not None


def save_game(
    game_id: str,
    created_at_ms: int,
    pgn: str,
    raw: dict,
    summary: str,
    mistakes: list,
    feedback: str,
    sections: Optional[dict] = None,
    key_moments: Optional[dict] = None,
):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO games "
            "(game_id, created_at, stored_at, pgn, raw_json, summary, mistakes, feedback, sections, key_moments) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                game_id,
                created_at_ms,
                int(time.time()),
                pgn,
                json.dumps(raw),
                summary,
                json.dumps(mistakes),
                feedback,
                json.dumps(sections) if sections is not None else None,
                json.dumps(key_moments) if key_moments is not None else None,
            ),
        )


def get_game(game_id: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return dict(r) if r else None


def update_key_moments(game_id: str, key_moments: dict) -> None:
    """Patch only the key_moments column for an existing game."""
    with _conn() as c:
        c.execute(
            "UPDATE games SET key_moments=? WHERE game_id=?",
            (json.dumps(key_moments), game_id),
        )


def set_thread_id(game_id: str, thread_id: int) -> None:
    """Save the Discord thread id associated with a game."""
    with _conn() as c:
        c.execute("UPDATE games SET thread_id=? WHERE game_id=?", (int(thread_id), game_id))


def get_game_by_thread_id(thread_id: int) -> Optional[dict]:
    """Return the game row for a Discord thread id, if any."""
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM games WHERE thread_id=? ORDER BY stored_at DESC LIMIT 1",
            (int(thread_id),),
        ).fetchone()
        return dict(r) if r else None


def get_last_game() -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM games ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None


def get_games_since(since_ms: int) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM games WHERE created_at >= ? ORDER BY created_at ASC",
            (since_ms,),
        ).fetchall()
        return [dict(r) for r in rows]
