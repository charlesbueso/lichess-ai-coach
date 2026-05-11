"""Main entry point: Discord bot + background poller + weekly job, all in one asyncio loop."""
import asyncio
import datetime as dt
import io
import json
import logging
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import config
import storage
import lichess
import llm
import board as board_mod
import local_gif

log = logging.getLogger("coach")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Shared aiohttp session (created on startup)
_http: Optional[aiohttp.ClientSession] = None
_poll_started = False


# ---------- helpers ----------

def _result_str(winner) -> str:
    return "1-0" if winner == "white" else "0-1" if winner == "black" else "½-½"


def _embed_color(raw: dict, user_color: Optional[str]) -> int:
    winner = raw.get("winner")
    if not user_color or not winner:
        return 0x95A5A6  # grey (draw / unknown)
    if winner == user_color:
        return 0x2ECC71  # green
    return 0xE74C3C  # red


def _accuracy_line(raw: dict) -> str:
    acc = raw.get("accuracy") or {}
    w = acc.get("white")
    b = acc.get("black")
    if w is None and b is None:
        return ""
    return f"Accuracy — White {w if w is not None else '?'}% · Black {b if b is not None else '?'}%"


async def _send_long(channel: discord.abc.Messageable, text: str):
    """Discord has a 2000-char limit per message; chunk safely."""
    LIMIT = 1900
    while text:
        chunk = text[:LIMIT]
        if len(text) > LIMIT:
            cut = chunk.rfind("\n")
            if cut > 500:
                chunk = chunk[:cut]
        await channel.send(chunk)
        text = text[len(chunk):].lstrip("\n")


async def _send_section(
    channel: discord.abc.Messageable,
    heading: str,
    text: str,
    image_bytes: Optional[bytes],
    image_name: str,
):
    """Send one 'blog section': heading + paragraph + image, all inline."""
    body = f"### {heading}\n{text}".strip() if text else f"### {heading}"
    files = []
    if image_bytes:
        files.append(discord.File(io.BytesIO(image_bytes), filename=image_name))
    if len(body) <= 1900:
        await channel.send(content=body, files=files)
    else:
        # rare; split text first, attach image to first chunk
        await _send_long(channel, body)
        if files:
            await channel.send(files=files)


# ---------- core processing ----------

async def _post_game_blog(
    channel: discord.abc.Messageable,
    raw_summary: dict,
    sections: dict,
    key_moments: dict,
    pgn: str,
    game_id: str,
    created_at_ms: int,
):
    """Post a game as a blog-style sequence of messages."""
    user_color = key_moments.get("user_color")
    orient = user_color or "white"

    # Parse PGN once for ply counts (used for endgame range bounds).
    game_obj = board_mod.parse_pgn(pgn) if pgn else None
    plies_total = board_mod.total_plies(game_obj) if game_obj else 0

    async def _slice_or_static(start: int, end: int, fallback_fen: str,
                               fallback_last: Optional[str], frame_ms: int = 900,
                               final_hold_ms: int = 2500):
        """Render an animated slice locally; on failure return a static board image."""
        if game_obj and end >= start and local_gif.is_available():
            try:
                gif = await asyncio.to_thread(
                    local_gif.render_slice,
                    game_obj, start, end, orient, 360, frame_ms, final_hold_ms,
                )
            except Exception:
                log.exception("Local GIF render failed")
                gif = None
            if gif:
                return gif, "anim.gif"
        img = await board_mod.fetch_board_image(_http, fallback_fen, fallback_last, color=orient)
        return img, "board.gif"

    # ---- Header card (embed) — also serves as the thread anchor ----
    w = raw_summary.get("white") or {}
    bk = raw_summary.get("black") or {}
    opening = (raw_summary.get("opening") or {}).get("name") or "Unknown opening"
    when = dt.datetime.utcfromtimestamp(created_at_ms / 1000).strftime("%Y-%m-%d %H:%M UTC")
    speed = raw_summary.get("speed") or "?"

    headline = sections.get("headline") or f"{w.get('name')} vs {bk.get('name')}"
    title = headline
    embed = discord.Embed(
        title=title[:256],
        url=f"https://lichess.org/{game_id}",
        description=(
            f"**{w.get('name')}** ({w.get('rating')}) vs "
            f"**{bk.get('name')}** ({bk.get('rating')}) — **{_result_str(raw_summary.get('winner'))}**\n"
            f"_{speed} · {opening} · {when}_"
        ),
        color=_embed_color(raw_summary, user_color),
    )

    # Sparkline
    spark = board_mod.build_sparkline(raw_summary.get("ply_analysis"))
    if spark:
        embed.add_field(name="Eval flow", value=f"`{spark}`", inline=False)

    acc = _accuracy_line(raw_summary)
    if acc:
        embed.set_footer(text=acc)

    if sections.get("summary"):
        embed.add_field(name="Summary", value=sections["summary"][:1000], inline=False)

    # Final position image attached to the banner
    final_file = None
    eg = key_moments.get("endgame") or {}
    final_fen = eg.get("fen")
    final_last = eg.get("last_move")
    if final_fen:
        final_img = await board_mod.fetch_board_image(_http, final_fen, final_last, color=orient)
        if final_img:
            final_file = discord.File(io.BytesIO(final_img), filename="final.gif")
            embed.set_image(url="attachment://final.gif")

    # Send banner — try to create a thread on it for the rest of the discussion.
    send_kwargs = {"embed": embed}
    if final_file:
        send_kwargs["file"] = final_file
    banner_msg = await channel.send(**send_kwargs)

    # Build a concise thread name (Discord limit: 100 chars).
    result_str = _result_str(raw_summary.get("winner"))
    thread_name = (
        f"{headline} · {w.get('name')} vs {bk.get('name')} · {result_str}"
    )[:100]

    target: discord.abc.Messageable = channel
    if hasattr(banner_msg, "create_thread"):
        try:
            thread = await banner_msg.create_thread(
                name=thread_name,
                auto_archive_duration=1440,  # 24h
            )
            target = thread
            try:
                storage.set_thread_id(game_id, thread.id)
            except Exception:
                log.exception("Could not persist thread_id for %s", game_id)
        except Exception:
            log.exception("Could not create thread; falling back to channel posts")

    # ---- Section 1: Opening (animate the last ~6 plies into the opening pos) ----
    op = key_moments.get("opening")
    if op and op.get("fen"):
        op_ply = op.get("ply") or 0
        start = max(1, op_ply - 5)
        img, fname = await _slice_or_static(start, op_ply, op["fen"], op.get("last_move"))
        text = sections.get("opening_comment", "")
        await _send_section(target, f"♙ Opening — {op.get('label','')}", text, img, fname)

    # ---- Section 2: Midgame / critical moment (3 plies before -> 2 after) ----
    mg = key_moments.get("midgame")
    if mg and mg.get("fen"):
        mg_ply = mg.get("ply") or 0
        start = max(1, mg_ply - 3)
        end = min(plies_total or mg_ply, mg_ply + 2)
        img, fname = await _slice_or_static(start, end, mg["fen"], mg.get("last_move"))
        callout = ""
        if mg.get("eval_before") is not None or mg.get("eval_after") is not None:
            eb = board_mod.fmt_eval(mg.get("eval_before"))
            ea = board_mod.fmt_eval(mg.get("eval_after"))
            callout = f"_Eval: **{eb} → {ea}** (move {mg.get('move_number','?')})_\n"
        text = callout + sections.get("midgame_comment", "")
        await _send_section(target, f"⚔️ Midgame — {mg.get('label','Critical moment')}", text, img, fname)

        # Bonus: render the engine's recommended line as a second GIF.
        pre_fen = mg.get("pre_fen")
        cont = mg.get("engine_continuation") or []
        best = mg.get("best_san")
        if pre_fen and cont and local_gif.is_available():
            try:
                alt_gif = await asyncio.to_thread(
                    local_gif.render_variation,
                    pre_fen, cont, orient, 360, 1000, 2500, 6,
                )
            except Exception:
                log.exception("Variation GIF render failed")
                alt_gif = None
            if alt_gif:
                eng_eval = board_mod.fmt_eval(mg.get("engine_eval"))
                alt_text = (
                    f"_Engine recommends:_ **{best or '?'}** (eval {eng_eval}). "
                    f"Continuation: `{' '.join(cont[:6])}`"
                )
                await _send_section(
                    target, "🤖 Engine line", alt_text, alt_gif, "engine_line.gif",
                )

    # ---- Section 3: Endgame (last ~8 plies → checkmate / final pos) ----
    eg = key_moments.get("endgame")
    if eg and eg.get("fen"):
        eg_ply = eg.get("ply") or plies_total
        start = max(1, eg_ply - 7)
        img, fname = await _slice_or_static(
            start, eg_ply, eg["fen"], eg.get("last_move"),
            frame_ms=1100, final_hold_ms=3000,
        )
        text = sections.get("endgame_comment", "")
        await _send_section(target, f"🏁 Endgame — {eg.get('label','Final position')}", text, img, fname)

    # ---- Section 4: Strengths + Improvements + style ----
    tail_parts = []
    if sections.get("strengths"):
        tail_parts.append("### ✅ Strengths\n" + "\n".join(f"- {i}" for i in sections["strengths"]))
    if sections.get("improvements"):
        tail_parts.append("### 💡 Improvements\n" + "\n".join(f"- {i}" for i in sections["improvements"]))
    if sections.get("style_note"):
        tail_parts.append(f"### 🎨 Style\n{sections['style_note']}")
    await _send_long(target, "\n\n".join(tail_parts))


async def process_new_games():
    """Fetch new games since the last seen timestamp, analyze, store, post."""
    global _http
    assert _http is not None

    last_ms = int(storage.get_state("last_game_ms", "0") or "0")
    if last_ms == 0:
        storage.set_state("last_game_ms", str(int(time.time() * 1000)))
        log.info("First run: marking baseline, no backfill.")
        return

    since = last_ms + 1
    try:
        games = await lichess.fetch_games(_http, config.LICHESS_USERNAME, since_ms=since)
    except Exception:
        log.exception("Lichess fetch failed")
        return

    if not games:
        return

    log.info("Fetched %d new game(s)", len(games))
    games.sort(key=lambda g: g.get("createdAt", 0))
    channel = bot.get_channel(config.DISCORD_CHANNEL_ID)

    for g in games:
        gid = g.get("id")
        if not gid or storage.has_game(gid):
            continue
        try:
            summary = lichess.extract_summary_fields(g)
            mistakes = lichess.extract_key_mistakes(g, config.LICHESS_USERNAME)
            pgn = g.get("pgn", "")

            # Pick key moments (parse PGN locally — no extra API calls)
            game_obj = board_mod.parse_pgn(pgn) if pgn else None
            if game_obj:
                key_moments = board_mod.pick_key_moments(
                    game_obj, summary, config.LICHESS_USERNAME, summary.get("ply_analysis")
                )
                mg = (key_moments or {}).get("midgame") or {}
                if not mg.get("played_san"):
                    # No Lichess per-ply analysis — scan all user moves to find worst.
                    worst = await board_mod.find_worst_user_move(
                        _http, game_obj, summary, config.LICHESS_USERNAME
                    )
                    if worst and key_moments.get("midgame") is not None:
                        key_moments["midgame"].update(worst)
                    elif worst:
                        key_moments["midgame"] = worst
                else:
                    await board_mod.enrich_with_engine(_http, key_moments)
            else:
                key_moments = {"user_color": None, "opening": None, "midgame": None, "endgame": None}

            sections = await llm.analyze_game(
                _http, summary, config.LICHESS_USERNAME, key_moments,
                move_table=lichess.build_move_table(summary),
            )
            full_md = llm.sections_to_markdown(sections)
            short = sections.get("summary") or full_md[:300]

            storage.save_game(
                game_id=gid,
                created_at_ms=g.get("createdAt", int(time.time() * 1000)),
                pgn=pgn,
                raw=summary,
                summary=short,
                mistakes=mistakes,
                feedback=full_md,
                sections=sections,
                key_moments=key_moments,
            )
            storage.set_state("last_game_ms", str(g.get("createdAt", since)))

            if channel is not None:
                await _post_game_blog(
                    channel, summary, sections, key_moments, pgn, gid,
                    g.get("createdAt", int(time.time() * 1000)),
                )
            log.info("Processed game %s", gid)
        except Exception:
            log.exception("Failed processing game %s", gid)


async def run_weekly_if_due():
    """Run the weekly report once per week if there were games in the last 7 days."""
    now = dt.datetime.now()
    if now.weekday() != config.WEEKLY_DAY or now.hour != config.WEEKLY_HOUR:
        return

    last_run = storage.get_state("weekly_last_run", "0")
    last_run_ts = int(last_run)
    if time.time() - last_run_ts < 6 * 24 * 3600:  # already ran in last 6 days
        return

    week_ago_ms = int((time.time() - 7 * 24 * 3600) * 1000)
    recent = storage.get_games_since(week_ago_ms)
    if not recent:
        log.info("Weekly: no games in last 7 days, skipping.")
        storage.set_state("weekly_last_run", str(int(time.time())))
        return

    three_mo_ms = int((time.time() - 90 * 24 * 3600) * 1000)
    games = storage.get_games_since(three_mo_ms)
    summaries = []
    for g in games:
        try:
            raw = json.loads(g["raw_json"])
            mistakes = json.loads(g["mistakes"] or "[]")
            summaries.append({
                "id": g["game_id"],
                "opening": (raw.get("opening") or {}).get("name"),
                "speed": raw.get("speed"),
                "winner": raw.get("winner"),
                "accuracy": raw.get("accuracy"),
                "mistakes": mistakes,
                "summary": g["summary"],
            })
        except Exception:
            continue

    try:
        report = await llm.weekly_report(_http, summaries, config.LICHESS_USERNAME)
    except Exception:
        log.exception("Weekly LLM failed")
        return

    channel = bot.get_channel(config.DISCORD_CHANNEL_ID)
    if channel is not None:
        header = f"**Weekly review** — {len(summaries)} games (last 90 days), {len(recent)} this week.\n\n"
        await _send_long(channel, header + report)
    storage.set_state("weekly_last_run", str(int(time.time())))
    log.info("Weekly report posted.")


# ---------- background loops ----------

@tasks.loop(minutes=1)
async def weekly_loop():
    try:
        await run_weekly_if_due()
    except Exception:
        log.exception("weekly_loop error")


async def poll_loop():
    await bot.wait_until_ready()
    interval = max(1, config.POLL_INTERVAL_MINUTES) * 60
    while not bot.is_closed():
        try:
            await process_new_games()
        except Exception:
            log.exception("poll_loop error")
        await asyncio.sleep(interval)


# ---------- discord events / commands ----------

@bot.event
async def on_ready():
    global _http
    if _http is None:
        _http = aiohttp.ClientSession()
    try:
        import engine_pool
        await engine_pool.init_pool()
    except Exception:
        log.exception("engine_pool init failed; falling back to remote engine")
    log.info("Logged in as %s (channel=%s)", bot.user, config.DISCORD_CHANNEL_ID)
    if not weekly_loop.is_running():
        weekly_loop.start()
    global _poll_started
    if not _poll_started:
        _poll_started = True
        bot.loop.create_task(poll_loop())


@bot.event
async def on_message(message: discord.Message):
    # Ignore our own messages and other bots.
    if message.author.bot:
        return
    # Let the prefix-command machinery run first (so `!ask`, `!game`, etc. still work in threads).
    await bot.process_commands(message)

    # If we already invoked a prefix command, don't double-handle.
    content = (message.content or "").strip()
    if not content or content.startswith(bot.command_prefix if isinstance(bot.command_prefix, str) else "!"):
        return

    # Only auto-answer inside threads we created for a stored game.
    ch = message.channel
    if not isinstance(ch, discord.Thread):
        return
    try:
        g = storage.get_game_by_thread_id(ch.id)
    except Exception:
        log.exception("get_game_by_thread_id failed")
        return
    if not g:
        return

    async with ch.typing():
        await _answer_game_question(ch, g, content)


@bot.command(name="last")
async def cmd_last(ctx: commands.Context):
    g = storage.get_last_game()
    if not g:
        await ctx.send("No games stored yet.")
        return
    await _replay_stored(ctx.channel, g)


@bot.command(name="game")
async def cmd_game(ctx: commands.Context, game_id: str):
    g = storage.get_game(game_id)
    if not g:
        await ctx.send(f"No stored analysis for `{game_id}`.")
        return
    await _replay_stored(ctx.channel, g)


async def _replay_stored(channel: discord.abc.Messageable, g: dict):
    """Re-render a stored game using the blog flow (re-fetches images)."""
    raw = json.loads(g["raw_json"])
    sections = json.loads(g["sections"]) if g.get("sections") else None
    key_moments = json.loads(g["key_moments"]) if g.get("key_moments") else None
    if sections and key_moments:
        await _post_game_blog(
            channel, raw, sections, key_moments, g["pgn"], g["game_id"], g["created_at"]
        )
    else:
        # Legacy row stored before blog mode — just dump the markdown.
        await _send_long(channel, (g.get("feedback") or g.get("summary") or "(no data)"))


@bot.command(name="ask")
async def cmd_ask(ctx: commands.Context, game_id: str, *, question: str):
    g = storage.get_game(game_id)
    if not g:
        await ctx.send(f"No stored analysis for `{game_id}`.")
        return
    async with ctx.typing():
        await _answer_game_question(ctx.channel, g, question)


async def _answer_game_question(
    channel: discord.abc.Messageable, g: dict, question: str
) -> None:
    """Run the LLM Q&A pipeline for a stored game and post the answer + GIFs."""
    game_id = g["game_id"]
    try:
        raw = json.loads(g["raw_json"])
        user_color = lichess.user_color(raw, config.LICHESS_USERNAME)
        move_table = lichess.build_move_table(raw)
        key_moments = json.loads(g["key_moments"]) if g.get("key_moments") else None

        # Re-enrich if key_moments is stale (missing engine data from old stored games).
        mg = (key_moments or {}).get("midgame") or {}
        if not mg.get("engine_continuation"):
            log.info("ask: key_moments stale for %s — re-running pick+enrich", game_id)
            import io as _io
            import chess.pgn as _pgn
            parsed = _pgn.read_game(_io.StringIO(g["pgn"]))
            if parsed:
                key_moments = board_mod.pick_key_moments(
                    parsed, raw, config.LICHESS_USERNAME,
                    lichess_analysis=raw.get("ply_analysis"),
                )
                new_mg = (key_moments or {}).get("midgame") or {}
                if not new_mg.get("played_san") and parsed:
                    log.info("ask: no Lichess analysis — scanning user moves for worst move")
                    worst = await board_mod.find_worst_user_move(
                        _http, parsed, raw, config.LICHESS_USERNAME
                    )
                    if worst and key_moments and key_moments.get("midgame") is not None:
                        key_moments["midgame"].update(worst)
                    elif worst and key_moments:
                        key_moments["midgame"] = worst
                else:
                    key_moments = await board_mod.enrich_with_engine(_http, key_moments)
                storage.update_key_moments(game_id, key_moments)
                log.info("ask: re-enriched and saved key_moments for %s", game_id)

        result = await llm.answer_question(
            _http,
            pgn=g["pgn"],
            feedback=g["feedback"] or "",
            raw_summary=raw,
            question=question,
            username=config.LICHESS_USERNAME,
            user_color=user_color,
            move_table=move_table,
            key_moments=key_moments,
        )
    except Exception:
        log.exception("ask failed")
        await channel.send("LLM call failed, try again later.")
        return

    answer = result.get("text", "") if isinstance(result, dict) else str(result)
    engine_calls = result.get("engine_calls", []) if isinstance(result, dict) else []
    critical_moment = result.get("critical_moment") if isinstance(result, dict) else None

    await _send_long(channel, f"**Q:** {question}\n\n{answer}")

    # Render the played-move + engine-recommendation GIFs only when the answer
    # actually discusses the critical moment (mentions the played SAN, the
    # engine's recommended SAN, or the move number).
    answer_lower = (answer or "").lower()
    cm = critical_moment or {}
    cm_played = cm.get("played_san")
    cm_best = cm.get("best_san")
    cm_move_no = cm.get("move_number")
    cm_referenced = bool(
        (cm_played and cm_played.lower() in answer_lower)
        or (cm_best and cm_best.lower() in answer_lower)
        or (cm_move_no and f"move {cm_move_no}" in answer_lower)
    )

    if cm_referenced and cm.get("fen") and cm_played and local_gif.is_available():
        try:
            played_gif = await asyncio.to_thread(
                local_gif.render_variation,
                cm["fen"], [cm_played], user_color or "white", 360, 1500, 3000, 1,
            )
        except Exception:
            log.exception("Played-move GIF render failed for ask")
            played_gif = None
        if played_gif:
            ev_b = cm.get("eval_before")
            ev_a = cm.get("eval_after")
            swing_str = ""
            if ev_b is not None and ev_a is not None:
                swing_str = f" (eval {ev_b:+.2f} → {ev_a:+.2f})"
            caption = f"❌ What you played: **{cm_played}**{swing_str}"
            try:
                file = discord.File(io.BytesIO(played_gif), filename="played_move.gif")
                await channel.send(content=caption, file=file)
            except Exception:
                log.exception("Failed to send played-move GIF")

        cont = cm.get("continuation") or []
        if cont:
            try:
                eng_gif = await asyncio.to_thread(
                    local_gif.render_variation,
                    cm["fen"], cont, user_color or "white", 360, 1000, 2500, 6,
                )
            except Exception:
                log.exception("Engine-line GIF render failed for ask")
                eng_gif = None
            if eng_gif:
                eng_eval = cm.get("engine_eval")
                eval_str = ""
                if eng_eval is not None:
                    eval_str = f" (eval {'+' if eng_eval >= 0 else ''}{eng_eval:.2f})"
                caption = f"✅ Engine recommends: **{cm_best or '?'}**{eval_str}"
                try:
                    file = discord.File(io.BytesIO(eng_gif), filename="engine_line.gif")
                    await channel.send(content=caption, file=file)
                except Exception:
                    log.exception("Failed to send engine-line GIF")

    if engine_calls and local_gif.is_available():
        orient = user_color or "white"
        for i, call in enumerate(engine_calls[:3]):
            cont = call.get("continuation") or []
            fen = call.get("fen")
            if not fen or not cont:
                continue
            try:
                gif = await asyncio.to_thread(
                    local_gif.render_variation,
                    fen, cont, orient, 360, 1000, 2500, 6,
                )
            except Exception:
                log.exception("Variation GIF render failed for ask")
                gif = None
            if not gif:
                continue
            san = call.get("san") or "?"
            ev = call.get("eval")
            mate = call.get("mate")
            if mate is not None:
                eval_str = f"#{mate}"
            elif ev is not None:
                eval_str = f"{'+' if ev >= 0 else ''}{ev:.2f}"
            else:
                eval_str = "?"
            caption = f"✅ Engine recommends: **{san}** (eval {eval_str})"
            try:
                file = discord.File(io.BytesIO(gif), filename=f"engine_line_{i+1}.gif")
                await channel.send(content=caption, file=file)
            except Exception:
                log.exception("Failed to send variation GIF")


@bot.command(name="board")
async def cmd_board(ctx: commands.Context, game_id: str, move: int):
    """Show the board image after a given full-move number for a stored game."""
    g = storage.get_game(game_id)
    if not g:
        await ctx.send(f"No stored analysis for `{game_id}`.")
        return
    if move < 1:
        await ctx.send("Move number must be >= 1.")
        return
    game_obj = board_mod.parse_pgn(g["pgn"])
    if not game_obj:
        await ctx.send("Couldn't parse PGN for that game.")
        return
    # full move N -> ply 2N (after black's reply); fall back if game shorter
    target_ply = min(2 * move, board_mod.total_plies(game_obj))
    pos = board_mod.position_at_ply(game_obj, target_ply)
    if not pos:
        await ctx.send("Couldn't find that position.")
        return
    raw = json.loads(g["raw_json"])
    km = json.loads(g["key_moments"]) if g.get("key_moments") else {}
    user_color = km.get("user_color") or "white"
    img = await board_mod.fetch_board_image(_http, pos["fen"], pos.get("last_move"), color=user_color)
    if not img:
        await ctx.send("Couldn't fetch board image.")
        return
    await ctx.send(
        content=f"`{game_id}` — after move {move} ({pos['side']} just moved)",
        file=discord.File(io.BytesIO(img), filename=f"{game_id}_m{move}.gif"),
    )


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    await ctx.send(
        "**Lichess AI Coach**\n"
        "`!last` — replay the last analyzed game (with board images)\n"
        "`!game <id>` — replay a stored game\n"
        "`!ask <id> <question>` — ask anything about a stored game\n"
        "`!board <id> <move>` — show the board after move N of a stored game"
    )


# ---------- entrypoint ----------

def main():
    storage.init()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
