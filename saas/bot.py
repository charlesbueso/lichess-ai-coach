"""Discord bot: slash commands + on_ready hook for the SaaS service."""
from __future__ import annotations

import io
import json
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands

import board as board_mod
import lichess as lichess_mod
from saas import app_config, coach, db

log = logging.getLogger("coach.bot")


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
        f"Connected ✓\n"
        f"• Lichess: **{lichess}**\n"
        f"• Channel: {channel.mention}\n\n"
        f"Play a rated/casual game and the analysis will appear within "
        f"{app_config.POLL_INTERVAL_MINUTES} minutes.",
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


# ---------- /last ---------------------------------------------------------

@tree.command(name="last", description="Replay the most recently analyzed game.")
async def cmd_last(inter: discord.Interaction):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    g = await db.get_last_game(tenant["id"])
    if not g:
        await inter.response.send_message("No games stored yet.", ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    await coach.post_game_blog(
        http(), inter.channel,
        g["raw_json"], g["sections"] or {}, g["key_moments"] or {},
        g["pgn"], g["game_id"], int(g["created_at_ms"]),
    )
    await inter.followup.send("Replayed ✓", ephemeral=True)


# ---------- /game ---------------------------------------------------------

@tree.command(name="game", description="Replay a stored game by id.")
@app_commands.describe(game_id="Lichess game id (8 chars).")
async def cmd_game(inter: discord.Interaction, game_id: str):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    g = await db.get_game(tenant["id"], game_id)
    if not g:
        await inter.response.send_message(f"No stored analysis for `{game_id}`.", ephemeral=True)
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
        g = await db.get_game(tenant["id"], game_id)
    elif isinstance(inter.channel, discord.Thread):
        g = await db.get_game_by_thread(tenant["id"], inter.channel.id)

    if not g:
        await inter.response.send_message(
            "Couldn't find a stored game. Provide `game_id:` or run `/ask` inside a "
            "game thread.",
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

@tree.command(name="board", description="Show the board after a given full-move number.")
@app_commands.describe(game_id="Game id (8 chars).", move="Full-move number (1+).")
async def cmd_board(inter: discord.Interaction, game_id: str, move: int):
    tenant = await _need_active_tenant(inter)
    if not tenant:
        return
    g = await db.get_game(tenant["id"], game_id)
    if not g:
        await inter.response.send_message(f"No stored analysis for `{game_id}`.", ephemeral=True)
        return
    if move < 1:
        await inter.response.send_message("Move number must be >= 1.", ephemeral=True)
        return
    game_obj = board_mod.parse_pgn(g["pgn"])
    if not game_obj:
        await inter.response.send_message("Couldn't parse PGN.", ephemeral=True)
        return
    target_ply = min(2 * move, board_mod.total_plies(game_obj))
    pos = board_mod.position_at_ply(game_obj, target_ply)
    if not pos:
        await inter.response.send_message("Couldn't find that position.", ephemeral=True)
        return
    user_color = (g.get("key_moments") or {}).get("user_color") or "white"
    await inter.response.defer(thinking=True)
    img = await board_mod.fetch_board_image(http(), pos["fen"], pos.get("last_move"), color=user_color)
    if not img:
        await inter.followup.send("Couldn't fetch board image.", ephemeral=True)
        return
    await inter.followup.send(
        content=f"`{game_id}` — after move {move} ({pos['side']} just moved)",
        file=discord.File(io.BytesIO(img), filename=f"{game_id}_m{move}.gif"),
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
        "**Lichess AI Coach**\n"
        "• `/setup lichess channel` — connect your Lichess username and target channel.\n"
        "• `/setchannel channel` — change the target channel.\n"
        "• `/last` — replay the most recent analyzed game.\n"
        "• `/game game_id` — replay any stored game.\n"
        "• `/ask question [game_id]` — ask a question about a game (works inside the game thread).\n"
        "• `/board game_id move` — show a static board at a given full-move number.\n"
        "• `/billing` — manage your subscription (admins only).\n"
        f"\nLimits: {app_config.GAMES_PER_DAY_PER_TENANT} games analyzed/day, "
        f"{app_config.ASKS_PER_GAME} questions per game."
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
