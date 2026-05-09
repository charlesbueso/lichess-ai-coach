"""Per-tenant game processing: fetch new Lichess games, analyze, store, post.

This module is the SaaS-flavoured equivalent of `process_new_games` from the
single-tenant `main.py`. It reuses the OSS modules (`lichess`, `llm`, `board`,
`local_gif`) so behaviour stays consistent with the open-source coach.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import time
from typing import Optional

import aiohttp
import discord

import board as board_mod
import lichess
import llm
import local_gif
from saas import app_config, db
from saas.rate_limit import LICHESS_BUCKET

log = logging.getLogger("coach.saas")


# ---------- Discord helpers (lifted/inlined from main.py) -----------------

def _result_str(winner) -> str:
    return "1-0" if winner == "white" else "0-1" if winner == "black" else "½-½"


def _embed_color(raw: dict, user_color: Optional[str]) -> int:
    winner = raw.get("winner")
    if not user_color or not winner:
        return 0x95A5A6
    return 0x2ECC71 if winner == user_color else 0xE74C3C


def _accuracy_line(raw: dict) -> str:
    acc = raw.get("accuracy") or {}
    w, b = acc.get("white"), acc.get("black")
    if w is None and b is None:
        return ""
    return f"Accuracy — White {w if w is not None else '?'}% · Black {b if b is not None else '?'}%"


async def _send_long(channel: discord.abc.Messageable, text: str):
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
    body = f"### {heading}\n{text}".strip() if text else f"### {heading}"
    files = []
    if image_bytes:
        files.append(discord.File(io.BytesIO(image_bytes), filename=image_name))
    if len(body) <= 1900:
        await channel.send(content=body, files=files)
    else:
        await _send_long(channel, body)
        if files:
            await channel.send(files=files)


# ---------- core: post one game as a blog ---------------------------------

async def post_game_blog(
    http: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    raw_summary: dict,
    sections: dict,
    key_moments: dict,
    pgn: str,
    game_id: str,
    created_at_ms: int,
) -> Optional[int]:
    """Post a game as the familiar header + 3 sections + footer.

    Returns the created thread id, or None on failure / if the channel
    doesn't support threads.
    """
    user_color = (key_moments or {}).get("user_color")
    orient = user_color or "white"

    game_obj = board_mod.parse_pgn(pgn) if pgn else None
    plies_total = board_mod.total_plies(game_obj) if game_obj else 0

    async def _slice_or_static(start: int, end: int, fallback_fen: str,
                               fallback_last: Optional[str], frame_ms: int = 900,
                               final_hold_ms: int = 2500):
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
        img = await board_mod.fetch_board_image(http, fallback_fen, fallback_last, color=orient)
        return img, "board.gif"

    w  = raw_summary.get("white") or {}
    bk = raw_summary.get("black") or {}
    opening = (raw_summary.get("opening") or {}).get("name") or "Unknown opening"
    _CST = dt.timezone(dt.timedelta(hours=-6))
    when = dt.datetime.fromtimestamp(created_at_ms / 1000, tz=_CST).strftime("%Y-%m-%d %H:%M CST")
    speed = raw_summary.get("speed") or "?"

    headline = sections.get("headline") or f"{w.get('name')} vs {bk.get('name')}"
    embed = discord.Embed(
        title=headline[:256],
        url=f"https://lichess.org/{game_id}",
        description=(
            f"**{w.get('name')}** ({w.get('rating')}) vs "
            f"**{bk.get('name')}** ({bk.get('rating')}) — **{_result_str(raw_summary.get('winner'))}**\n"
            f"_{speed} · {opening} · {when}_"
        ),
        color=_embed_color(raw_summary, user_color),
    )
    spark = board_mod.build_sparkline(raw_summary.get("ply_analysis"))
    if spark:
        embed.add_field(name="Eval flow", value=f"`{spark}`", inline=False)
    acc = _accuracy_line(raw_summary)
    if acc:
        embed.set_footer(text=acc)
    if sections.get("summary"):
        embed.add_field(name="Summary", value=sections["summary"][:1000], inline=False)

    final_file = None
    eg = (key_moments or {}).get("endgame") or {}
    final_fen = eg.get("fen")
    final_last = eg.get("last_move")
    if final_fen:
        final_img = await board_mod.fetch_board_image(http, final_fen, final_last, color=orient)
        if final_img:
            final_file = discord.File(io.BytesIO(final_img), filename="final.gif")
            embed.set_image(url="attachment://final.gif")

    send_kwargs = {"embed": embed}
    if final_file:
        send_kwargs["file"] = final_file
    banner_msg = await channel.send(**send_kwargs)

    result_str = _result_str(raw_summary.get("winner"))
    thread_name = (
        f"{headline} · {w.get('name')} vs {bk.get('name')} · {result_str}"
    )[:100]

    target: discord.abc.Messageable = channel
    thread_id: Optional[int] = None
    if hasattr(banner_msg, "create_thread"):
        try:
            thread = await banner_msg.create_thread(
                name=thread_name, auto_archive_duration=1440,
            )
            target = thread
            thread_id = thread.id
        except Exception:
            log.exception("Could not create thread; falling back to channel posts")

    # --- Opening
    op = (key_moments or {}).get("opening")
    if op and op.get("fen"):
        op_ply = op.get("ply") or 0
        start = max(1, op_ply - 5)
        img, fname = await _slice_or_static(start, op_ply, op["fen"], op.get("last_move"))
        await _send_section(
            target, f"♙ Opening — {op.get('label','')}",
            sections.get("opening_comment", ""), img, fname,
        )

    # --- Midgame
    mg = (key_moments or {}).get("midgame")
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
        await _send_section(
            target, f"⚔️ Midgame — {mg.get('label','Critical moment')}",
            callout + sections.get("midgame_comment", ""), img, fname,
        )

        # Bonus: render the engine's recommended line as a second GIF so the
        # user can SEE what they should have played, not just read the SAN.
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

    # --- Endgame
    if eg and eg.get("fen"):
        eg_ply = eg.get("ply") or plies_total
        start = max(1, eg_ply - 7)
        img, fname = await _slice_or_static(
            start, eg_ply, eg["fen"], eg.get("last_move"),
            frame_ms=1100, final_hold_ms=3000,
        )
        await _send_section(
            target, f"🏁 Endgame — {eg.get('label','Final position')}",
            sections.get("endgame_comment", ""), img, fname,
        )

    # --- Footer
    tail_parts = []
    if sections.get("strengths"):
        tail_parts.append("### ✅ Strengths\n" + "\n".join(f"- {i}" for i in sections["strengths"]))
    if sections.get("improvements"):
        tail_parts.append("### 💡 Improvements\n" + "\n".join(f"- {i}" for i in sections["improvements"]))
    if sections.get("style_note"):
        tail_parts.append(f"### 🎨 Style\n{sections['style_note']}")
    tail_parts.append(
        f"### 🆔 Game ID\n"
        f"`{game_id}` — view on Lichess: <https://lichess.org/{game_id}>\n"
        f"Use this id with `/ask question:<your question> game_id:{game_id}` "
        f"or `/board game_id:{game_id} move:<n>`.\n"
        f"_(Tip: inside this thread you can just use `/ask` without a game id.)_"
    )
    await _send_long(target, "\n\n".join(tail_parts))

    return thread_id


# ---------- per-tenant pipeline -------------------------------------------

async def process_tenant(
    http: aiohttp.ClientSession,
    bot: discord.Client,
    tenant: dict,
) -> None:
    """Fetch new Lichess games for a single tenant, analyze, store, post."""
    tenant_id = tenant["id"]
    username  = tenant["lichess_username"]
    channel_id = tenant["discord_channel_id"]
    last_ms   = int(tenant["last_game_ms"] or 0)

    if last_ms == 0:
        # First poll after /setup. Look back 30 minutes so a game the user
        # played between subscribing and finishing setup still gets picked up.
        # We don't backfill arbitrary history (would be expensive on a fresh
        # install with active accounts).
        baseline = int(time.time() * 1000) - 30 * 60 * 1000
        await db.update_last_game_ms(tenant_id, baseline)
        log.info(
            "[t=%s] baseline set with 30-min lookback; will pick up any "
            "game finished in the last 30 minutes on the next pass",
            tenant_id,
        )
        return

    await LICHESS_BUCKET.acquire()
    try:
        games = await lichess.fetch_games(http, username, since_ms=last_ms + 1)
    except Exception:
        log.exception("[t=%s] Lichess fetch failed", tenant_id)
        await db.touch_poll(tenant_id)
        return

    if not games:
        await db.touch_poll(tenant_id)
        return

    log.info("[t=%s] %d new game(s)", tenant_id, len(games))
    games.sort(key=lambda g: g.get("createdAt", 0))

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            log.exception("[t=%s] cannot fetch channel %s", tenant_id, channel_id)
            await db.touch_poll(tenant_id)
            return

    today = dt.date.today()

    for g in games:
        gid = g.get("id")
        if not gid:
            continue
        g_ms = int(g.get("createdAt", last_ms))

        if await db.has_game(tenant_id, gid):
            # Already saved in a previous (possibly partially-failed) run.
            # Advance the cursor anyway so we don't refetch this game forever.
            log.info("[t=%s] game %s already in DB; advancing cursor", tenant_id, gid)
            await db.update_last_game_ms(tenant_id, g_ms)
            continue

        # Quota: count this analysis attempt; skip if over cap.
        under_cap = await db.consume_game_quota(
            tenant_id, today, app_config.GAMES_PER_DAY_PER_TENANT,
        )
        if not under_cap:
            log.info("[t=%s] daily game quota hit; skipping rest", tenant_id)
            # Advance cursor so we don't reprocess these tomorrow.
            await db.update_last_game_ms(tenant_id, g_ms)
            break

        try:
            await _analyze_and_post(http, channel, tenant, g)
            await db.update_last_game_ms(tenant_id, g_ms)
        except discord.Forbidden as e:
            log.error(
                "[t=%s] Discord refused post in channel %s (Missing Access / "
                "permissions). Advancing cursor to skip game %s. "
                "Fix: grant the bot View Channel, Send Messages, Embed Links, "
                "Attach Files, Create Public Threads, and Send Messages in "
                "Threads on that channel. Detail: %s",
                tenant_id, tenant.get("discord_channel_id"), gid, e,
            )
            # Permission errors won't fix themselves on retry; advance so we
            # don't burn LLM/engine cycles re-processing the same game.
            await db.update_last_game_ms(tenant_id, g_ms)
        except Exception:
            log.exception("[t=%s] failed processing game %s", tenant_id, gid)
            # Don't advance cursor on transient failure so we retry next cycle.

    await db.touch_poll(tenant_id)


async def _analyze_and_post(
    http: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    tenant: dict,
    g: dict,
) -> None:
    tenant_id = tenant["id"]
    username  = tenant["lichess_username"]
    gid       = g["id"]

    summary  = lichess.extract_summary_fields(g)
    pgn      = g.get("pgn", "")

    game_obj = board_mod.parse_pgn(pgn) if pgn else None
    if game_obj:
        key_moments = board_mod.pick_key_moments(
            game_obj, summary, username, summary.get("ply_analysis"),
        )
        mg = (key_moments or {}).get("midgame") or {}
        if not mg.get("played_san"):
            worst = await board_mod.find_worst_user_move(http, game_obj, summary, username)
            if worst and key_moments.get("midgame") is not None:
                key_moments["midgame"].update(worst)
            elif worst:
                key_moments["midgame"] = worst
        else:
            await board_mod.enrich_with_engine(http, key_moments)
    else:
        key_moments = {"user_color": None, "opening": None, "midgame": None, "endgame": None}

    sections = await llm.analyze_game(
        http, summary, username, key_moments,
        move_table=lichess.build_move_table(summary),
    )
    full_md = llm.sections_to_markdown(sections)
    short = sections.get("summary") or full_md[:300]

    await db.save_game(
        tenant_id=tenant_id,
        game_id=gid,
        created_at_ms=int(g.get("createdAt", int(time.time() * 1000))),
        pgn=pgn,
        raw=summary,
        summary=short,
        feedback=full_md,
        sections=sections,
        key_moments=key_moments,
    )

    thread_id = await post_game_blog(
        http, channel, summary, sections, key_moments, pgn, gid,
        int(g.get("createdAt", int(time.time() * 1000))),
    )
    if thread_id is not None:
        await db.set_thread_id(tenant_id, gid, thread_id)
    log.info("[t=%s] posted game %s", tenant_id, gid)


# ---------- /ask helper ---------------------------------------------------

async def answer_question(
    http: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    tenant: dict,
    g: dict,
    question: str,
) -> None:
    tenant_id = tenant["id"]
    username  = tenant["lichess_username"]
    raw       = g["raw_json"]
    user_color = lichess.user_color(raw, username)
    move_table = lichess.build_move_table(raw)
    key_moments = g.get("key_moments")

    try:
        result = await llm.answer_question(
            http,
            pgn=g["pgn"],
            feedback=g.get("feedback") or "",
            raw_summary=raw,
            question=question,
            username=username,
            user_color=user_color,
            move_table=move_table,
            key_moments=key_moments,
        )
    except Exception:
        log.exception("[t=%s] /ask failed for %s", tenant_id, g["game_id"])
        await channel.send("LLM call failed, try again later.")
        return

    answer = result.get("text", "") if isinstance(result, dict) else str(result)
    await _send_long(channel, f"**Q:** {question}\n\n{answer}")
