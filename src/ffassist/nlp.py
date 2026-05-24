from __future__ import annotations

import contextvars
import logging

from anthropic import (
    Anthropic,
    APIError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
    beta_tool,
)
from rapidfuzz import process

from ffassist import state as state_mod
from ffassist.config import settings
from ffassist.draft_state import (
    available_players,
    filter_by_position,
    my_picks,
    parse_picks,
    parse_players,
)
from ffassist.mfl.client import MFLClient
from ffassist.poller import find_my_franchise, league_label
from ffassist.rankings import (
    best_available,
    best_available_blended,
    get_filtered_adp,
    load_csv_override,
    merge_rankings,
    my_drafted_counts,
    parse_mfl_adp,
)

logger = logging.getLogger(__name__)

_thread_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "thread_ctx", default=None
)


def _resolve_league(query: str) -> str | None:
    """Resolve a league_id argument that may be a numeric id OR a host name.

    Tries: exact id match → exact display_name match → fuzzy display_name match.
    Returns the canonical league_id string or None if no match.
    """
    if not query:
        return None
    q = str(query).strip()
    ids = settings.league_ids
    if q in ids:
        return q
    state = state_mod.load()
    leagues = state.get("leagues", {})
    name_to_id = {
        (leagues.get(lid, {}).get("display_name") or "").lower(): lid
        for lid in ids
        if leagues.get(lid, {}).get("display_name")
    }
    low = q.lower()
    if low in name_to_id:
        return name_to_id[low]
    # Fuzzy match against host names
    if name_to_id:
        match = process.extractOne(low, list(name_to_id.keys()), score_cutoff=70)
        if match:
            return name_to_id[match[0]]
    return None


@beta_tool
def get_on_the_clock() -> str:
    """Authoritative check of who is ON THE CLOCK right now in each tracked league.

    Uses our stored draft_order (preferred) or inferred order from completed picks. Returns
    one line per league with the current pick number and whose turn it is. Always call this
    BEFORE telling Scott whether he is on the clock anywhere. Never speculate.
    """
    from ffassist import state as state_mod
    from ffassist.draft_state import (
        extract_draft_order,
        extract_draft_type,
        infer_round1_order,
        next_pick_number,
        parse_picks,
        snake_owner,
    )
    from ffassist.poller import find_my_franchise

    state = state_mod.load()
    ids = settings.league_ids
    if not ids:
        return "No leagues tracked."
    lines = []
    with MFLClient() as mfl:
        for lid in ids:
            try:
                league = mfl.league(lid)
                dr = mfl.draft_results(lid)
                picks = parse_picks(dr)
                api_order = extract_draft_order(dr)
                draft_type = extract_draft_type(dr) or state.get("leagues", {}).get(lid, {}).get("draft_type", "")
                my_id = find_my_franchise(league, settings.mfl_username)
                ls = state.get("leagues", {}).get(lid, {})
                stored_order = ls.get("draft_order")
                if api_order:
                    order = api_order
                elif stored_order:
                    order = list(stored_order)
                else:
                    order = infer_round1_order(picks)
                team_count = len(order) if order else None
                next_pick = next_pick_number(picks, team_count=team_count)
                on_clock = snake_owner(next_pick, order, draft_type=draft_type) if order else None
                n = max(len(order), 1)
                round_n = (next_pick - 1) // n + 1
                franchises = league.get("franchises", {}).get("franchise", [])
                if isinstance(franchises, dict):
                    franchises = [franchises]
                names = {f.get("id"): f.get("name", "?") for f in franchises}
                host = ls.get("display_name") or f"L{lid}"
                if on_clock == my_id and my_id:
                    lines.append(
                        f"{host}: :rotating_light: YOU are on the clock — pick #{next_pick}, round {round_n}"
                    )
                elif on_clock:
                    lines.append(
                        f"{host}: pick #{next_pick}, on clock: {names.get(on_clock, on_clock)}"
                    )
                else:
                    lines.append(
                        f"{host}: pick #{next_pick}, draft order not fully known yet (round 1 inference partial)"
                    )
            except Exception as e:
                lines.append(f"{league_label(lid)}: error {e!r}")
    return "\n".join(lines)


@beta_tool
def get_status() -> str:
    """Pick COUNTS across tracked leagues. Use get_on_the_clock to check who is on the clock."""
    ids = settings.league_ids
    if not ids:
        return "No leagues tracked."
    lines = []
    with MFLClient() as mfl:
        for lid in ids:
            try:
                league = mfl.league(lid)
                picks = parse_picks(mfl.draft_results(lid))
                my_id = find_my_franchise(league, settings.mfl_username)
                mine = sum(1 for p in picks if p.franchise_id == my_id) if my_id else 0
                lines.append(
                    f"{league_label(lid)}: {len(picks)} total picks, {mine} are yours"
                )
            except Exception as e:
                lines.append(f"{league_label(lid)}: error {e!r}")
    return "\n".join(lines)


@beta_tool
def get_league_top_20(league_id: str) -> str:
    """Return the top-20 list for a league.

    `league_id` accepts either the numeric MFL id ("37714") OR the host's name
    ("Matt Harmon", "Liz Loza"). If a recent (<30 min) cached list exists, it's
    returned. Otherwise a fresh list is computed, cached, and returned — this is
    the manual fallback when the auto on-the-clock build hasn't fired or failed.
    """
    import time
    from ffassist import rankings_v2 as _r2

    lid = _resolve_league(league_id)
    if not lid:
        return (
            f"Could not resolve league '{league_id}'. Tracked leagues: "
            + ", ".join(
                f"{league_label(x)} (L{x})" for x in settings.league_ids
            )
        )

    state = state_mod.load()
    ls = state.get("leagues", {}).get(lid, {})
    host = ls.get("display_name") or f"L{lid}"
    text = ls.get("last_top20_text")
    last_at = ls.get("last_top20_at", 0)
    last_pick = ls.get("last_top20_pick")
    now = time.time()
    age_sec = now - last_at if last_at else None

    # Determine the CURRENT pick — if it differs from the cached one, the cache is
    # stale by definition (a player has been drafted since) and must NOT be returned.
    current_pick = None
    try:
        with MFLClient() as mfl:
            dr = mfl.draft_results(lid)
            from ffassist.draft_state import (
                extract_draft_order as _edo,
                next_pick_number as _npn,
                parse_picks as _pp,
            )
            picks_now = _pp(dr)
            order_now = _edo(dr) or ls.get("draft_order") or []
            tc_now = len(order_now) if order_now else None
            current_pick = _npn(picks_now, team_count=tc_now)
    except Exception:
        pass

    cache_pick_matches = (
        current_pick is not None and last_pick is not None and current_pick == last_pick
    )

    if text and age_sec is not None and age_sec < 1800 and cache_pick_matches:
        age_label = f"{int(age_sec / 60)}m ago" if age_sec > 60 else "just now"
        return (
            f"*Top 20 in {host}'s league* (cached {age_label}, pick #{last_pick}):\n"
            "Format: rank. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins}\n  · `fce` = # of tracked FCE drafts this player has been selected in\n\n"
            f"{text}"
        )

    # Stale, missing, or pick has advanced — compute fresh and store
    try:
        with MFLClient() as mfl:
            fresh_text, fresh_list = _r2.build_league_top_20(lid, mfl, settings)
    except Exception as e:
        if text:
            old_label = f"{int(age_sec / 60)}m ago" if age_sec else "unknown age"
            return (
                f":warning: Fresh compute failed ({e!r}); returning stale cached list ({old_label}, pick #{last_pick}):\n\n"
                f"*Top 20 in {host}'s league*:\n{text}"
            )
        return f":x: Could not build top-20 for {host}'s league: {e!r}"

    state.setdefault("leagues", {}).setdefault(lid, {})
    state["leagues"][lid]["last_top20_text"] = fresh_text
    state["leagues"][lid]["last_top20"] = fresh_list
    state["leagues"][lid]["last_top20_at"] = now
    if current_pick is not None:
        state["leagues"][lid]["last_top20_pick"] = current_pick
    state_mod.save(state)
    return (
        f"*Top 20 in {host}'s league* (freshly computed):\n"
        "Format: rank. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins}\n  · `fce` = # of tracked FCE drafts this player has been selected in\n\n"
        f"{fresh_text}"
    )


def _dnp_tag(mflid: str) -> str:
    """Return ' [DID NOT PLAY 2025]' / ' (limited 2025)' tag based on per-year gp.

    Reads ppg_2025.cached() which is the per-year dict. Cheap (in-memory cache).
    """
    try:
        from ffassist.ppg_2025 import cached as cached_ppg
        info = cached_ppg().get(mflid, {})
    except Exception:
        return ""
    v25 = info.get("2025", {})
    v24 = info.get("2024", {})
    gp25 = int(v25.get("games_played", 0) or 0)
    gp24 = int(v24.get("games_played", 0) or 0)
    if gp25 == 0 and gp24 > 0:
        return f" [DID NOT PLAY 2025 — number is 2024×0.9 fallback]"
    if 0 < gp25 < 9 and gp24 >= 12:
        return f" [LIMITED 2025: {gp25}gp]"
    return ""


@beta_tool
def get_player_outlook(player_query: str) -> str:
    """Comprehensive player outlook — REQUIRED for any 'how is X looking', 'outlook on Y',
    'news on Z', 'status of W', 'should I draft player' question. NEVER guess from memory.

    Combines: MFL identity (team/position/rookie/FA status), per-year PPG with GAMES PLAYED
    breakdown so you can spot full-season absences, FantasyPros ECR/tier/projection, and
    recent FP news. This is the single source of truth for player outlook questions.

    Args:
        player_query: Player name (fuzzy match against MFL roster).
    """
    from ffassist import fantasypros as fp
    from ffassist.ppg_2025 import cached as cached_ppg

    with MFLClient() as mfl:
        players = parse_players(mfl.players())
    names = {p.name: p for p in players.values()}
    match = process.extractOne(player_query, list(names.keys()), score_cutoff=70)
    if not match:
        return f"No MFL player matched '{player_query}' (fuzzy score < 70)."
    p = names[match[0]]

    lines = [f"*{p.name}* ({p.team} {p.position}{' R' if p.is_rookie else ''})"]
    if p.team == "FA":
        lines.append("  ⚠️ Free agent — no current team. Likely no projections until signed.")

    # Per-year PPG with games_played
    info = cached_ppg().get(p.id, {})
    v25 = info.get("2025", {})
    v24 = info.get("2024", {})
    gp25 = int(v25.get("games_played", 0) or 0)
    gp24 = int(v24.get("games_played", 0) or 0)
    ppg25 = v25.get("ppg")
    ppg24 = v24.get("ppg")
    if gp25 == 0 and gp24 == 0:
        lines.append("  PPG history: no recorded games in 2024 or 2025 (likely rookie or never played)")
    else:
        s25 = f"{ppg25:.1f} over {gp25}gp" if gp25 > 0 and ppg25 is not None else f"0gp (DID NOT PLAY)"
        s24 = f"{ppg24:.1f} over {gp24}gp" if gp24 > 0 and ppg24 is not None else f"0gp (DID NOT PLAY)"
        lines.append(f"  2025 PPG: {s25}")
        lines.append(f"  2024 PPG: {s24}")
        if gp25 == 0 and gp24 > 0:
            lines.append(
                "  ⚠️ MISSED ALL OF 2025. Any blended PPG you see elsewhere "
                "(e.g. in best-available tool output) is 2024 × 0.9 fallback, NOT a 2025 number. "
                "Do not present it as 2025 performance."
            )

    # FantasyPros
    if settings.fp_api_key:
        try:
            enriched = fp.enrich_for_mflids([p.id])
            e = enriched.get(p.id)
        except fp.FPError as err:
            lines.append(f"  FP error: {err}")
            e = None
        if e and e.fpid is not None:
            fp_bits = []
            if e.ecr is not None:
                fp_bits.append(f"ECR #{e.ecr}")
            if e.tier is not None:
                fp_bits.append(f"Tier {e.tier}")
            if e.pos_rank:
                fp_bits.append(e.pos_rank)
            if e.points_ppr is not None:
                fp_bits.append(f"proj {e.points_ppr:.1f} PPR pts")
            lines.append(f"  FantasyPros: {' · '.join(fp_bits) if fp_bits else '(no consensus rank yet)'}")
            if e.news_impact:
                lines.append("  📰 Recent FP news on this player — calling get_fantasypros_news now would show details")
        elif e:
            lines.append("  FantasyPros: NOT in projections — likely no 2026 landing spot, FA, or retired")
    else:
        lines.append("  FantasyPros: not configured")

    # Recent news (cheap — cached)
    if settings.fp_api_key:
        try:
            news = fp.get_news()
            needle_first = p.name.split()[0].lower()
            needle_last = p.name.split()[-1].lower()
            matches = [
                i for i in news.get("items", [])
                if needle_last in (i.get("title", "") or "").lower()
                and needle_first[0] in (i.get("title", "") or "").lower()
            ][:3]
            if matches:
                lines.append("  Recent news:")
                for item in matches:
                    when = item.get("created_formated", "")
                    title = item.get("title", "")
                    lines.append(f"    [{when}] {title}")
        except fp.FPError:
            pass

    return "\n".join(lines)


@beta_tool
def get_universal_best_available(position: str = "", limit: int = 20) -> str:
    """Universal blended ranking across ALL players (not filtered to any specific league).

    Use this for cross-league questions ("top 10 players by value", "where's Gibbs ranked").
    Since all 4 leagues have IDENTICAL scoring with no position requirements, the underlying
    ranking is the same everywhere. The only per-league difference is which players have been
    drafted off-the-board.

    Args:
        position: QB/RB/WR/TE/FLEX/SFLEX/Def/empty.
        limit: Max players (default 20, cap 100).
    """
    from ffassist.ppg_2025 import cached_ppg_only as cached_ppg

    limit = max(1, min(limit, 100))
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        adp = get_filtered_adp(mfl.adp(), players)
        dupes = my_drafted_counts(mfl, settings.league_ids, settings.mfl_username)
    ppg = cached_ppg()

    pool = [p for p in players.values() if p.position in {"QB", "RB", "WR", "TE", "Def", "PN"}]
    if position:
        pool = filter_by_position(pool, position)
    top = best_available_blended(pool, adp, ppg, n=limit, already_drafted=dupes)
    if not top:
        return f"No players found matching position={position!r}."
    lines = [
        f"Universal top {len(top)} {position or 'all'} (Blend = 0.6 × PPG_rank + 0.4 × raw_ADP):"
    ]
    for pl, info in top:
        ppg_str = "PPG —" if info["is_rookie"] else (f"PPG {info['ppg']:.1f}" if info["ppg"] is not None else "PPG —")
        adp_str = f"ADP {info['adp']:.1f}" if info["adp"] is not None else "ADP —"
        bias = f"  -{info['penalty_pct']}% bias" if info["dupes"] else ""
        rookie = "  [ROOKIE]" if info["is_rookie"] else ""
        dnp = _dnp_tag(pl.id)
        lines.append(
            f"  blend={info['blend']:6.2f}  {pl.display()} [id={pl.id}]  {ppg_str}  {adp_str}{rookie}{bias}{dnp}"
        )
    return "\n".join(lines)


@beta_tool
def get_available_players(league_id: str, position: str = "", limit: int = 15) -> str:
    """List the best available players in a league, ranked by BLENDED 60% PPG / 40% ADP.

    Special handling:
    - Rookies use 100% ADP (no 2025 PPG penalty).
    - Players Scott has already drafted in other leagues get a +5%-per-dupe rank penalty
      (diversification bias). Shown in output.

    Use get_available_players_by_adp_only for pure ADP.
    """
    from ffassist.ppg_2025 import cached_ppg_only as cached_ppg

    limit = max(1, min(limit, 50))
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        picks = parse_picks(mfl.draft_results(league_id))
        adp = get_filtered_adp(mfl.adp(), players)
        # Compute cross-league dupe counts for diversification bias
        dupes = my_drafted_counts(mfl, settings.league_ids, settings.mfl_username)
    overrides = load_csv_override(league_id, players=players)
    rankings = merge_rankings(adp, overrides)
    ppg = cached_ppg()

    avail = available_players(players, picks)
    if position:
        avail = filter_by_position(avail, position)
    top = best_available_blended(avail, rankings, ppg, n=limit, already_drafted=dupes)
    if not top:
        return f"No available players in L{league_id} matching position={position!r}."
    lines = [
        f"Top {len(top)} available {position or 'all'} in L{league_id} "
        f"(60% PPG / 40% ADP; rookies=100% ADP; -5%/dupe diversification):"
    ]
    for pl, info in top:
        ppg_str = "PPG —" if info["is_rookie"] else (f"PPG {info['ppg']:.1f}" if info["ppg"] is not None else "PPG —")
        adp_str = f"ADP {info['adp']:.1f}" if info["adp"] is not None else "ADP —"
        bias = ""
        if info["dupes"]:
            bias = f"  -{info['penalty_pct']}% bias ({info['dupes']}x drafted)"
        rookie_tag = "  [ROOKIE: 100% ADP]" if info["is_rookie"] else ""
        dnp = _dnp_tag(pl.id)
        lines.append(f"  {pl.display()} [id={pl.id}]  {ppg_str}  {adp_str}{rookie_tag}{bias}{dnp}")
    return "\n".join(lines)


@beta_tool
def get_available_players_by_adp_only(league_id: str, position: str = "", limit: int = 15) -> str:
    """Best available by pure ADP (no PPG blending). Use only when Scott specifically asks."""
    limit = max(1, min(limit, 50))
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        picks = parse_picks(mfl.draft_results(league_id))
        adp = get_filtered_adp(mfl.adp(), players)
    overrides = load_csv_override(league_id, players=players)
    rankings = merge_rankings(adp, overrides)
    avail = available_players(players, picks)
    if position:
        avail = filter_by_position(avail, position)
    top = best_available(avail, rankings, n=limit)
    if not top:
        return f"No available players in L{league_id} matching position={position!r}."
    lines = [f"Top {len(top)} available {position or 'all'} in L{league_id} (ADP only):"]
    for pl, rank in top:
        r = f"ADP {rank:.1f}" if rank is not None else "no rank"
        lines.append(f"  {pl.display()} [id={pl.id}] ({r})")
    return "\n".join(lines)


@beta_tool
def get_my_picks(league_id: str) -> str:
    """Get Scott's picks so far in a specific MFL league."""
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        league = mfl.league(league_id)
        picks = parse_picks(mfl.draft_results(league_id))
    my_id = find_my_franchise(league, settings.mfl_username)
    if not my_id:
        return f"Could not identify your franchise in L{league_id}."
    mine = my_picks(picks, my_id)
    if not mine:
        return f"No picks yet in L{league_id}."
    lines = [f"L{league_id} my picks:"]
    for p in mine:
        disp = players[p.player_id].display() if p.player_id in players else p.player_id
        lines.append(f"  R{p.round} #{p.pick}: {disp}")
    return "\n".join(lines)


@beta_tool
def match_player(league_id: str, query: str) -> str:
    """Fuzzy-match a player name against the available pool in a league.

    Returns the player's id, name, team, and position so you can call propose_pick.
    """
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        picks = parse_picks(mfl.draft_results(league_id))
    avail = available_players(players, picks)
    names = {p.name: p for p in avail}
    if not names:
        return "No available players."
    match = process.extractOne(query, list(names.keys()), score_cutoff=70)
    if not match:
        return f"No available player matched '{query}' (score < 70)."
    p = names[match[0]]
    return (
        f"Match: {p.name} ({p.team} {p.position}) "
        f"[player_id={p.id}, score={match[1]:.0f}]"
    )


@beta_tool
def bootstrap_draft_thread(league_id: str) -> str:
    """Manually create an active draft thread for a league where Scott is on the clock.

    Use this ONLY when get_on_the_clock confirms Scott is on the clock in `league_id`
    but the poller did not fire an alert (e.g. the bot was down, draft order wasn't
    known yet). This creates the thread state so propose_pick + 'yes' confirmation will
    actually submit through MFL.

    The caller (LLM) must follow this with: match_player → propose_pick → (Scott says 'yes')
    which then routes through _submit_pending. NEVER fabricate confirmation prose.

    Args:
        league_id: numeric MFL league id, e.g. "73196".

    Returns the new thread_ts so propose_pick will work, OR an error message.
    """
    from ffassist.draft_state import (
        extract_draft_order,
        extract_draft_type,
        next_pick_number,
        parse_picks,
        snake_owner,
    )
    from ffassist.poller import find_my_franchise, league_label
    from ffassist.slack_notify import SlackNotifier

    if not settings.slack_channel_id:
        return ":x: SLACK_CHANNEL_ID not configured — can't post bootstrap thread."

    try:
        with MFLClient() as mfl:
            league = mfl.league(league_id)
            draft = mfl.draft_results(league_id)
    except Exception as e:
        return f":x: Could not fetch league/draft for L{league_id}: {e!r}"

    my_id = find_my_franchise(league, settings.mfl_username)
    if not my_id:
        return f":x: Could not identify Scott's franchise in L{league_id}."
    picks = parse_picks(draft)
    order = extract_draft_order(draft)
    draft_type = extract_draft_type(draft)
    if not order:
        return f":x: L{league_id} hasn't published a round1DraftOrder yet — cannot determine who is on the clock."
    tc = len(order)
    next_pick = next_pick_number(picks, team_count=tc)
    on_clock = snake_owner(next_pick, order, draft_type=draft_type)
    if on_clock != my_id:
        return (
            f":warning: Scott is NOT on the clock in {league_label(league_id)} — pick #{next_pick} "
            f"belongs to franchise {on_clock}. Refusing to bootstrap a thread."
        )

    round_num = (next_pick - 1) // tc + 1
    label = league_label(league_id)
    notifier = SlackNotifier()
    text = (
        f":alarm_clock: *Manual bootstrap — On the clock* in *{label}*'s league "
        f"— pick *#{next_pick}* (round {round_num})\n"
        "_(Poller missed this one; bot is now tracking it.)_\n"
        "_Reply in this thread to act._"
    )
    result = notifier.channel(text)
    if not result or not result.get("ok"):
        return ":x: Failed to post bootstrap message to Slack."

    state = state_mod.load()
    ls = state["leagues"].setdefault(league_id, {})
    ls["draft_order"] = order
    ls["last_alerted_pick"] = next_pick
    ls["on_clock"] = on_clock
    ls["next_pick"] = next_pick
    ls["active_thread_ts"] = result["ts"]
    ls["active_thread_channel"] = result["channel"]
    state["threads"][result["ts"]] = {
        "league_id": league_id,
        "channel": result["channel"],
        "pick_number": next_pick,
        "round": round_num,
        "year": settings.mfl_year,
        "my_franchise_id": my_id,
        "kind": "mfl_draft",
        "pending": None,
        "last_top20": [],
    }
    state_mod.save(state)
    return (
        f"Bootstrapped draft thread for {label} — pick #{next_pick}, round {round_num}. "
        f"Tell Scott to reply in the new Slack thread with the player he wants, "
        f"then 'yes' to confirm. thread_ts={result['ts']}"
    )


@beta_tool
def resync_on_clock_threads() -> str:
    """Recovery: post a FRESH on-the-clock thread for every tracked league where Scott is
    currently on the clock — bypasses the poller's "already alerted" dedupe.

    Use when Scott says things like "resync", "give me my threads", "I'm missing threads",
    "the bot was off, refresh", or any time after downtime/restart when threads are missing
    for active picks. Posts ONE new thread per on-the-clock league (with top-20 list inside),
    skips leagues where it's someone else's pick. Safe to call repeatedly — each call posts a
    new thread in the leagues where Scott is on the clock.
    """
    from ffassist.poller import force_resync_threads

    result = force_resync_threads()
    posted = result.get("posted", [])
    skipped = result.get("skipped", [])
    errors = result.get("errors", [])
    lines = []
    if posted:
        lines.append(f":white_check_mark: Posted {len(posted)} fresh thread(s):")
        for p in posted:
            lines.append(f"  • {p.get('host', p['league'])} — pick #{p.get('pick')}")
    else:
        lines.append("No new threads posted — Scott isn't on the clock in any tracked league right now.")
    if skipped:
        not_oc = [s for s in skipped if "not on the clock" in s.get("reason", "")]
        if not_oc:
            lines.append(f"_Skipped {len(not_oc)} league(s) where it's someone else's pick._")
    if errors:
        lines.append(f":warning: Errors in {len(errors)} league(s):")
        for e in errors:
            lines.append(f"  • L{e['league']}: {e['error']}")
    return "\n".join(lines)


def _resolve_player_for_autodraft(query: str, players_lookup: dict) -> tuple[str | None, str]:
    """Fuzzy-match a player name to an MFL id. Returns (player_id, display_or_error)."""
    from rapidfuzz import process
    names = {p.display(): p for p in players_lookup.values()}
    match = process.extractOne(query, list(names.keys()), score_cutoff=70)
    if not match:
        return None, f"No player matched '{query}' (score < 70)."
    p = names[match[0]]
    return p.id, p.display()


@beta_tool
def get_autodraft_list(league_id: str) -> str:
    """Show Scott's auto-draft list for a league (ordered by priority).

    `league_id` accepts numeric MFL id ("42033") or host name ("Joey Wright").
    Also reports whether MFL auto-mode is currently ON for the league.
    """
    from ffassist import autodraft
    from ffassist.draft_state import parse_picks, parse_players

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    lst = autodraft.get_list(lid)
    auto_on = autodraft.get_auto_mode(lid)
    host = state_mod.load().get("leagues", {}).get(lid, {}).get("display_name") or f"L{lid}"
    if not lst:
        return (
            f"*{host}* auto-draft list is empty. "
            f"MFL auto-mode: {'ON' if auto_on else 'off'}.\n"
            "Seed it with `seed_autodraft_from_top20` or `add_to_autodraft_list`."
        )

    with MFLClient() as mfl:
        players = parse_players(mfl.players())
        drafted = {p.player_id for p in parse_picks(mfl.draft_results(lid))}

    lines = [f"*{host}* auto-draft list ({len(lst)} entries). MFL auto-mode: {'*ON*' if auto_on else 'off'}."]
    for i, pid in enumerate(lst, 1):
        pl = players.get(pid)
        name = pl.display() if pl else f"player_id={pid}"
        tag = " ~drafted~" if pid in drafted else ""
        lines.append(f"  {i:>2}. {name}{tag}")
    avail = sum(1 for pid in lst if pid not in drafted)
    lines.append(f"_{avail} still available._")
    return "\n".join(lines)


@beta_tool
def set_autodraft_list(league_id: str, players_csv: str) -> str:
    """REPLACE the auto-draft list for a league with the given ordered players.

    `players_csv` is a comma-separated list of player names IN PRIORITY ORDER
    (top of list = picked first). Each name is fuzzy-matched against MFL's player
    pool. Use this when Scott gives you a full list at once. For incremental
    edits use add_to_autodraft_list / remove_from_autodraft_list.

    Example: set_autodraft_list("Joey Wright", "Tank Dell, James Conner, Evan Engram, Tyjae Spears")
    """
    from ffassist import autodraft
    from ffassist.draft_state import parse_players

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    names = [n.strip() for n in players_csv.split(",") if n.strip()]
    if not names:
        return "No player names parsed."
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
    resolved: list[str] = []
    misses: list[str] = []
    display: list[str] = []
    for q in names:
        pid, disp = _resolve_player_for_autodraft(q, players)
        if pid:
            resolved.append(pid)
            display.append(disp)
        else:
            misses.append(q)
    if not resolved:
        return ":x: No players matched. Misses: " + ", ".join(misses)
    autodraft.set_list(lid, resolved)
    host = state_mod.load().get("leagues", {}).get(lid, {}).get("display_name") or f"L{lid}"
    out = [f":white_check_mark: Set {host} auto-draft list ({len(resolved)} entries):"]
    for i, d in enumerate(display, 1):
        out.append(f"  {i:>2}. {d}")
    if misses:
        out.append(":warning: Could not match: " + ", ".join(misses))
    return "\n".join(out)


@beta_tool
def add_to_autodraft_list(league_id: str, player_name: str, position: int | None = None) -> str:
    """Add a single player to the auto-draft list.

    By default appends at the end. Pass `position` (1-based) to insert at a specific
    spot — e.g. position=1 makes them the next pick if Scott goes on the clock.
    """
    from ffassist import autodraft
    from ffassist.draft_state import parse_players

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
    pid, disp = _resolve_player_for_autodraft(player_name, players)
    if not pid:
        return disp
    idx = (position - 1) if position else None
    added = autodraft.add(lid, pid, index=idx)
    host = state_mod.load().get("leagues", {}).get(lid, {}).get("display_name") or f"L{lid}"
    if not added:
        return f"_{disp} was already on the {host} list._"
    spot = f"position {position}" if position else f"end ({len(autodraft.get_list(lid))})"
    return f":white_check_mark: Added *{disp}* to {host} auto-draft list at {spot}."


@beta_tool
def remove_from_autodraft_list(league_id: str, player_name: str) -> str:
    """Remove a player from the auto-draft list by name."""
    from ffassist import autodraft
    from ffassist.draft_state import parse_players

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    lst = autodraft.get_list(lid)
    if not lst:
        return "List is empty — nothing to remove."
    with MFLClient() as mfl:
        players = parse_players(mfl.players())
    # Only match against players already on the list
    on_list_names = {players[pid].display(): pid for pid in lst if pid in players}
    from rapidfuzz import process
    match = process.extractOne(player_name, list(on_list_names.keys()), score_cutoff=70)
    if not match:
        return f"No player on the list matched '{player_name}'."
    pid = on_list_names[match[0]]
    autodraft.remove(lid, pid)
    host = state_mod.load().get("leagues", {}).get(lid, {}).get("display_name") or f"L{lid}"
    return f":white_check_mark: Removed *{match[0]}* from {host} auto-draft list."


@beta_tool
def seed_autodraft_from_top20(league_id: str, n: int = 20) -> str:
    """Bootstrap an auto-draft list by copying the current top-20 in priority order.

    Use this as a fast starting point — Scott can then add/remove/reorder. Replaces
    any existing list. `n` defaults to 20 (full list); pass smaller for just the top few.
    """
    from ffassist import autodraft, rankings_v2 as _r2

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    state = state_mod.load()
    ls = state.get("leagues", {}).get(lid, {})
    cached = ls.get("last_top20") or []
    if not cached:
        try:
            with MFLClient() as mfl:
                _text, cached = _r2.build_league_top_20(lid, mfl, settings)
        except Exception as e:
            return f":x: Could not build top-20 for L{lid}: {e!r}"
    if not cached:
        return f":x: No top-20 entries available for L{lid}."
    n = max(1, min(n, len(cached)))
    pids = [entry["player_id"] for entry in cached[:n]]
    autodraft.set_list(lid, pids)
    host = ls.get("display_name") or f"L{lid}"
    lines = [f":white_check_mark: Seeded {host} auto-draft list from top-{n}:"]
    for i, entry in enumerate(cached[:n], 1):
        lines.append(f"  {i:>2}. {entry['player_name']} ({entry['position']} {entry['team']})")
    return "\n".join(lines)


@beta_tool
def set_mfl_auto_mode(league_id: str, on: bool) -> str:
    """Toggle MFL auto-draft mode for a league.

    Turn ON when MFL has flipped Scott to auto-pick (no clock, MFL picks instantly).
    When ON, the bot races MFL by submitting from Scott's auto-draft list the
    moment he's on the clock. Turn OFF when Scott is manually drafting again.

    BEFORE turning on: confirm an auto-draft list exists for the league
    (call get_autodraft_list). If empty, warn Scott — MFL will pick for him.
    """
    from ffassist import autodraft

    lid = _resolve_league(league_id)
    if not lid:
        return f"Could not resolve league '{league_id}'."
    autodraft.set_auto_mode(lid, on)
    host = state_mod.load().get("leagues", {}).get(lid, {}).get("display_name") or f"L{lid}"
    state_label = "ON :robot_face:" if on else "off"
    extra = ""
    if on and not autodraft.get_list(lid):
        extra = "\n:warning: List is empty — add players or MFL will pick for you."
    return f"MFL auto-mode for {host}: *{state_label}*.{extra}"


@beta_tool
def propose_pick(player_id: str) -> str:
    """Propose a draft pick in the active on-the-clock thread.

    Sets a pending pick that Scott must confirm with 'yes' to actually submit.
    You do NOT submit picks directly — the confirmation flow is enforced outside your tools.
    Only works inside an active MFL draft thread.

    Args:
        player_id: MFL player ID from match_player (e.g. "12345").
    """
    ctx = _thread_ctx.get()
    if not ctx or ctx.get("kind") != "mfl_draft":
        return "Cannot propose a pick — not in an active draft thread."
    thread_ts = ctx["thread_ts"]
    state = state_mod.load()
    thread = state["threads"].get(thread_ts)
    if not thread:
        return "Thread context missing in state."
    year = thread.get("year", settings.mfl_year)
    with MFLClient(year=year) as mfl:
        players = parse_players(mfl.players())
    if player_id not in players:
        return f"Player id {player_id} not found in MFL player list."
    p = players[player_id]
    thread["pending"] = {
        "player_id": p.id,
        "name": p.name,
        "team": p.team,
        "position": p.position,
    }
    state["threads"][thread_ts] = thread
    state_mod.save(state)
    return (
        f"Pending pick set: {p.name} ({p.team} {p.position}). "
        "Tell Scott to reply 'yes' to confirm and submit."
    )


@beta_tool
def get_rules_summary(league_id: str, year: int = 0) -> str:
    """Get scoring rules for a league, summarized by position.

    Args:
        league_id: MFL league ID.
        year: Season year. 0 means use the default (current year).
    """
    from ffassist.rules_compare import _index_position_rules

    if not year:
        year = settings.mfl_year
    with MFLClient(year=year) as mfl:
        rules = mfl.rules(league_id)
    idx = _index_position_rules(rules)
    out = []
    for pos in sorted(idx):
        items = sorted(idx[pos].items())
        if not items:
            continue
        out.append(f"[{pos}]")
        for k, v in items[:30]:
            out.append(f"  {k}: {v}")
    return "\n".join(out) if out else "No rules found."


@beta_tool
def compare_rules(league_a: str, year_a: int, league_b: str, year_b: int) -> str:
    """Diff scoring rules between two MFL leagues (typically used to compare year-over-year)."""
    from ffassist.rules_compare import diff_position_rules, format_diff

    with MFLClient(year=year_a) as ca:
        a = ca.rules(league_a)
    with MFLClient(year=year_b) as cb:
        b = cb.rules(league_b)
    return format_diff(
        f"{league_a}@{year_a}", f"{league_b}@{year_b}", diff_position_rules(a, b)
    )


@beta_tool
def get_player_pick_history(player_query: str, limit: int = 30) -> str:
    """Show every individual pick of a player across all tracked Fantasy Cares Eliminator drafts.

    Use this to answer "where has X been going?" or "what's the range on Y?". Returns league,
    overall pick, round, who drafted them, when, and how long they took. Fuzzy-matches the name.

    Args:
        player_query: Player name (e.g. "Bowers", "Justin Jefferson", "JSN").
        limit: Max rows (default 30, cap 100).
    """
    from ffassist.eliminator_picks import picks_by_player_name

    limit = max(1, min(limit, 100))
    rows = picks_by_player_name(player_query, limit=limit)
    if not rows:
        return f"No eliminator-draft picks found for '{player_query}'."
    out = [f"All eliminator picks for '{player_query}' ({len(rows)} shown):"]
    for r in rows:
        ttp = r.get("time_to_pick", "?")
        out.append(
            f"  overall #{r.get('overall', '?'):>3} (R{int(r.get('round', 0))}.{int(r.get('pick', 0)):02d}) — "
            f"{r.get('player_name'):<25} {r.get('pos'):<3} {r.get('team'):<4}  "
            f"L{r.get('league_id')}  by {r.get('franchise_name')}  (took {ttp})"
        )
    return "\n".join(out)


@beta_tool
def get_picks_at_pick_number(overall_pick: int, window: int = 0) -> str:
    """Show what was drafted at a specific overall pick number across all eliminator leagues.

    Use this when Scott is about to pick at slot N and wants to know what's typically taken there.

    Args:
        overall_pick: The overall pick number (e.g. 9 for round 1 pick 9 in an 18-team league).
        window: +/- range. 0 = exact slot only, 2 = picks N-2 through N+2.
    """
    from ffassist.eliminator_picks import picks_in_overall_range, position_distribution_at_pick

    lo, hi = overall_pick - window, overall_pick + window
    rows = picks_in_overall_range(lo, hi)
    if not rows:
        return f"No picks logged in the overall pick {lo}-{hi} range yet."
    out = [f"Picks at overall #{lo}-{hi} across all eliminator leagues ({len(rows)} picks):"]
    dist = position_distribution_at_pick(overall_pick, window=window)
    out.append("  Position distribution: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])))
    out.append("")
    for r in rows:
        out.append(
            f"  #{r.get('overall'):>3}  L{r.get('league_id')}  "
            f"{r.get('player_name'):<25} {r.get('pos'):<3} {r.get('team'):<4}  by {r.get('franchise_name')}"
        )
    return "\n".join(out)


@beta_tool
def refresh_eliminator_picks() -> str:
    """Force-refresh the cached all_picks.csv from the spoonfull data repo."""
    from ffassist.eliminator_picks import cached, cache_status

    try:
        rows = cached(force_refresh=True)
        return f"Refreshed eliminator picks: {len(rows)} picks logged across all tracked drafts."
    except Exception as e:
        return f":x: Refresh failed: {e!r}"


@beta_tool
def get_eliminator_picks_status() -> str:
    """Show freshness and size of the cached eliminator picks data."""
    from ffassist.eliminator_picks import cache_status

    s = cache_status()
    if not s.get("present"):
        return "Eliminator picks cache is empty — will fetch on next query."
    age_min = s.get("age_sec", 0) // 60
    stale = " (stale, will refresh on next use)" if s.get("stale") else ""
    return f"Eliminator picks cache: {s.get('count')} picks, {age_min} min old{stale}."


@beta_tool
def refresh_eliminator_adp() -> str:
    """Force-refresh the self-aggregated MFL eliminator ADP (pulls all leagues, ~3-5 min)."""
    from ffassist.mfl_adp import cached_adp, cache_status

    try:
        adp = cached_adp(force_refresh=True)
        s = cache_status()
        meta = s.get("meta", {})
        return (
            f"Refreshed MFL-aggregated eliminator ADP: {len(adp)} players ranked. "
            f"Sourced from {meta.get('leagues_with_picks', '?')} of "
            f"{meta.get('total_leagues_discovered', '?')} discovered leagues "
            f"({meta.get('total_picks_aggregated', '?')} total picks). "
            f"Min drafts: {meta.get('min_drafts', '?')}."
        )
    except Exception as e:
        return f":x: Refresh failed: {e!r}"


@beta_tool
def get_eliminator_adp_status() -> str:
    """Show freshness and size of the self-aggregated MFL eliminator ADP cache."""
    from ffassist.mfl_adp import cache_status

    s = cache_status()
    if not s.get("present"):
        return "Eliminator ADP cache is empty — first query will populate it (~3-5 min)."
    age_min = s.get("age_sec", 0) // 60
    stale = " (STALE — will refetch on next use)" if s.get("stale") else ""
    meta = s.get("meta", {})
    return (
        f"Self-aggregated ADP: {s.get('count')} players, {age_min} min old{stale}. "
        f"Sources: {meta.get('leagues_with_picks', '?')} leagues, "
        f"{meta.get('total_picks_aggregated', '?')} picks."
    )


@beta_tool
def add_sfb_watch(pattern: str, one_shot: bool = True) -> str:
    """Watch for a new Scott Fish Bowl eliminator matching `pattern` (case-insensitive substring).

    When the SFB monitor next detects a new eliminator whose name contains `pattern`, the bot
    auto-signs Scott up via the SFB website (requires SFB credentials in .env). Posts confirmation
    to #fantasyfootball. If one_shot=True (default), the watch disables itself after one match.

    Example: pattern='Cooterdoodle' will fire as soon as 'Cooterdoodle 3' (or any new Cooterdoodle
    league) appears.
    """
    from ffassist import sfb_watch

    w = sfb_watch.add_watch(pattern, one_shot=one_shot)
    mode = "one-shot" if one_shot else "persistent"
    return f":eyes: Watching SFB for '*{pattern}*' ({mode}). Will auto-signup when it appears."


@beta_tool
def list_sfb_watches() -> str:
    """Show active SFB watches."""
    from ffassist import sfb_watch

    watches = sfb_watch.list_watches()
    if not watches:
        return "No SFB watches set."
    lines = ["*SFB watches:*"]
    for w in watches:
        status = "active" if w.active else f"USED ({w.used_eliminator_name})"
        mode = "one-shot" if w.one_shot else "persistent"
        lines.append(f"  '{w.pattern}' [{mode}, {status}]")
    return "\n".join(lines)


@beta_tool
def remove_sfb_watch(pattern: str) -> str:
    """Cancel an SFB watch by pattern."""
    from ffassist import sfb_watch

    ok = sfb_watch.remove_watch(pattern)
    return f"Removed watch '{pattern}'." if ok else f"No watch matching '{pattern}'."


@beta_tool
def set_blend_weights(weight_ppg: float, weight_adp: float) -> str:
    """Set the blend weights for 'best available' (default 0.6 PPG / 0.4 ADP).

    Weights are normalized to sum to 1.0. Both must be non-negative.
    """
    from ffassist import settings_store

    if weight_ppg < 0 or weight_adp < 0 or (weight_ppg + weight_adp) == 0:
        return ":x: Both weights must be non-negative and sum > 0."
    total = weight_ppg + weight_adp
    wp = weight_ppg / total
    wa = weight_adp / total
    settings_store.set_value("blend_weight_ppg", wp)
    settings_store.set_value("blend_weight_adp", wa)
    return f"blend weights set: PPG {wp:.2f}, ADP {wa:.2f}"


@beta_tool
def set_eliminator_only_adp(eliminator_only: bool) -> str:
    """Toggle whether rankings use ONLY eliminator ADP (default True) or layer MFL ADP underneath.

    Set True to ignore MFL ADP entirely — players without eliminator data show as unranked.
    Set False to include MFL ADP as a fallback (noisier this offseason but more coverage).
    Per-league FantasyPros CSVs are ALWAYS layered on top either way.
    """
    from ffassist import settings_store

    settings_store.set_value("eliminator_only_adp", bool(eliminator_only))
    return f"eliminator_only_adp = {bool(eliminator_only)}"


@beta_tool
def set_exclude_rookies(exclude: bool) -> str:
    """Toggle whether MFL ADP excludes rookies when ranking 'best available'.

    Set True to drop rookies from ADP rankings (so veteran players surface first), False
    to include them. Rookies are still draftable — they just won't appear at the top of
    'best available' lists when this is on. Useful because MFL's offseason ADP heavily
    skews toward dynasty rookie drafts.
    """
    from ffassist import settings_store

    settings_store.set_value("exclude_rookies_from_adp", bool(exclude))
    return f"exclude_rookies_from_adp = {bool(exclude)}"


@beta_tool
def get_bot_settings() -> str:
    """Show current bot-level settings (toggles for ADP behavior, etc.)."""
    from ffassist import settings_store

    s = settings_store.all_settings()
    return "\n".join(f"{k} = {v}" for k, v in s.items())


@beta_tool
def add_tracked_league(league_id: str) -> str:
    """Add a league to the active tracking list (so the on-the-clock poller will watch it).

    Use this when Scott wants the bot to start polling a league he just joined or discovered.
    Takes effect immediately — no service restart needed.
    """
    state = state_mod.load()
    dyn = list(state.get("tracked_leagues", []))
    if league_id in dyn or league_id in [
        x.strip() for x in settings.mfl_league_ids.split(",") if x.strip()
    ]:
        return f"L{league_id} is already tracked."
    dyn.append(league_id)
    state["tracked_leagues"] = dyn
    state_mod.save(state)
    return f"✓ Now tracking L{league_id}. On-the-clock alerts will fire when it's your turn."


@beta_tool
def remove_tracked_league(league_id: str) -> str:
    """Remove a league from the active tracking list.

    Note: leagues set via .env (MFL_LEAGUE_IDS) cannot be removed via this tool —
    only dynamically-added leagues can.
    """
    state = state_mod.load()
    dyn = list(state.get("tracked_leagues", []))
    env_ids = [x.strip() for x in settings.mfl_league_ids.split(",") if x.strip()]
    if league_id in env_ids and league_id not in dyn:
        return (
            f"L{league_id} is set in .env (MFL_LEAGUE_IDS) and can't be removed via this tool. "
            "Tell Scott to edit .env if he really wants it gone."
        )
    if league_id not in dyn:
        return f"L{league_id} isn't in the dynamic tracking list."
    dyn.remove(league_id)
    state["tracked_leagues"] = dyn
    state_mod.save(state)
    return f"✓ Stopped tracking L{league_id}."


@beta_tool
def get_all_my_mfl_leagues(year: int = 0, only_year: bool = True) -> str:
    """List EVERY league on Scott's MFL account (not just the ones we're actively tracking).

    Use this when Scott asks about all his leagues, his MFL history, or whether he's in leagues
    we haven't configured. Requires authenticated session — works automatically since we have his login.

    Args:
        year: Filter to a specific season year. 0 means use the current default year.
        only_year: If True (default), only return leagues for the given year. If False, return all years.
    """
    if not year:
        year = settings.mfl_year
    try:
        with MFLClient(year=year) as mfl:
            data = mfl.myleagues(year=year if only_year else None)
    except Exception as e:
        return f":x: Could not fetch myleagues: {e!r}"
    raw = data.get("league", []) if isinstance(data, dict) else data
    if isinstance(raw, dict):
        raw = [raw]
    if not raw:
        return f"No leagues found for {year}."
    lines = [f"All MFL leagues for Scott ({year}):"]
    tracked = set(settings.league_ids)
    for lg in raw:
        lid = lg.get("league_id") or lg.get("id") or "?"
        name = lg.get("name", "?")
        y = lg.get("year", year)
        marker = " (tracked)" if str(lid) in tracked else ""
        lines.append(f"  L{lid} [{y}]: {name}{marker}")
    return "\n".join(lines)


@beta_tool
def get_fantasypros_player(player_query: str) -> str:
    """Look up a player's FantasyPros consensus rank (ECR), tier, position rank,
    projected points (PPR), and any recent news.

    Use this when Scott asks "what's FantasyPros say about X" / "FP tier on Y" /
    "is there news on Z". This is OVERLAY context — it does NOT override Scott's
    own ranks. Fuzzy-matches the name against the MFL player directory, then maps
    to the FP id via the cached fp↔mfl id map.

    Args:
        player_query: Player name (e.g. "Bijan", "Justin Jefferson", "Kelce").
    """
    if not settings.fp_api_key:
        return "FantasyPros API not configured (FP_API_KEY not set in .env)."
    from ffassist import fantasypros as fp

    with MFLClient() as mfl:
        players = parse_players(mfl.players())
    names = {p.name: p for p in players.values()}
    match = process.extractOne(player_query, list(names.keys()), score_cutoff=70)
    if not match:
        return f"No MFL player matched '{player_query}' (fuzzy score < 70)."
    p = names[match[0]]
    try:
        enriched = fp.enrich_for_mflids([p.id])
    except fp.FPError as e:
        return f":x: FantasyPros error: {e}"
    e = enriched.get(p.id)
    if not e or e.fpid is None:
        return f"{p.display()} — no FantasyPros mapping (not in projections roster)."
    bits = [f"{p.display()}"]
    if e.ecr is not None:
        bits.append(f"ECR #{e.ecr}")
    if e.tier is not None:
        bits.append(f"Tier {e.tier}")
    if e.pos_rank:
        bits.append(f"{e.pos_rank}")
    if e.rank_min is not None and e.rank_max is not None:
        bits.append(f"min/max {e.rank_min}/{e.rank_max}")
    if e.rank_ave is not None:
        bits.append(f"ave {e.rank_ave:.2f}")
    if e.points_ppr is not None:
        bits.append(f"PPR pts {e.points_ppr:.1f}")
    if e.news_impact:
        bits.append("📰 recent news (call get_fantasypros_news for details)")
    return " · ".join(bits)


@beta_tool
def get_fantasypros_position_ecr(
    position: str,
    scoring: str = "PPR",
    type_: str = "draft",
    limit: int = 20,
) -> str:
    """FantasyPros consensus rankings (ECR) for a position. Use for sanity-checking
    Scott's own ranks at a position — DO NOT present this as a replacement.

    Args:
        position: QB/RB/WR/TE/K/DST/OP/ALL.
        scoring: PPR/HALF/STD (default PPR).
        type_: draft/weekly/ros/dynasty (default draft).
        limit: Max players (default 20, cap 50).
    """
    if not settings.fp_api_key:
        return "FantasyPros API not configured (FP_API_KEY not set in .env)."
    from ffassist import fantasypros as fp

    limit = max(1, min(limit, 50))
    try:
        data = fp.get_consensus(position=position, type_=type_, scoring=scoring)
    except fp.FPError as e:
        return f":x: FantasyPros error: {e}"
    out = [
        f"FantasyPros {position} {type_} {scoring} — "
        f"{data.get('count')} players, {data.get('total_experts')} experts, "
        f"updated {data.get('last_updated')}"
    ]
    for pl in data.get("players", [])[:limit]:
        ecr = pl.get("rank_ecr")
        tier = pl.get("tier")
        name = pl.get("player_name", "?")
        team = pl.get("player_team_id", "")
        pos = pl.get("player_position_id", "")
        rmin = pl.get("rank_min") or "-"
        rmax = pl.get("rank_max") or "-"
        out.append(f"  #{ecr} T{tier}  {name} ({pos} {team})  min/max {rmin}/{rmax}")
    used, day_limit = fp.calls_today()
    out.append(f"_(FP calls today: {used}/{day_limit})_")
    return "\n".join(out)


@beta_tool
def get_fantasypros_news(player_query: str = "", limit: int = 15) -> str:
    """Recent FantasyPros player news (last 24h-ish). Optional name filter.

    Use when Scott asks "any injury news?" / "what's the news on X?" / "any
    updates I should know about?".

    Args:
        player_query: Optional name to filter on (substring, case-insensitive).
        limit: Max items (default 15, cap 30).
    """
    if not settings.fp_api_key:
        return "FantasyPros API not configured (FP_API_KEY not set in .env)."
    from ffassist import fantasypros as fp

    limit = max(1, min(limit, 30))
    try:
        data = fp.get_news()
    except fp.FPError as e:
        return f":x: FantasyPros error: {e}"
    items = data.get("items", [])
    if player_query:
        needle = player_query.lower()
        items = [
            i for i in items
            if needle in (i.get("title", "") or "").lower()
            or needle in (i.get("desc", "") or "").lower()
        ]
    if not items:
        scope = f" matching '{player_query}'" if player_query else ""
        return f"No FantasyPros news items found{scope}."
    out = [f"FantasyPros news ({len(items)} items{' matching ' + player_query if player_query else ''}):"]
    for item in items[:limit]:
        when = item.get("created_formated", "")
        title = item.get("title", "")
        desc = (item.get("desc", "") or "")[:200]
        out.append(f"  [{when}] {title}")
        if desc:
            out.append(f"      {desc}")
    return "\n".join(out)


@beta_tool
def get_fantasypros_status() -> str:
    """Show FantasyPros cache freshness, daily call usage, and id-map size."""
    if not settings.fp_api_key:
        return "FantasyPros API not configured (FP_API_KEY not set in .env)."
    from ffassist import fantasypros as fp

    used, limit = fp.calls_today()
    mapping = fp.load_fp_to_mfl_map()
    cache_files = list(fp.CACHE_DIR.glob("*.json")) if fp.CACHE_DIR.exists() else []
    return (
        f"FantasyPros status:\n"
        f"  Daily calls: {used}/{limit}\n"
        f"  Cached responses: {len(cache_files)}\n"
        f"  fp↔mfl id map: {len(mapping)} entries\n"
        f"  Run `ffassist fp-refresh` to warm caches (uses ~14 calls)."
    )


@beta_tool
def set_auto_pick_rule(league_id: str, rule: str) -> str:
    """Enable auto-pick for a league with a natural-language rule describing how to pick.

    The bot warns Scott in-thread when <30 min remain on his pick clock, then submits at 10 min
    unless Scott replies 'stop'. The `rule` is interpreted by Claude at decision time, so it can
    be plain English (e.g. "prioritize best WR or TE flex, never kicker or QB"). Replacing an
    existing rule is fine — just call this again.

    Args:
        league_id: MFL league ID (e.g. "37714").
        rule: Natural-language rule for how to pick.
    """
    from ffassist import auto_pick

    auto_pick.set_rule(league_id, rule)
    return f"Auto-pick enabled for L{league_id}: {rule}"


@beta_tool
def disable_auto_pick(league_id: str) -> str:
    """Disable auto-pick for a league. The rule text is kept but inactive."""
    from ffassist import auto_pick

    ok = auto_pick.disable(league_id)
    return f"Auto-pick disabled for L{league_id}." if ok else f"No active rule for L{league_id}."


@beta_tool
def list_auto_pick_rules() -> str:
    """List all auto-pick rules across tracked leagues with their enabled state."""
    from ffassist import auto_pick

    rules = auto_pick.list_rules()
    if not rules:
        return "No auto-pick rules set."
    lines = []
    for r in rules:
        s = "enabled" if r.enabled else "disabled"
        lines.append(f"L{r.league_id} [{s}]: {r.rule}")
    return "\n".join(lines)


TOOLS = [
    get_status,
    get_on_the_clock,
    get_league_top_20,
    get_universal_best_available,
    get_available_players,
    get_my_picks,
    match_player,
    bootstrap_draft_thread,
    resync_on_clock_threads,
    get_autodraft_list,
    set_autodraft_list,
    add_to_autodraft_list,
    remove_from_autodraft_list,
    seed_autodraft_from_top20,
    set_mfl_auto_mode,
    propose_pick,
    get_rules_summary,
    compare_rules,
    get_all_my_mfl_leagues,
    add_tracked_league,
    remove_tracked_league,
    set_auto_pick_rule,
    disable_auto_pick,
    list_auto_pick_rules,
    set_exclude_rookies,
    set_eliminator_only_adp,
    set_blend_weights,
    get_available_players_by_adp_only,
    get_bot_settings,
    add_sfb_watch,
    list_sfb_watches,
    remove_sfb_watch,
    refresh_eliminator_adp,
    get_eliminator_adp_status,
    get_player_pick_history,
    get_picks_at_pick_number,
    refresh_eliminator_picks,
    get_eliminator_picks_status,
    get_fantasypros_player,
    get_fantasypros_position_ecr,
    get_fantasypros_news,
    get_fantasypros_status,
    get_player_outlook,
]


def _system_prompt(thread_ctx: dict | None) -> str:
    ids = settings.league_ids
    base = (
        "You are an MFL (myfantasyleague.com) fantasy football draft assistant for Scott Gerhardt "
        "(MFL username: thegamersdome). Scott plays in Fantasy Cares Eliminator slow drafts — "
        "best-ball, 16-player rosters, weekly low-scorer elimination. "
        "Flex = RB/WR/TE. Superflex = QB/RB/WR/TE.\n\n"
        f"Currently *tracked* MFL leagues (the ones the bot polls for on-the-clock alerts): "
        f"{', '.join(ids) if ids else '(none)'}\n"
        f"Default year: {settings.mfl_year}\n\n"
        "Scott may be in MORE leagues on MFL than the tracked list. Use get_all_my_mfl_leagues to "
        "enumerate his full account when he asks about 'all my leagues', 'my MFL history', or any "
        "league not in the tracked list. If he wants a league added to active tracking, tell him to "
        "add it to MFL_LEAGUE_IDS in .env (you can't do that yourself yet).\n\n"
        "Scott is a FantasyPros ranker, so don't oversell ADP — call out that MFL ADP is rookie-skewed "
        "this offseason if it comes up. Be CONCISE: terse answers, plain text or light Slack markdown only, "
        "no preamble like 'Great question!' Just answer.\n\n"
        "CRITICAL behavior: NEVER give meta-commentary about limits of data or what you 'would need'. "
        "Always call tools and give a CONCRETE answer. Examples:\n"
        "  ❌ 'The ADP cache is light; what's the context?'\n"
        "  ✅ <call get_available_players + get_picks_at_pick_number, then give a real top-5 list>\n"
        "If a league_id was mentioned anywhere earlier in the thread, USE IT — do not ask again. If no "
        "league is in context AND Scott doesn't specify, default to the first tracked league. Treat the "
        "full thread history as available memory; do NOT re-ask things Scott already told you.\n\n"
        "For draft picks: call match_player FIRST to confirm the player is available, then call propose_pick "
        "with the returned player_id. propose_pick only sets a PENDING pick — Scott must reply 'yes' to submit. "
        "You never submit picks directly. If you can't propose a pick (e.g. not in a draft thread), say so.\n\n"
        "For auto-pick rules: Scott can ask things like 'auto-pick for 37714 if I'm running out of time, "
        "prefer best WR/TE flex, never kicker'. Use set_auto_pick_rule(league_id, rule). The rule is plain "
        "English and gets re-interpreted at decision time. Use disable_auto_pick to turn off, "
        "list_auto_pick_rules to see what's set. When the bot eventually auto-picks, it warns at 30min "
        "remaining and submits at 10min unless Scott says 'stop' in the thread.\n\n"
        "SCORING (verified 2026-05-12, aggregated): All 3 tracked leagues use IDENTICAL FCEliminator 2026 "
        "scoring. Skill positions:\n"
        "  Pass yards 0.01/yd · Catch yards 0.15/yd · Rush yards 0.01/yd · All TDs 6 · Fumble lost -3\n"
        "  Reception (CC): QB 0.5, RB 0.5, WR 1.25, TE 2.5\n"
        "  Rush attempt (RA): QB 1.0, RB 0.75, WR 1.0, TE 3.5\n"
        "  1st-down catch (1C): QB 0.5, RB 0.5, WR 1.0, TE 1.5\n"
        "  1st-down rush (1R): QB 2.0, RB 1.5, WR 1.5, TE 3.0\n"
        "  QB INT -3 · 2-pt rush/pass 2, 2-pt catch 1\n"
        "TE IS MASSIVELY PREMIUM: 5x reception value of RB, 3.5x rush-attempt value of RB. Elite TE > "
        "elite WR > elite RB in this format. Receiving-back TEs are unicorns.\n"
        "D/ST tiered OPA (15/10/5/-2/-5) + TYA (10/5/0/-5/-10). Sack 2. Blocks 2. Coach W+10 / L-10 / "
        "point-diff 0.5.\n"
        "When Scott asks about scoring, lead with these facts. Only call get_rules_summary if Scott asks "
        "for raw MFL rules dump.\n\n"
        "ADP SOURCE (CRITICAL — Scott trusts these and ONLY these):\n"
        "1. Eliminator ADP (spoonfull's data repo, ~19 players currently) — PRIMARY trusted source.\n"
        "2. Per-league FantasyPros CSV at data/rankings/<league_id>.csv — secondary, used for sleepers/depth.\n"
        "3. MFL ADP fallback — OFF by default.\n\n"
        "RANKING SYSTEM (PRIMARY — Scott's spec):\n"
        "  RAW List: top 300 by 2025 PPG + rookies with ADP\n"
        "    Vets: raw_score = 0.6 × PPG_rank + 0.4 × raw_ADP\n"
        "    Rookies: raw_score = raw_ADP (100% ADP, no PPG)\n"
        "    Sort raw_score asc → raw_rank (max-tie rule: tied group → max position)\n"
        "  OVERALL Modified: overall_score = raw_score × (1 + 0.05 × dupes_across_all_leagues)\n"
        "  LEAGUE Adjusted: league_score = overall_score × (1 + (3^N)/100), N≥1\n"
        "    where N = players on Scott's roster in THIS league sharing prospective player's bye\n"
        "    Pre-Thursday: same-team is the bye proxy (Gibbs+Goff both DET = N=1 if Scott has one)\n"
        "  Display: top 20 available, format: 'N. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins} [ECR/PosRk]'\n"
        "  No-ADP suppression: if any top-20 player has ADP > current_pick+15, players without recorded ADP are dropped from the list.\n"
        "PRIMARY TOOL: get_league_top_20(league_id) — returns the league-adjusted top 20.\n"
        "Top-20s are auto-built on each on-the-clock alert (30-min dedupe). If the cached list is\n"
        "fresh (<30 min), it's reused; otherwise the tool computes a fresh one on demand. The\n"
        "league_id argument accepts EITHER the numeric MFL id ('37714') OR the host's name\n"
        "('Matt Harmon', 'Liz Loza', 'Joey Wright', 'Jen Piacenti'). Prefer the host name in\n"
        "questions and conversation — Scott refers to leagues by their host. Other ranking tools\n"
        "(get_universal_best_available, get_available_players) exist for cross-league or ADP-only\n"
        "questions; use get_league_top_20 for 'top in <league>' or 'top in <host>'.\n"
        "DRAFT-BY-NUMBER: If Scott replies with just a number (1-20) in a draft thread after seeing the\n"
        "top 20, that means draft that rank. Look up the player from thread state ['last_top20'], confirm\n"
        "with 'Pick X (Pos Team)?', and on 'yes' call propose_pick.\n\n"
        "CRITICAL — CROSS-LEAGUE CONSISTENCY: All 4 leagues use IDENTICAL scoring with no roster splits, "
        "so the underlying ranking is the SAME across all leagues. Per-league differences come ONLY from "
        "(a) who's already been drafted in that league, and (b) the diversification bias on Scott's own "
        "duplicates. NEVER re-sort or curate the tool output — present it EXACTLY in the order returned. "
        "If two leagues both have player X and player Y available, X comes before Y in BOTH lists (or "
        "neither). Do not flip orderings. Do not silently drop players. Do not 'pick interesting ones'.\n\n"
        "For 'top N across all my leagues' or 'top N by value', use get_universal_best_available — one "
        "global ranking. For 'top N available in league X', use get_available_players(X) — same ranking, "
        "filtered to undrafted-in-X.\n\n"
        "ROOKIES: 2026 rookies have no 2025 PPG, so they're ranked on 100% ADP (no PPG penalty). The "
        "[ROOKIE: 100% ADP] tag appears in tool output for clarity.\n\n"
        "DIVERSIFICATION BIAS: Each player Scott has already drafted in another tracked league gets their "
        "blended rank multiplied by (1 + 0.05*N) where N = times drafted. Prevents over-concentration. "
        "Tool output shows the bias in the player line when applicable.\n\n"
        "PICK HISTORY: The bot also has access to every individual pick made across all FC Eliminator "
        "drafts (all_picks.csv, ~250+ picks and growing, refreshes hourly). Use:\n"
        "- get_player_pick_history(name): every pick of a specific player — answers 'where has X been "
        "going?'. Shows pick #, league, drafter, time-to-pick.\n"
        "- get_picks_at_pick_number(overall_pick, window=N): what's typically taken at pick slot X "
        "across leagues. Window=2 means ±2 picks. Answers 'what should I expect to be available at 9?'.\n"
        "- refresh_eliminator_picks / get_eliminator_picks_status for cache management.\n"
        "Use these proactively when Scott asks about player value, pick range, or 'who's gone by now'.\n\n"
        "=== HARD RULE: PLAYER OUTLOOK / NEWS / STATUS QUESTIONS ===\n"
        "If Scott asks ANY of: 'outlook on X', 'how is X looking', 'news on X', 'status of X',\n"
        "'movement on X', 'should I draft X', 'what's happening with X', 'is X healthy', or any\n"
        "similar phrasing about a SPECIFIC named player — you MUST call get_player_outlook(name)\n"
        "BEFORE answering. NEVER answer player-outlook questions from memory or by reasoning over\n"
        "blended PPG tool output alone — that PPG number can be a 10%-discounted fallback from a\n"
        "PRIOR season (e.g. a player who missed all of 2025 will show ~90% of their 2024 PPG, NOT\n"
        "actual 2025 performance). The get_player_outlook tool surfaces per-year games-played, FP\n"
        "ECR, projections, FA status, and recent news in one call. After calling it, present what\n"
        "it returns honestly — if it says 'DID NOT PLAY 2025', say so explicitly. If FantasyPros has\n"
        "no projection (FA, unsigned, retired), say so explicitly. NEVER fabricate a 'PPG in 2025'\n"
        "for a player who didn't play in 2025.\n\n"
        "Also: if you see `[DID NOT PLAY 2025 — number is 2024×0.9 fallback]` or `[LIMITED 2025]`\n"
        "appended to any best-available row, the PPG number on that row is NOT real 2025 production.\n"
        "Flag this in your response if it affects the answer.\n\n"
        "FANTASYPROS OVERLAY (read-only context, NEVER overrides Scott's ranks):\n"
        "- get_fantasypros_player(name): FP consensus rank (ECR), tier, position rank, projected\n"
        "  points (PPR), and a news flag for one player. Use when Scott asks 'what's FP say about X'\n"
        "  or 'FP tier on Y'.\n"
        "- get_fantasypros_position_ecr(POS): FP top-N at a position. Use ONLY for sanity-checking\n"
        "  ('what does the consensus look like at TE this year?') — never present as the authoritative\n"
        "  ranking. Scott is a FantasyPros ranker himself, so don't repackage their ranks as 'the answer'.\n"
        "- get_fantasypros_news(player_query=''): recent FP news, optionally filtered by name.\n"
        "  Use for 'any injury news', 'what happened to X', 'updates on my queue'.\n"
        "- get_fantasypros_status(): cache freshness and daily call budget.\n"
        "The on-the-clock top-20 already shows FP tier/ECR/POSrank tags inline ([T1 ECR#2 RB1]); 📰\n"
        "next to a player means there's recent news on them — call get_fantasypros_news for details.\n"
        "Daily limit is 100 FP calls — most queries are cache hits, but don't run get_fantasypros_position_ecr\n"
        "in a tight loop."
    )
    if thread_ctx and thread_ctx.get("kind") == "mfl_draft":
        base += (
            f"\n\nCURRENT THREAD: This is the active on-the-clock thread for league "
            f"{thread_ctx['league_id']}, pick #{thread_ctx.get('pick_number', '?')}, "
            f"round {thread_ctx.get('round', '?')}. Scott IS on the clock here — that's "
            "why this thread exists. NEVER ask 'are you on the clock' or 'which league' — "
            "the league is this thread's league. If Scott says 'this league', he means "
            f"{thread_ctx['league_id']}. If he says 'draft X', call match_player with "
            f"league_id='{thread_ctx['league_id']}' then propose_pick — do not chitchat about it. "
            "If you need authoritative on-the-clock info across all leagues, call get_on_the_clock "
            "— do not speculate or fabricate pick numbers."
        )
    elif thread_ctx and thread_ctx.get("kind") == "sfb_eliminator":
        base += (
            "\n\nCURRENT THREAD: Scott Fish Bowl eliminator signup thread. MFL tools don't apply; "
            "the signup flow is handled by separate commands ('join', 'yes', 'no')."
        )
    else:
        base += (
            "\n\nCURRENT CONTEXT: Top-level message (channel or DM), no active draft thread. "
            "You can answer questions but cannot propose picks (no thread to set pending state in). "
            "If Scott asks whether he's on the clock anywhere, ALWAYS call get_on_the_clock for "
            "ground truth. Never speculate about pick numbers, who's up next, or whether he's "
            "made picks — call the tools."
        )
    # HARD RULE — applies in every context. Confabulating a draft submission is
    # the worst possible failure mode: Scott misses a pick because he trusted the bot.
    base += (
        "\n\n=== HARD RULE: DRAFT CONFIRMATIONS ===\n"
        "You MUST NEVER write text containing 'Proposing:', 'Reply yes to submit', "
        "'drafted to', 'pick submitted', a green checkmark next to a player name, or any other "
        "draft-confirmation prose. These messages are ONLY produced by:\n"
        "  1. The `propose_pick` tool's literal return value (the 'Pending pick set: ...' string)\n"
        "  2. The internal `_submit_pending` code path (which actually calls MFL's API)\n"
        "If Scott asks to draft a player and you do not have an active 'mfl_draft' thread context, "
        "you have TWO options and ONLY these two:\n"
        "  (a) Call `bootstrap_draft_thread(league_id)` to create the thread. Use this when Scott "
        "      mentions a specific league and is on the clock there per get_on_the_clock.\n"
        "  (b) If you cannot bootstrap (no league specified or not on clock anywhere), reply with "
        "      EXACTLY: ':warning: I can\\'t submit a pick from here — no active draft thread. "
        "      Make the pick on MFL directly, or tell me which league and I\\'ll try to bootstrap.'\n"
        "Do NOT generate any prose that LOOKS like a confirmation, even informally. If you do not "
        "have the propose_pick tool's actual return value to relay, you have not done anything yet. "
        "Saying you submitted a pick when you didn't is a critical failure."
    )
    return base


def handle_message(
    user_text: str,
    thread_ctx: dict | None = None,
    history: list[dict] | None = None,
) -> str:
    """Send a user message to Claude with tool use; optionally include thread history."""
    if not settings.anthropic_api_key:
        return "(NLP disabled: ANTHROPIC_API_KEY not set)"
    client = Anthropic(api_key=settings.anthropic_api_key)
    token = _thread_ctx.set(thread_ctx)

    # Build messages array: history (if any) + current turn
    if history:
        # Trust the history (already cleaned to alternate and start with user)
        messages = list(history)
        # If the last message in history is the current user_text (already included), don't dup
        if messages and messages[-1]["role"] == "user" and messages[-1]["content"].strip() == user_text.strip():
            pass
        else:
            messages.append({"role": "user", "content": user_text})
    else:
        messages = [{"role": "user", "content": user_text}]

    try:
        runner = client.beta.messages.tool_runner(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=_system_prompt(thread_ctx),
            tools=TOOLS,
            messages=messages,
        )
        final = None
        for message in runner:
            final = message
        if final is None:
            return "(no response)"
        texts = [b.text for b in final.content if b.type == "text"]
        return "\n".join(t for t in texts if t) or "(no text content)"
    except AuthenticationError:
        return ":x: Anthropic API key is invalid."
    except RateLimitError:
        return ":x: Anthropic rate limited — try again in a moment."
    except BadRequestError as e:
        logger.exception("NLP bad request")
        return f":x: NLP request error: {e}"
    except APIError as e:
        logger.exception("NLP API error")
        return f":x: NLP API error: {e}"
    except Exception as e:
        msg = str(e).lower()
        if "429" in msg or "rate-limit" in msg or "rate limit" in msg:
            return ":warning: MFL is rate-limiting us right now — try again in 60s. (Scopes/data are fine; this is a temporary throttle.)"
        logger.exception("NLP unexpected error")
        return f":x: NLP error: {e!r}"
    finally:
        _thread_ctx.reset(token)
