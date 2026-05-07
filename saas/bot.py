"""Discord bot: slash commands + on_ready hook for the SaaS service."""
from __future__ import annotations

import io
import json
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands

import asyncio

import board as board_mod
import lichess as lichess_mod
import local_gif
from saas import app_config, coach, db

log = logging.getLogger("coach.bot")


def _normalize_game_id(raw: str) -> str:
    """Lichess full ids are 12 chars (8 public + 4 player token); we store the 8-char public id.
    Strip any URL prefix, whitespace, then take the first 8 chars.
    """
    s = (raw or "").strip()
    if "/" in s:
        s = s.rstrip("/").split("/")[-1]
    # Drop query/fragment if pasted from URL
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s[:8]


# Default intents — NO message content (slash commands only).
def _intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = False
    return intents


# Shared HTTP session, set by main.py at startup.
_http: Optional[aiohttp.ClientSession] = None


def set_http(session: aiohttp.ClientSession) -> None:
    global _http
    _http = session


def http() -> aiohttp.ClientSession:
    if _http is None:
        raise RuntimeError("aiohttp session not set; call set_http() at startup")
    return _http


# --- bot factory ----------------------------------------------------------

class CoachBot(discord.Client):
    def __init__(self):
        super().__init__(intents=_intents())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Global slash command sync. May take up to ~1h to propagate to all guilds
        # the first time, but registers immediately for newly-joined guilds.
        try:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
        except Exception:
            log.exception("Slash command sync failed")


bot = CoachBot()
tree = bot.tree


# ---------- helpers -------------------------------------------------------

async def _tenant_for_interaction(inter: discord.Interaction) -> Optional[dict]:
    if inter.guild_id is None:
        return None
    return await db.get_tenant_by_guild(inter.guild_id)


def _is_admin_or_installer(inter: discord.Interaction, tenant: dict) -> bool:
    if inter.user.id == int(tenant.get("discord_installer_user_id") or 0):
        return True
    perms = getattr(inter.user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


async def _need_active_tenant(inter: discord.Interaction) -> Optional[dict]:
    """Reply with a guard message and return None if no usable tenant is bound."""
    tenant = await _tenant_for_interaction(inter)
    if not tenant:
        await inter.response.send_message(
            "This server isn't connected to a Lichess Coach subscription. "
            f"Get one at {app_config.BASE_URL} and install the bot from the success page.",
            ephemeral=True,
        )
        return None
    if tenant["status"] in ("canceled",):
        await inter.response.send_message(
            "Your subscription has been canceled. Use `/billing` to reactivate.",
            ephemeral=True,
        )
        return None
    return tenant


# ---------- /setup --------------------------------------------------------

@tree.command(name="setup", description="Connect a Lichess username and target channel for analyses.")
@app_commands.describe(
    lichess="Your Lichess username (case-insensitive).",
    channel="The channel where game analyses will be posted.",
)
async def cmd_setup(inter: discord.Interaction, lichess: str, channel: discord.TextChannel):
    tenant = await _tenant_for_interaction(inter)
    if not tenant:
        await inter.response.send_message(
            "This server is not bound to a subscription. Subscribe at "
            f"{app_config.BASE_URL} first.",
            ephemeral=True,
        )
        return
    if not _is_admin_or_installer(inter, tenant):
        await inter.response.send_message(
            "Only the installer or a server admin can run `/setup`.",
            ephemeral=True,
        )
        return

    await inter.response.defer(ephemeral=True, thinking=True)
    # Confirm the Lichess username exists by hitting the public profile endpoint.
    try:
        async with http().get(
            f"https://lichess.org/api/user/{lichess}", timeout=15,
        ) as r:
            if r.status == 404:
                await inter.followup.send(
                    f"Lichess user `{lichess}` not found. Double-check spelling.",
                    ephemeral=True,
                )
                return
            r.raise_for_status()
    except Exception:
        log.exception("Lichess profile lookup failed")
        await inter.followup.send(
            "Could not reach Lichess to verify the username. Try again in a minute.",
            ephemeral=True,
        )
        return

    await db.set_tenant_setup(
        guild_id=inter.guild_id,
        lichess_username=lichess,
        discord_channel_id=channel.id,
    )
    await inter.followup.send(
        f"**Connected ✓**\n"
        f"• Lichess username: **{lichess}**\n"
        f"• Analyses post in: {channel.mention}\n"
        f"• Polling cadence: every **{app_config.POLL_INTERVAL_MINUTES} minutes**\n\n"
        f"**What happens next**\n"
        f"1. Play any rated/casual game on Lichess and **finish it** "
        f"(resign, mate, or draw — aborted games are skipped).\n"
        f"2. Within {app_config.POLL_INTERVAL_MINUTES} min the bot posts an "
        f"analysis embed in {channel.mention} and opens a **thread** under it.\n"
        f"3. Inside that thread, just type `/ask question:<your question>` — no "
        f"need to provide a game id, the thread is already linked to the game.\n\n"
        f"**Useful commands** (run `/help` any time)\n"
        f"• `/game` — replay the most recent analysis here.\n"
        f"• `/game game_id:<id>` — replay a specific game (id is in the post footer).\n"
        f"• `/ask question:... [game_id:...]` — follow-up Q&A about a game.\n"
        f"• `/setchannel channel:#new` — move the analysis feed.\n"
        f"• `/billing` — manage your subscription.\n\n"
        f"_Heads up: only the installer or a server admin can change setup or billing._",
        ephemeral=True,
    )


# ---------- /setchannel ---------------------------------------------------

@tree.command(name="setchannel", description="Change the channel where analyses are posted.")
@app_commands.describe(channel="The new target channel.")
async def cmd_setchannel(inter: discord.Interaction, channel: discord.TextChannel):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    if not _is_admin_or_installer(inter, tenant):
        await inter.response.send_message(
            "Only the installer or a server admin can change the channel.",
            ephemeral=True,
        )
        return
    await db.set_tenant_setup(
        guild_id=inter.guild_id,
        lichess_username=tenant["lichess_username"] or "",
        discord_channel_id=channel.id,
    )
    await inter.response.send_message(f"Channel updated → {channel.mention}", ephemeral=True)


# ---------- /game ---------------------------------------------------------

@tree.command(
    name="game",
    description="Replay a previously-analyzed game (latest by default).",
)
@app_commands.describe(
    game_id=(
        "Lichess game id (8 chars). Find it in the post footer or the URL: "
        "lichess.org/<game_id>. Omit to replay your most recent analysis."
    ),
)
async def cmd_game(inter: discord.Interaction, game_id: Optional[str] = None):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    if game_id:
        gid = _normalize_game_id(game_id)
        g = await db.get_game(tenant["id"], gid)
        if not g:
            await inter.response.send_message(
                f"No stored analysis for `{gid}`.\n"
                f"• Only games **played after you ran `/setup`** are analyzed and stored.\n"
                f"• Use the 8-char public id (the one in `lichess.org/<id>` URLs, or the **🆔 Game ID** "
                f"footer of an analysis post).",
                ephemeral=True,
            )
            return
    else:
        g = await db.get_last_game(tenant["id"])
        if not g:
            await inter.response.send_message(
                "No games stored yet — play a game on Lichess and one will be "
                "analyzed automatically within "
                f"{app_config.POLL_INTERVAL_MINUTES} minutes.",
                ephemeral=True,
            )
            return
    await inter.response.defer(thinking=True)
    await coach.post_game_blog(
        http(), inter.channel,
        g["raw_json"], g["sections"] or {}, g["key_moments"] or {},
        g["pgn"], g["game_id"], int(g["created_at_ms"]),
    )
    await inter.followup.send("Replayed ✓", ephemeral=True)


# ---------- /ask ----------------------------------------------------------

@tree.command(name="ask", description="Ask a follow-up question about a stored game.")
@app_commands.describe(
    question="Your question about the game.",
    game_id="Game id (optional if running inside the auto-created thread).",
)
async def cmd_ask(inter: discord.Interaction, question: str, game_id: Optional[str] = None):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return

    g = None
    if game_id:
        g = await db.get_game(tenant["id"], _normalize_game_id(game_id))
    elif isinstance(inter.channel, discord.Thread):
        g = await db.get_game_by_thread(tenant["id"], inter.channel.id)

    if not g:
        await inter.response.send_message(
            "Couldn't find a stored game.\n"
            "• Run this **inside the auto-created thread** under an analysis post (no `game_id` needed), or\n"
            "• Pass `game_id:<id>` — find it in the **🆔 Game ID** footer of the analysis post, "
            "or at the end of the Lichess URL (`lichess.org/<game_id>`).",
            ephemeral=True,
        )
        return

    under_cap = await db.consume_ask_quota(tenant["id"], g["game_id"], app_config.ASKS_PER_GAME)
    if not under_cap:
        await inter.response.send_message(
            f"You've hit the {app_config.ASKS_PER_GAME}-question limit for game "
            f"`{g['game_id']}`. (Limit resets per game.)",
            ephemeral=True,
        )
        return

    await inter.response.defer(thinking=True)
    await coach.answer_question(http(), inter.channel, tenant, g, question)
    await inter.followup.send("Answered ✓", ephemeral=True)


# ---------- /board --------------------------------------------------------

@tree.command(
    name="board",
    description="Animated GIF window: 5 moves before to 5 moves after the requested full-move.",
)
@app_commands.describe(game_id="Game id (8 chars).", move="Full-move number (1+).")
async def cmd_board(inter: discord.Interaction, game_id: str, move: int):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    gid = _normalize_game_id(game_id)
    g = await db.get_game(tenant["id"], gid)
    if not g:
        await inter.response.send_message(
            f"No stored analysis for `{gid}`. Only games played after `/setup` are analyzed; "
            f"use the 8-char public id from `lichess.org/<id>`.",
            ephemeral=True,
        )
        return
    if move < 1:
        await inter.response.send_message("Move number must be >= 1.", ephemeral=True)
        return
    game_obj = board_mod.parse_pgn(g["pgn"])
    if not game_obj:
        await inter.response.send_message("Couldn't parse PGN.", ephemeral=True)
        return

    total_plies = board_mod.total_plies(game_obj)
    if total_plies < 1:
        await inter.response.send_message("Game has no moves.", ephemeral=True)
        return

    # Window: 5 full-moves before through 5 after the target move (clamped to game).
    # White's Nth move = ply 2N-1; end of full-move N (after black) = ply 2N.
    win_lo = max(1, move - 5)
    win_hi = move + 5
    start_ply = max(1, 2 * win_lo - 1)
    end_ply = min(total_plies, 2 * win_hi)
    if end_ply < start_ply:
        end_ply = start_ply

    user_color = (g.get("key_moments") or {}).get("user_color") or "white"
    await inter.response.defer(thinking=True)

    gif_bytes = None
    if local_gif.is_available():
        try:
            loop = asyncio.get_running_loop()
            gif_bytes = await loop.run_in_executor(
                None,
                local_gif.render_slice,
                game_obj,
                start_ply,
                end_ply,
                user_color,
                360,    # size_px
                900,    # frame_ms
                2500,   # final_hold_ms
            )
        except Exception:
            log.exception("render_slice failed")

    if not gif_bytes:
        # Fallback: single static board at requested move.
        target_ply = min(2 * move, total_plies)
        pos = board_mod.position_at_ply(game_obj, target_ply)
        if not pos:
            await inter.followup.send("Couldn't render that position.", ephemeral=True)
            return
        gif_bytes = await board_mod.fetch_board_image(
            http(), pos["fen"], pos.get("last_move"), color=user_color,
        )
        if not gif_bytes:
            await inter.followup.send("Couldn't fetch board image.", ephemeral=True)
            return

    actual_lo = (start_ply + 1) // 2
    actual_hi = (end_ply + 1) // 2
    await inter.followup.send(
        content=(
            f"`{gid}` \u2014 moves **{actual_lo}\u2013{actual_hi}** "
            f"(target: move {move})"
        ),
        file=discord.File(
            io.BytesIO(gif_bytes),
            filename=f"{gid}_m{move}_window.gif",
        ),
    )


# ---------- /billing ------------------------------------------------------

@tree.command(name="billing", description="Open the Stripe customer portal to manage your subscription.")
async def cmd_billing(inter: discord.Interaction):
    tenant = await _tenant_for_interaction(inter)
    if not tenant:
        await inter.response.send_message(
            f"This server is not connected. Subscribe at {app_config.BASE_URL}.",
            ephemeral=True,
        )
        return
    if not _is_admin_or_installer(inter, tenant):
        await inter.response.send_message(
            "Only the installer or a server admin can manage billing.",
            ephemeral=True,
        )
        return

    from saas import billing
    try:
        url = await billing.create_billing_portal_url(tenant["stripe_customer_id"])
    except Exception:
        log.exception("Stripe portal create failed")
        await inter.response.send_message(
            "Could not generate the billing link. Try again in a minute.",
            ephemeral=True,
        )
        return
    await inter.response.send_message(
        f"Manage your subscription here (private link, single-use): {url}",
        ephemeral=True,
    )


# ---------- /help ---------------------------------------------------------

@tree.command(name="help", description="Show what the Lichess Coach bot can do.")
async def cmd_help(inter: discord.Interaction):
    text = (
        "**Chess Brain, the Lichess AI Coach — quick guide**\n\n"
        "**Setup (admins, one time)**\n"
        "1. Subscribe at " + app_config.BASE_URL + " and authorize the bot in your server.\n"
        "2. Run `/setup lichess:<your_username> channel:#some-channel`.\n"
        "3. Play any game on Lichess and finish it — analysis posts automatically "
        f"within {app_config.POLL_INTERVAL_MINUTES} minutes.\n\n"
        "**Daily commands**\n"
        "• `/game` — replay your most recent analysis here.\n"
        "• `/game game_id:<id>` — replay a specific game by id.\n"
        "• `/ask question:<your question>` — Q&A about a game. Run it **inside the "
        "thread** auto-created under each analysis (no id needed). From elsewhere, "
        "pass `game_id:<id>`.\n"
        "• `/board game_id:<id> move:<n>` — show the board after full-move `n`.\n"
        "• `/setchannel channel:#new` — move the analysis feed (admins).\n"
        "• `/billing` — manage your subscription (admins).\n\n"
        "**Where do I find a game id?**\n"
        "It's the 8-character code at the end of every Lichess URL "
        "(`lichess.org/abc12345`) and is also printed in the **🆔 Game ID** footer "
        "of every analysis post.\n\n"
        f"**Limits:** {app_config.GAMES_PER_DAY_PER_TENANT} games analyzed/day, "
        f"{app_config.ASKS_PER_GAME} `/ask` questions per game."
    )
    await inter.response.send_message(text, ephemeral=True)


# ---------- guild lifecycle ----------------------------------------------

@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("Joined guild %s (%s)", guild.id, guild.name)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s); connected to %d guild(s)",
             bot.user, bot.user.id if bot.user else "?", len(bot.guilds))
