# Lichess AI Coach

Tiny Python service that watches your Lichess games, asks Groq for human-like coaching feedback, posts it to Discord, and lets you ask follow-up questions per game. Designed to run forever on a Raspberry Pi Zero W.

## Files

- [main.py](main.py) — Discord bot + background poller + weekly loop (single process)
- [lichess.py](lichess.py) — Lichess API client (uses built-in analysis)
- [llm.py](llm.py) — Groq chat completions
- [board.py](board.py) — PGN parsing, key-moment selection, board image URLs, eval sparkline
- [storage.py](storage.py) — SQLite persistence
- [config.py](config.py) — env loader
- [requirements.txt](requirements.txt)
- [.env.example](.env.example)

## Setup

1. **Python 3.10+** recommended.
2. Clone / copy this folder, then:
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux/Pi
   source .venv/bin/activate

   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill it in.
4. **Lichess**: just put your username. A token is only needed for higher rate limits.
5. **Groq**: create an API key at https://console.groq.com/keys. Default model `llama-3.1-8b-instant` is fast and cheap.
6. **Discord bot**:
   - Create an app + bot at https://discord.com/developers/applications
   - Enable the **Message Content Intent**
   - Invite it to your server with the `bot` scope and `Send Messages` + `Read Message History` permissions
   - Copy the bot token into `.env`
   - Right-click your target channel → **Copy Channel ID** (Developer Mode must be on) → put into `.env`

## Run

```bash
python main.py
```

That's it. On first start it does **not** backfill old games — it just records "now" as the baseline and only analyzes games played afterwards.

## What you get per game

Each new game is posted as a "blog-style" sequence in your channel:

1. **Header card** — embed with result, opening, accuracy badge, and an ASCII eval sparkline
2. **♙ Opening** — board image after move ~10 + 1-2 sentences about your setup
3. **⚔️ Midgame — critical moment** — board image at the worst blunder/mistake (from Lichess analysis), with `Eval: +0.4 → -3.2` callout and a focused comment
4. **🏁 Endgame** — final position image + a closing comment
5. **💡 Improvements** + 🎨 **Style** + the game id for follow-ups

The bot needs the **Attach Files** permission in the channel for the board images.

## Discord commands

| Command | What it does |
|---|---|
| `!last` | Replays the most recently analyzed game with board images |
| `!game <id>` | Replays a stored game (header card + 3 boards + improvements) |
| `!ask <id> <question>` | Asks the LLM a follow-up question about that stored game |
| `!board <id> <move>` | Shows the board after the given full-move number |
| `!help` | Quick command list |

Example:
```
!ask abc123XY Why was move 17 bad?
```

## Weekly review

Once per week (defaults: Sunday 18:00 local, configurable via `WEEKLY_DAY`/`WEEKLY_HOUR`) the bot checks if you played any games in the last 7 days. If yes, it pulls all stored games from the last 90 days and posts a Markdown review: recurring mistakes, patterns, strengths, training recs.

## Storage

A single `data.db` SQLite file in the project folder. Wipe it to reset.

## Raspberry Pi Zero W notes

- All deps are pure-Python except `aiohttp` (small C ext) and `discord.py`. Both build/install fine on Pi OS.
  ```bash
  sudo apt install python3-venv python3-pip
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```
- Memory footprint at idle: ~40-60 MB. Fits the Zero W's 512 MB easily.
- No local engine, no ML models, no Docker — just network I/O.
- Run it under **systemd** so it survives reboots:

  `/etc/systemd/system/lichess-coach.service`:
  ```ini
  [Unit]
  Description=Lichess AI Coach
  After=network-online.target

  [Service]
  Type=simple
  WorkingDirectory=/home/pi/lichess-ai-coach
  ExecStart=/home/pi/lichess-ai-coach/.venv/bin/python main.py
  Restart=always
  RestartSec=15
  User=pi

  [Install]
  WantedBy=multi-user.target
  ```
  Then:
  ```bash
  sudo systemctl daemon-reload
  sudo systemctl enable --now lichess-coach
  journalctl -u lichess-coach -f
  ```
- Keep `POLL_INTERVAL_MINUTES` at 10+ to be nice to the Lichess API (and the Pi).

## How it avoids duplicates

The `state` table stores `last_game_ms` = the `createdAt` of the most recently processed game. Each poll calls `GET /api/games/user/{username}?since=<ms+1>`. Already-stored game ids are also checked defensively before re-processing.

## Troubleshooting

- **Bot connects but no posts ever appear**: confirm you played a *new* game after first start (no backfill by design). Test by sending `!last` after your first game finishes.
- **`Missing required env var`**: check `.env` is in the same folder you run `python main.py` from.
- **Groq 401**: regenerate API key.
- **Discord 403/can't send**: bot lacks permission in that channel, or `MessageContentIntent` not enabled in the dev portal.

## Next steps to fully test

1. Fill `.env`, run `python main.py`.
2. Confirm log line `Logged in as <YourBot>`.
3. In Discord, type `!help` — the bot should reply.
4. Play a quick Lichess game, request server-side analysis on it (or it'll just have less detail), wait one poll cycle.
5. You should see the analysis post. Then try `!ask <id> "what should I have played on move 10?"`.
