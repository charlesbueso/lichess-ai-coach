"""One-off tester: analyze a specific Lichess game ID and post it to Discord.

Usage:
    python test_game.py <game_id>
    python test_game.py <game_id> --no-discord     # skip Discord, print to stdout
    python test_game.py <game_id> --no-store       # don't write to the DB

Examples:
    python test_game.py 08EkrvyB
    python test_game.py 08EkrvyB --no-discord
"""
import argparse
import asyncio
import io
import json
import logging
import sys
import time

import aiohttp
import discord

import config
import storage
import lichess
import llm
import board as board_mod
import main as app  # reuse _post_game_blog and helpers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("test_game")


async def fetch_one_game(session: aiohttp.ClientSession, game_id: str) -> dict:
    """Fetch a single game's full JSON (with analysis) from Lichess."""
    url = f"https://lichess.org/game/export/{game_id}"
    params = {
        "evals": "true",
        "opening": "true",
        "accuracy": "true",
        "clocks": "false",
        "moves": "true",
        "pgnInJson": "true",
    }
    headers = {"Accept": "application/json"}
    if config.LICHESS_TOKEN:
        headers["Authorization"] = f"Bearer {config.LICHESS_TOKEN}"
    async with session.get(url, headers=headers, params=params, timeout=60) as r:
        r.raise_for_status()
        return await r.json()


async def run(game_id: str, post_discord: bool, store: bool):
    async with aiohttp.ClientSession() as http:
        log.info("Fetching game %s ...", game_id)
        g = await fetch_one_game(http, game_id)

        summary = lichess.extract_summary_fields(g)
        mistakes = lichess.extract_key_mistakes(g, config.LICHESS_USERNAME)
        pgn = g.get("pgn", "")

        game_obj = board_mod.parse_pgn(pgn) if pgn else None
        if game_obj:
            key_moments = board_mod.pick_key_moments(
                game_obj, summary, config.LICHESS_USERNAME, summary.get("ply_analysis")
            )
            await board_mod.enrich_with_engine(http, key_moments)
        else:
            key_moments = {"user_color": None, "opening": None, "midgame": None, "endgame": None}

        log.info("Calling Groq for analysis ...")
        move_table = lichess.build_move_table(summary)
        sections = await llm.analyze_game(
            http, summary, config.LICHESS_USERNAME, key_moments, move_table=move_table,
        )
        full_md = llm.sections_to_markdown(sections)

        if store:
            storage.init()
            storage.save_game(
                game_id=game_id,
                created_at_ms=g.get("createdAt", int(time.time() * 1000)),
                pgn=pgn,
                raw=summary,
                summary=sections.get("summary") or full_md[:300],
                mistakes=mistakes,
                feedback=full_md,
                sections=sections,
                key_moments=key_moments,
            )
            log.info("Stored in DB.")

        # ---- Stdout preview ----
        print("\n========== HEADLINE ==========")
        print(sections.get("headline"))
        print("\n========== SECTIONS ==========")
        print(full_md)
        print("\n========== KEY MOMENTS ==========")
        print(json.dumps(key_moments, indent=2))

        if not post_discord:
            return

        # ---- Discord post ----
        log.info("Connecting to Discord ...")
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        done = asyncio.Event()

        @client.event
        async def on_ready():
            try:
                # Share the http session with main.py's blog renderer
                app._http = http
                channel = client.get_channel(config.DISCORD_CHANNEL_ID)
                if channel is None:
                    log.error("Channel %s not found (bot in server?)", config.DISCORD_CHANNEL_ID)
                    return
                await channel.send(f"_(test) analyzing game `{game_id}`_")
                await app._post_game_blog(
                    channel, summary, sections, key_moments, pgn, game_id,
                    g.get("createdAt", int(time.time() * 1000)),
                )
                log.info("Posted to Discord.")
            except Exception:
                log.exception("Discord post failed")
            finally:
                done.set()
                await client.close()

        await client.start(config.DISCORD_TOKEN)
        await done.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("game_id", help="Lichess game id (e.g. 08EkrvyB)")
    ap.add_argument("--no-discord", action="store_true", help="Don't post to Discord, just print")
    ap.add_argument("--no-store", action="store_true", help="Don't save to the SQLite DB")
    args = ap.parse_args()

    try:
        asyncio.run(run(args.game_id, post_discord=not args.no_discord, store=not args.no_store))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
