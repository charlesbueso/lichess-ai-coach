"""Groq LLM client. Uses OpenAI-compatible chat completions endpoint."""
import aiohttp
import asyncio
import json
import logging
import re
from typing import Optional

import config

log = logging.getLogger("coach.llm")

GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_URL = f"{GROQ_BASE}/chat/completions"
GROQ_MODELS_URL = f"{GROQ_BASE}/models"

# Ordered preference list. Configure via GROQ_MODELS="a,b,c" to override.
# We try config.GROQ_MODEL first, then fall through these on
# model_decommissioned / model_not_found.
_DEFAULT_PREFERENCES = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "llama-3.2-90b-text-preview",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


def _preference_list() -> list[str]:
    raw = (getattr(config, "GROQ_MODELS", "") or "").strip()
    if raw:
        prefs = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        prefs = list(_DEFAULT_PREFERENCES)
    primary = (config.GROQ_MODEL or "").strip()
    if primary and primary not in prefs:
        prefs.insert(0, primary)
    elif primary:
        prefs.remove(primary)
        prefs.insert(0, primary)
    return prefs


# Cached working model for this process. Updated on successful fallback so we
# stop hammering a decommissioned model on every call.
_active_model: Optional[str] = None
_models_cache: tuple[float, set[str]] | None = None  # (expires_at_monotonic, ids)
_fallback_lock = asyncio.Lock()


async def _list_available_models(session: aiohttp.ClientSession) -> set[str]:
    """Fetch the current set of model IDs Groq is serving. Cached for 5 min."""
    global _models_cache
    loop = asyncio.get_event_loop()
    now = loop.time()
    if _models_cache and _models_cache[0] > now:
        return _models_cache[1]
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    try:
        async with session.get(GROQ_MODELS_URL, headers=headers, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception:
        log.exception("Failed to list Groq models; using stale cache if any")
        return _models_cache[1] if _models_cache else set()
    ids = {m.get("id") for m in (data.get("data") or []) if m.get("id")}
    # Filter to chat-capable when Groq exposes that hint (best-effort).
    chat_ids: set[str] = set()
    for m in data.get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        # Groq returns `active: true` for usable models.
        if m.get("active") is False:
            continue
        chat_ids.add(mid)
    chat_ids = chat_ids or ids
    _models_cache = (now + 300, chat_ids)
    return chat_ids


async def _pick_fallback_model(session: aiohttp.ClientSession,
                               failed: str) -> Optional[str]:
    """Pick the next preferred model that Groq currently serves."""
    available = await _list_available_models(session)
    if not available:
        return None
    for cand in _preference_list():
        if cand == failed:
            continue
        if cand in available:
            return cand
    # Last-ditch: any active model that mentions llama / mixtral / gemma.
    for mid in sorted(available):
        if any(t in mid.lower() for t in ("llama", "mixtral", "gemma")):
            if mid != failed:
                return mid
    return None


def _is_model_error(status: int, body: str) -> bool:
    if status not in (400, 404):
        return False
    b = body.lower()
    return any(t in b for t in (
        "model_decommissioned", "model_not_found",
        "decommissioned", "no longer supported", "does not exist",
        "the model `", "invalid model",
    ))


def _is_json_validate_failed(status: int, body: str) -> bool:
    return status == 400 and "json_validate_failed" in body.lower()


def _synth_response_from_failed_generation(body: str) -> Optional[dict]:
    """Groq returns the raw model output in `failed_generation` when strict
    JSON mode rejects it. Try to recover the partial output so callers can
    salvage it via `_extract_json`."""
    try:
        err = json.loads(body)
        gen = err.get("error", {}).get("failed_generation")
        if not isinstance(gen, str) or not gen.strip():
            return None
        return {
            "choices": [
                {"message": {"role": "assistant", "content": gen}}
            ],
            "_recovered_from": "failed_generation",
        }
    except Exception:
        return None


async def _chat_messages(
    session: aiohttp.ClientSession,
    messages: list,
    max_tokens: int = 800,
    temperature: float = 0.4,
    json_mode: bool = False,
    tools: Optional[list] = None,
) -> dict:
    """Low-level: send a messages list to Groq, return the raw response dict.

    On a model-related 400/404, transparently fall back to the next preferred
    model that Groq currently lists as active, cache it, and retry once.

    On a `json_validate_failed` 400, retry once without strict JSON mode (the
    caller's `_extract_json` is tolerant). If that also fails, recover the
    model's raw partial output from `failed_generation` so the caller's
    extractor still gets a chance.
    """
    global _active_model
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    def _build_payload(model: str, *, force_json: bool) -> dict:
        p: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if force_json:
            p["response_format"] = {"type": "json_object"}
        if tools:
            p["tools"] = tools
            p["tool_choice"] = "auto"
        return p

    model = _active_model or config.GROQ_MODEL
    use_json = json_mode
    json_retry_used = False
    for attempt in range(3):
        payload = _build_payload(model, force_json=use_json)
        async with session.post(GROQ_URL, headers=headers, json=payload, timeout=60) as r:
            if r.status < 400:
                if _active_model != model:
                    _active_model = model  # remember success
                return await r.json()
            body = await r.text()

            # Model decommissioned / unknown — pick a fallback and retry once.
            if not json_retry_used and _is_model_error(r.status, body):
                async with _fallback_lock:
                    new_model = await _pick_fallback_model(session, model)
                if new_model and new_model != model:
                    log.warning(
                        "Groq model %r rejected (%s). Falling back to %r. Detail: %s",
                        model, r.status, new_model, body[:300],
                    )
                    _active_model = new_model
                    model = new_model
                    continue

            # Strict JSON mode failed — drop the constraint and retry once.
            if use_json and not json_retry_used and _is_json_validate_failed(r.status, body):
                log.warning(
                    "Groq json_validate_failed on model %r; retrying without "
                    "strict JSON mode. Detail: %s", model, body[:300],
                )
                use_json = False
                json_retry_used = True
                continue

            # Last-ditch: recover the partial generation from the error body
            # so callers using _extract_json can still salvage it.
            if _is_json_validate_failed(r.status, body):
                recovered = _synth_response_from_failed_generation(body)
                if recovered:
                    log.warning(
                        "Groq rejected JSON twice; returning recovered "
                        "failed_generation for tolerant parsing.",
                    )
                    return recovered

            raise aiohttp.ClientResponseError(
                r.request_info,
                r.history,
                status=r.status,
                message=f"Groq {r.status} (model={model}): {body[:600]}",
                headers=r.headers,
            )
    raise RuntimeError("Groq chat: exhausted retries without response")


async def chat(
    session: aiohttp.ClientSession,
    system: str,
    user: str,
    max_tokens: int = 800,
    temperature: float = 0.4,
    json_mode: bool = False,
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    data = await _chat_messages(session, messages, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)
    return data["choices"][0]["message"]["content"].strip()


COACH_SYSTEM = (
    "You are a friendly, sharp chess coach. Be concise, concrete and human. "
    "Avoid generic advice. Refer to actual moves/phases when possible. "
    "Use simple Markdown. Do NOT invent engine evaluations you weren't given."
)


def _extract_json(text: str) -> dict:
    """Best-effort: parse JSON, tolerating code fences / extra prose / common
    malformations from instruction-tuned models (truncation, unquoted string
    values, trailing commas)."""
    text = (text or "").strip()
    if not text:
        return {}
    # Strip code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Fast path
    try:
        return json.loads(text)
    except Exception:
        pass
    # Isolate the largest {...} block
    m = re.search(r"\{.*\}", text, re.S)
    candidate = m.group(0) if m else text
    # Try once as-is
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # Heuristic key-value extractor: scan for `"key": value-or-string-until-next-key-or-end`
    # Works on outputs like:  "summary": ChessCharli faced ... "opening_comment": ...
    out: dict = {}
    # Build a list of (key, span_start) pairs by finding each "key":
    pat = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*', re.S)
    matches = list(pat.finditer(candidate))
    for i, mm in enumerate(matches):
        key = mm.group(1)
        val_start = mm.end()
        val_end = matches[i + 1].start() if i + 1 < len(matches) else len(candidate)
        raw_val = candidate[val_start:val_end].strip()
        # Trim trailing commas / closing braces / quotes
        raw_val = raw_val.rstrip(",}] \n\r\t")
        if not raw_val:
            continue
        # Try parsing as JSON value first (handles strings, numbers, arrays, bools).
        parsed = None
        try:
            parsed = json.loads(raw_val)
        except Exception:
            pass
        if parsed is None:
            # Maybe a quoted string missing its closing quote
            if raw_val.startswith('"'):
                inner = raw_val[1:]
                if inner.endswith('"'):
                    inner = inner[:-1]
                parsed = inner
            elif raw_val.startswith("["):
                # Try to recover an array of strings
                try:
                    parsed = json.loads(raw_val.rstrip(",") + "]" if not raw_val.endswith("]") else raw_val)
                except Exception:
                    parsed = []
            else:
                # Bare unquoted string — treat as plain text up to end
                parsed = raw_val
        out[key] = parsed
    return out


# UCI move pattern: from-square + to-square (+ optional promotion piece).
_UCI_RE = re.compile(r"\b([a-h][1-8][a-h][1-8][nbrq]?)\b")


def _strip_uci(text: str) -> str:
    """Replace any UCI-style move tokens (e.g. f3e2, e2e4, g1f3) with [move]."""
    if not text:
        return text
    return _UCI_RE.sub("[move]", text)


_UCI_LEAK_KEYS = {"last_move", "lastMove", "uci"}


def _scrub_uci_fields(obj):
    """Recursively drop fields whose names suggest UCI move encoding."""
    if isinstance(obj, dict):
        return {k: _scrub_uci_fields(v) for k, v in obj.items() if k not in _UCI_LEAK_KEYS}
    if isinstance(obj, list):
        return [_scrub_uci_fields(v) for v in obj]
    return obj


def _scrub_summary(summary: dict) -> dict:
    """Drop heavy / UCI-leaking fields from the raw summary before sending."""
    if not isinstance(summary, dict):
        return summary
    drop = {"pgn", "ply_analysis", "moves"}  # moves is SAN but redundant w/ move_table
    out = {k: v for k, v in summary.items() if k not in drop}
    return _scrub_uci_fields(out)


async def analyze_game(
    session: aiohttp.ClientSession,
    summary: dict,
    username: str,
    key_moments: dict,
    move_table: Optional[list] = None,
) -> dict:
    """Returns a dict with sectioned coaching feedback for blog-style display."""
    system = (
        COACH_SYSTEM
        + "\nYou MUST respond with a single valid JSON object, no prose, no code fences. "
        "Every string value MUST be wrapped in double quotes. Unquoted values are INVALID "
        "and will cause the response to be rejected."
    )
    user_color = key_moments.get("user_color") or "?"
    opponent = "black" if user_color == "white" else "white"
    opp_name = (summary.get(opponent) or {}).get("name", "opponent")

    # Strip UCI fields from key_moments so the LLM only sees SAN.
    # Otherwise it tends to mix UCI ("f3e2") and SAN ("Be2") for the SAME move.
    safe_key_moments = _scrub_uci_fields(key_moments)
    # Trim raw summary to fields that don't leak UCI / PGN.
    safe_summary = _scrub_summary(summary)

    midgame = key_moments.get("midgame") or {}
    mid_played = midgame.get("played_san")
    mid_best = midgame.get("best_san")
    mid_comment = midgame.get("lichess_comment")
    mid_move_no = midgame.get("move_number")

    endgame = key_moments.get("endgame") or {}
    end_last_moves = endgame.get("last_moves") or []
    end_status = endgame.get("status")
    end_winner = endgame.get("winner")

    user_msg = (
        f"USER (the player you are coaching): {username} — playing {user_color.upper()}.\n"
        f"OPPONENT: {opp_name} — playing {opponent.upper()}.\n"
        f"Never confuse these. All advice is for {username}.\n\n"
        f"Game data (JSON):\n{json.dumps(safe_summary)[:3000]}\n\n"
        f"Key moments selected:\n{json.dumps(safe_key_moments)[:1500]}\n\n"
        f"Move table (ground truth — eval in pawns; comment may contain Lichess's best move):\n"
        f"{json.dumps(move_table or [])[:5000]}\n\n"
        + (
            "CRITICAL MOMENT — STRICT FACTS (use ONLY these tokens):\n"
            f"- Move {mid_move_no}: {username} played `{mid_played}`.\n"
            + (
                f"- Lichess's best move (validated legal in that position): `{mid_best}`.\n"
                if mid_best else
                "- No validated best-move SAN available. Do NOT invent a specific move. "
                "Instead describe concretely WHY the played move was bad: what threat did it miss, "
                "what square or piece did it neglect, what did the opponent gain as a result?\n"
            )
            + (f"- Original comment: \"{mid_comment}\"\n" if mid_comment else "")
            + "\n"
            if mid_played else ""
        )
        + (
            "ENDGAME — STRICT FACTS:\n"
            f"- Result: {end_winner or 'draw'} ({end_status or 'unknown'}).\n"
            f"- Last moves played (SAN): {', '.join(end_last_moves) or '(none)'}.\n"
            "- These SANs are the ONLY moves you may reference in the endgame_comment. "
            "Do NOT invent piece placements, squares, or motifs that aren't visible "
            "in those SANs. If you can't say something concrete from them, just "
            "describe how the result was reached at a high level.\n\n"
        )
        + "NOTATION RULES (CRITICAL):\n"
        "- Use SAN ONLY (e.g., `Nf3`, `Be2`, `O-O`, `exd4`, `Qxh7+`).\n"
        "- NEVER write coordinate / UCI notation like `f3e2`, `e2e4`, `g1f3`.\n"
        "- `Be2` and `f3e2` are the SAME move — never describe them as alternatives.\n"
        "- If two notations look like the same move (one starts with a piece letter, "
        "the other is 4 lowercase chars / digits), pick the SAN form.\n\n"
        + "Return JSON with EXACTLY these keys:\n"
        '  "headline": one short punchy creative title (max 90 chars) — no markdown. '
        'Be original and witty: channel a chess commentator, use dramatic flair, wordplay, or a memorable phrase that captures HOW the game unfolded (e.g. result, opening, blunder, comeback). '
        'Avoid generic templates like "Tough loss in X" or "Win with Y". Each headline should feel unique.\n'
        '  "summary": 2-3 sentences on how the game went for the player.\n'
        '  "opening_comment": 1-2 sentences about the opening choice and resulting structure. '
        "Mention an actual move from the table if relevant.\n"
        '  "midgame_comment": 2-3 sentences about the critical moment. '
        "Use ONLY the played SAN and (if provided) the validated best SAN above — "
        "never any other move, never UCI form. Briefly explain WHY the better move would have helped.\n"
        '  "endgame_comment": 1-2 sentences about how the game finished, citing only '
        "moves from the 'Last moves played' list above.\n"
        '  "strengths": array of 2-3 SPECIFIC things the player did well in THIS game. '
        "Each must reference a concrete move number, a phase, an opening choice, or an evaluation swing "
        "from the data. No generic praise like 'good calculation'.\n"
        '  "improvements": array of 2-3 SPECIFIC actionable items grounded in THIS game. '
        "Each must tie to a concrete move/judgment from the move table or a clear pattern visible in it. "
        "Forbidden: generic advice like 'improve pawn structure' without naming where it broke down.\n"
        '  "style_note": one short observation about playing style/patterns visible in this game.\n'
        "JSON FORMAT RULES (STRICT — violating these fails the response):\n"
        "- Return ONE valid JSON object and nothing else (no prose before/after, no markdown fences).\n"
        '- EVERY string value MUST be wrapped in double quotes, e.g. "summary": "ChessCharli started strong..." '
        '— NEVER write `"summary": ChessCharli started strong...` (unquoted value is INVALID JSON).\n'
        "- Escape any internal double quotes with a backslash (\\\").\n"
        "- Arrays use square brackets with quoted string items: [\"item one\", \"item two\"].\n"
        "- No trailing commas. No comments. No single quotes.\n"
        "- Shape example (values are placeholders, do NOT copy them):\n"
        '  {"headline": "...", "summary": "...", "opening_comment": "...", '
        '"midgame_comment": "...", "endgame_comment": "...", '
        '"strengths": ["...", "..."], "improvements": ["...", "..."], "style_note": "..."}\n'
        "RULES:\n"
        f"- Only reference moves that ACTUALLY appear in the move table for {username} "
        "or in the strict-facts blocks above.\n"
        "- Do NOT invent moves, evaluations, or piece positions.\n"
        "- Strengths and improvements MUST be game-specific.\n"
        "Keep total content under ~350 words. Plain text inside fields (no headings)."
    )
    raw = await chat(session, system, user_msg, max_tokens=1100,
                     temperature=0.2, json_mode=True)
    data = _extract_json(raw)
    return {
        "headline": _strip_uci(str(data.get("headline", "")).strip()),
        "summary": _strip_uci(str(data.get("summary", "")).strip()),
        "opening_comment": _strip_uci(str(data.get("opening_comment", "")).strip()),
        "midgame_comment": _strip_uci(str(data.get("midgame_comment", "")).strip()),
        "endgame_comment": _strip_uci(str(data.get("endgame_comment", "")).strip()),
        "strengths": [_strip_uci(str(s).strip()) for s in (data.get("strengths") or []) if str(s).strip()][:5],
        "improvements": [_strip_uci(str(s).strip()) for s in (data.get("improvements") or []) if str(s).strip()][:5],
        "style_note": _strip_uci(str(data.get("style_note", "")).strip()),
    }


def sections_to_markdown(s: dict) -> str:
    """Flatten sectioned feedback for storage / fallback display."""
    parts = []
    if s.get("summary"):
        parts.append(f"**Summary** — {s['summary']}")
    if s.get("opening_comment"):
        parts.append(f"**Opening** — {s['opening_comment']}")
    if s.get("midgame_comment"):
        parts.append(f"**Critical moment** — {s['midgame_comment']}")
    if s.get("endgame_comment"):
        parts.append(f"**Endgame** — {s['endgame_comment']}")
    if s.get("strengths"):
        parts.append("**Strengths**\n" + "\n".join(f"- {i}" for i in s["strengths"]))
    if s.get("improvements"):
        parts.append("**Improvements**\n" + "\n".join(f"- {i}" for i in s["improvements"]))
    if s.get("style_note"):
        parts.append(f"**Style** — {s['style_note']}")
    return "\n\n".join(parts)


async def answer_question(
    session: aiohttp.ClientSession,
    pgn: str,
    feedback: str,
    raw_summary: dict,
    question: str,
    username: str,
    user_color: Optional[str] = None,
    move_table: Optional[list] = None,
    key_moments: Optional[dict] = None,
) -> dict:
    """Returns {'text': str, 'engine_calls': [{'fen','san','eval','continuation'}]}.

    The engine_calls list captures every engine query made by the LLM during
    the tool-calling loop, so the caller can render variation GIFs.
    """
    import engine as engine_mod

    color = (user_color or "?").upper()
    opponent_color = "black" if user_color == "white" else "white" if user_color == "black" else "?"
    opp_name = (
        (raw_summary.get(opponent_color) or {}).get("name", "the opponent")
        if user_color else "the opponent"
    )

    # ---- Build worst-move hint from stored key_moments (engine-verified at game-post time) ----
    # Primary: use midgame from key_moments — this was engine-analyzed by Stockfish 18
    # via chess-api.com when the game was first processed and stored.
    worst_hint = ""
    engine_pre_fen = None
    engine_continuation = []

    mg = (key_moments or {}).get("midgame") or {}
    if mg:
        played = mg.get("played_san")
        best = mg.get("best_san")
        pre_fen = mg.get("pre_fen")
        eng_eval = mg.get("engine_eval")
        eng_cont = mg.get("engine_continuation") or []
        eng_text = mg.get("engine_text") or ""
        ev_before = mg.get("eval_before")
        ev_after = mg.get("eval_after")
        move_no = mg.get("move_number") or mg.get("ply", 0)
        label = mg.get("label") or "Critical moment"

        engine_pre_fen = pre_fen
        engine_continuation = eng_cont

        worst_hint = "\n\nSERVER-VERIFIED CRITICAL MOMENT — the user's WORST move (Stockfish 18):\n"
        if played:
            worst_hint += f"- Move {move_no} ({user_color}): user PLAYED `{played}` — {label}.\n"
        if ev_before is not None and ev_after is not None:
            worst_hint += f"- Eval swing after the played move: {ev_before:+.2f} → {ev_after:+.2f} pawns.\n"
        if best:
            worst_hint += (
                f"- Engine's recommendation INSTEAD (this is NOT a move the user played, "
                f"it is what the user MISSED): `{best}`.\n"
            )
        if eng_eval is not None:
            worst_hint += f"- Engine eval after engine's recommended move: {eng_eval}.\n"
        if eng_text:
            worst_hint += f"- Engine note: {eng_text}\n"
        if pre_fen:
            worst_hint += f"- FEN before this move (use for analyze_position): `{pre_fen}`\n"
        if eng_cont:
            worst_hint += f"- Engine continuation (UCI): {' '.join(eng_cont[:6])}\n"
        worst_hint += (
            "USE THIS BLOCK ONLY FOR QUESTIONS ABOUT THE USER'S WORST MOVE OR MISTAKES. "
            "Do NOT cite the engine's recommended move as something the user played.\n"
        )

    # Secondary fallback: scan Lichess per-ply judgments from move table
    if not worst_hint:
        _severity = {"Blunder": 3, "Mistake": 2, "Inaccuracy": 1}
        worst_row: Optional[dict] = None
        for row in (move_table or []):
            if row.get("color") != user_color:
                continue
            j = row.get("judgment")
            if not j:
                continue
            if worst_row is None or _severity.get(j, 0) > _severity.get(worst_row.get("judgment", ""), 0):
                worst_row = row
        if worst_row:
            engine_pre_fen = worst_row.get("fen")
            worst_hint = (
                f"\n\nSERVER-VERIFIED WORST MOVE (Lichess judgment):\n"
                f"Move {worst_row['n']} ({worst_row['color']}): `{worst_row['san']}` — "
                f"judgment: {worst_row['judgment']}, eval after: {worst_row['eval']}.\n"
                f"Comment: {worst_row.get('comment') or '(none)'}\n"
                f"FEN before this move (use for analyze_position): `{engine_pre_fen or 'unknown'}`\n"
                "Use analyze_position with this FEN to find the best alternative.\n"
            )

    # Pre-compute the user's best played move so the LLM can't guess wrong.
    # Exposed via `best_played_move` in the return dict; also injected into the prompt
    # as a single definitive fact so the LLM just elaborates rather than guessing.
    user_rows = [r for r in (move_table or []) if r.get("color") == user_color]
    best_row = next((r for r in user_rows if r.get("judgment") == "Best"), None)
    if not best_row:
        best_delta = None
        all_rows = move_table or []
        for idx, r in enumerate(all_rows):
            if r.get("color") != user_color:
                continue
            ev = r.get("eval")
            if not isinstance(ev, (int, float)):
                continue
            prev_ev = None
            for j in range(idx - 1, -1, -1):
                pe = all_rows[j].get("eval")
                if isinstance(pe, (int, float)):
                    prev_ev = pe
                    break
            if prev_ev is None:
                continue
            delta = (ev - prev_ev) if user_color == "white" else (prev_ev - ev)
            if delta < 0:
                continue
            if best_delta is None or delta > best_delta:
                best_delta = delta
                best_row = r

    best_played_move: Optional[dict] = None
    best_played_hint = ""
    if best_row:
        best_played_move = {
            "san": best_row.get("san"),
            "move_number": best_row.get("n"),
            "eval": best_row.get("eval"),
            "judgment": best_row.get("judgment"),
            "fen": best_row.get("fen"),
        }
        best_played_hint = (
            "\n\nUSER'S BEST PLAYED MOVE — DEFINITIVE (computed from move table):\n"
            f"- Move {best_row.get('n')}: `{best_row.get('san')}` ({user_color})"
        )
        if isinstance(best_row.get("eval"), (int, float)):
            best_played_hint += f", eval after: {best_row['eval']:+.2f}"
        if best_row.get("judgment"):
            best_played_hint += f", judgment: {best_row['judgment']}"
        best_played_hint += (
            ".\nFor 'best move I played' questions, state this move and briefly explain "
            "why it was strong. Do NOT substitute any other move.\n"
        )

    system = (
        COACH_SYSTEM
        + f"\nThe user you are coaching is **{username}** playing {color}. "
        f"Their opponent is **{opp_name}** playing {opponent_color.upper()}. "
        "Never swap these roles.\n\n"
        "CRITICAL DISTINCTION:\n"
        "- A 'move the user played' is a move that appears in the move_table or PGN.\n"
        "- The engine's recommended/best move in the CRITICAL MOMENT block is a move the "
        "user did NOT play — it is what they SHOULD have played. NEVER report it as the "
        "user's own best move.\n\n"
        "GROUND TRUTH HIERARCHY (in order of trust):\n"
        "1. SERVER-VERIFIED CRITICAL MOMENT block — the user's WORST move + the engine's "
        "alternative. Use only for worst-move/mistake questions.\n"
        "2. USER'S STRONGEST PLAYED MOVE block — for best-played-move questions.\n"
        "3. The MOVE TABLE — eval/judgment per ply; always cite moves from here when "
        "talking about what the user actually played.\n"
        "4. The `analyze_position` tool — for ad-hoc engine queries (always pass a real FEN).\n"
        "5. Stored coach feedback — unreliable for specific move judgments; ignore it.\n\n"
        "QUESTION ROUTING:\n"
        "- 'worst move' / 'biggest mistake' / 'what should I have played' → CRITICAL MOMENT block.\n"
        "- 'best move' / 'strongest move' / 'best move I played' → USER'S BEST PLAYED MOVE block. "
        "That block already has the definitive answer; just explain why that specific move was good.\n\n"
        "ANSWER STYLE — VERY IMPORTANT:\n"
        "- Never answer in a single sentence. A bare 'The best move you played was Nf6.' is FORBIDDEN.\n"
        "- Always give **3–6 sentences** (or short bullet list) covering: (a) the move and the "
        "move number with SAN, (b) the concrete tactical/positional reason it was strong or "
        "weak — name the squares, pieces, threats, plans, or pawn structure involved, "
        "(c) what the position demanded and how the move addressed it, and (d) one practical "
        "takeaway the user can apply in future games.\n"
        "- If the question is about a mistake, also state the engine's alternative (from the "
        "CRITICAL MOMENT block) and the eval swing in pawns.\n"
        "- Quote the exact move number and SAN every time, e.g. `12...Nf6`.\n"
        "- Do not pad with generic chess clichés ('chess is about strategy'); every sentence "
        "must reference the actual position.\n\n"
        "NOTATION: SAN only (Nf3, Be2, O-O). Never UCI (g1f3)."
    )

    user_msg = (
        f"USER: {username} ({color})\n"
        f"OPPONENT: {opp_name} ({opponent_color.upper()})\n"
        f"{worst_hint}"
        f"{best_played_hint}\n"
        f"Move table (all judged moves + opening/endgame rows — includes FEN before each move):\n"
        f"```json\n{json.dumps(move_table or [])[:5500]}\n```\n\n"
        f"PGN:\n```\n{pgn[:2000]}\n```\n\n"
        f"Stored coach feedback (treat as unreliable background context only):\n{feedback[:500]}\n\n"
        f"User question: {question}\n\n"
        "To call analyze_position, take the `fen` value from the relevant move_table row — "
        "do NOT invent a FEN."
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "analyze_position",
                "description": (
                    "Get Stockfish 18 engine analysis for a chess position. "
                    "Returns best move (SAN), evaluation in pawns (positive=white winning), "
                    "mate-in-N if forced, and a 5-move principal variation (UCI). "
                    "Call this whenever you need to find the best move in a specific position. "
                    "Use the `fen` field from the move_table row — do NOT guess a FEN."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fen": {
                            "type": "string",
                            "description": "FEN string of the position to analyze.",
                        }
                    },
                    "required": ["fen"],
                },
            },
        }
    ]

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    # engine_calls captures only LLM-driven tool calls (used to render their GIFs).
    # The pre-stored critical moment is exposed separately so the caller can decide
    # whether to render the played-move + recommended-line GIFs (only when the
    # answer actually discusses that moment).
    engine_calls: list = []
    critical_moment = None
    if engine_pre_fen and engine_continuation and mg:
        critical_moment = {
            "fen": engine_pre_fen,
            "played_san": mg.get("played_san"),
            "best_san": mg.get("best_san"),
            "eval_before": mg.get("eval_before"),
            "eval_after": mg.get("eval_after"),
            "engine_eval": mg.get("engine_eval"),
            "move_number": mg.get("move_number") or mg.get("ply"),
            "continuation": engine_continuation[:6],
        }

    for _ in range(3):
        data = await _chat_messages(session, messages, max_tokens=700, tools=tools)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            return {
                "text": _strip_uci(msg.get("content", "").strip()),
                "engine_calls": engine_calls,
                "critical_moment": critical_moment,
                "best_played_move": best_played_move,
            }

        messages.append(msg)

        for tc in tool_calls:
            fn_args = {}
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except Exception:
                pass

            if tc["function"]["name"] == "analyze_position":
                fen = fn_args.get("fen", "")
                result = await engine_mod.best_move(session, fen) if fen else None
                if result:
                    cont = result.get("continuationArr", []) or []
                    best_lan = result.get("lan") or result.get("move")
                    if best_lan and (not cont or cont[0] != best_lan):
                        cont = [best_lan] + list(cont)
                    engine_calls.append({
                        "fen": fen,
                        "san": result.get("san"),
                        "eval": result.get("eval"),
                        "mate": result.get("mate"),
                        "continuation": cont[:6],
                    })
                tool_result = (
                    {
                        "best_move_san": result.get("san"),
                        "eval": result.get("eval"),
                        "mate": result.get("mate"),
                        "depth": result.get("depth"),
                        "continuation": result.get("continuationArr", [])[:5],
                        "text": result.get("text", ""),
                    }
                    if result else {"error": "Engine returned no result."}
                )
            else:
                tool_result = {"error": f"Unknown tool: {tc['function']['name']}"}

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result),
            })

    data = await _chat_messages(session, messages, max_tokens=700)
    return {
        "text": _strip_uci(data["choices"][0]["message"].get("content", "").strip()),
        "engine_calls": engine_calls,
        "critical_moment": critical_moment,
        "best_played_move": best_played_move,
    }


async def weekly_report(session: aiohttp.ClientSession, games_summaries: list, username: str) -> str:
    user_msg = (
        f"Player: {username}\n"
        f"Past 3 months — {len(games_summaries)} games. "
        "Per-game brief data follows (JSON list, truncated):\n"
        f"```json\n{json.dumps(games_summaries)[:8000]}\n```\n\n"
        "Write a weekly review with sections:\n"
        "**Recurring mistakes**\n**Patterns** (openings, blunders, time pressure...)\n"
        "**Strengths**\n**Training recommendations** — concrete, prioritized.\n"
        "Keep under ~400 words. Use Markdown."
    )
    return await chat(session, COACH_SYSTEM, user_msg, max_tokens=1100, temperature=0.5)

