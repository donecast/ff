"""Three-list ranking system per user spec (2026-05-12).

Raw List:
    top 300 by 2025 PPG + 2026 rookies with ADP
    PPG rank uses max-tie rule (4th & 5th tied → both rank 5, 6th stays at 6)
    Vets: raw_score = 0.6 * PPG_rank + 0.4 * raw_ADP
    Rookies: raw_score = raw_ADP (100% ADP)
    Vets with no ADP: ADP = max_ADP + 1 (so they sort low on ADP component)
    Sort by raw_score ASC → raw_rank (max-tie)

Overall Modified List:
    overall_score = raw_score * (1 + 0.05 * dupes_across_all_leagues)
    Sort → overall_rank

League Adjusted List (per league):
    league_score = overall_score * (1 + (3^N)/100) when N >= 1, else overall_score
        where N = count of my roster in THAT league sharing prospective player's bye
    Pre-Thursday: use same-team as bye proxy
    Sort → league_rank
    For display: filter out drafted-in-this-league, present in same order with
    new sequential numbering (1..20).

Display format (2026-05-15):
    'N. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins}  [ECR/PosRk]'
    fce = number of tracked Fantasy Cares Eliminator drafts this player has been
          selected in (one count per league, deduped). Scott weights pick-frequency
          when judging the list — higher means more drafters agreed on the player.
    No-ADP suppression: if any top-N player has ADP >= current_pick + 15,
    players without recorded ADP are dropped from the displayed list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ffassist.draft_state import Player


_BYES_CACHE: dict[str, int] | None = None


def load_byes() -> dict[str, int]:
    """Load 2026 NFL bye weeks from data/byes_2026.json. {} if file missing."""
    global _BYES_CACHE
    if _BYES_CACHE is not None:
        return _BYES_CACHE
    path = Path(__file__).resolve().parents[2] / "data" / "byes_2026.json"
    try:
        _BYES_CACHE = json.loads(path.read_text()).get("byes", {})
    except FileNotFoundError:
        _BYES_CACHE = {}
    return _BYES_CACHE


@dataclass
class RankEntry:
    player_id: str
    player_name: str
    position: str
    team: str
    is_rookie: bool

    ppg: float | None
    ppg_rank: int | None  # None for rookies (no 2025 PPG)
    adp: float | None  # raw average pick or None if not in eliminator data
    fce_picks: int = 0  # # of tracked FCE drafts this player was selected in

    raw_score: float = 0.0
    raw_rank: int = 0
    dupes: int = 0
    overall_score: float = 0.0
    overall_rank: int = 0
    bye_twins: int = 0
    league_score: float = 0.0
    league_rank: int = 0


def _max_tie_ranks_asc(values: list[float]) -> list[int]:
    """For values sorted ASC (lower = better), assign max-tie ranks.

    [1.0, 2.0, 3.0, 3.0, 5.0] → [1, 2, 4, 4, 5]
    Tied group gets the max position within the group; next continues from there+1.
    """
    n = len(values)
    ranks = [0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        max_position = j + 1  # 1-based
        for k in range(i, j + 1):
            ranks[k] = max_position
        i = j + 1
    return ranks


def _max_tie_ranks_desc(values: list[float]) -> list[int]:
    """For values sorted DESC (higher = better), assign max-tie ranks.

    [39, 38, 34, 34, 28] → [1, 2, 4, 4, 5]
    """
    return _max_tie_ranks_asc(values)  # same logic — ties get the max position


import time as _time

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "Def", "PN", "Coach"}
SEASON_GAMES = 17
MAX_INJURY_PENALTY = 0.10  # cap injury multiplier at -10%


def compute_ppg_ranks(ppg: dict[str, float]) -> dict[str, int]:
    """Map player_id → PPG rank (1 = highest), max-tie rule."""
    sorted_pids = sorted(ppg.keys(), key=lambda pid: -ppg[pid])
    sorted_vals = [ppg[pid] for pid in sorted_pids]
    ranks = _max_tie_ranks_desc(sorted_vals)
    return dict(zip(sorted_pids, ranks))


def _injury_multiplier(games_played: int) -> float:
    """Legacy single-year multiplier. Kept for back-compat; new logic uses combined."""
    if games_played <= 0:
        return 0.0
    missed = max(0, SEASON_GAMES - games_played)
    return max(1.0 - MAX_INJURY_PENALTY, 1.0 - 0.01 * missed)


def _combined_injury_multiplier(gp25: int, gp24: int) -> float:
    """Total missed games across 2025+2024, 1%/game, cap 10%. Applied to BOTH years' PPG.

    Punishes year-long-out players (Mixon 12+0 missed 22 → 0.90) more than a single
    short year (Kittle 9+15 missed 10 → 0.90), but the cap protects everyone from
    being zeroed out.
    """
    combined_missed = max(0, 2 * SEASON_GAMES - (gp25 or 0) - (gp24 or 0))
    return max(1.0 - MAX_INJURY_PENALTY, 1.0 - 0.01 * combined_missed)


def _adjusted_ppg(ppg: float, games_played: int) -> float:
    """Legacy single-year injury-adjusted PPG (kept for nlp tool callers)."""
    return ppg * _injury_multiplier(games_played)


def _age_years(birth_ts: str | int) -> float | None:
    """Player age in years (float), from a UNIX-timestamp birthdate string."""
    if not birth_ts:
        return None
    try:
        ts = int(birth_ts)
    except (TypeError, ValueError):
        return None
    return (_time.time() - ts) / (365.25 * 86400)


def _age_multiplier(age: float | None, position: str) -> float:
    """Cumulative age-regression discount applied to PPG.

    QB: no regression (Scott's rule).
    WR/TE: +1% at ages 30,31; +2% at 32,33; +3% per year at 34+.
    RB:    same curve shifted -2 years (cliff starts at 28).
    Def/PN/Coach: none.
    """
    if age is None or position == "QB" or position not in {"RB", "WR", "TE"}:
        return 1.0
    cliff_start = 28 if position == "RB" else 30
    age_int = int(age)
    if age_int < cliff_start:
        return 1.0
    penalty = 0.0
    for y in range(age_int - cliff_start + 1):
        if y < 2:
            penalty += 0.01
        elif y < 4:
            penalty += 0.02
        else:
            penalty += 0.03
    return max(0.5, 1.0 - penalty)


def _blended_ppg(
    year_data: dict, age: float | None, position: str
) -> tuple[float | None, dict]:
    """Combine 2025 & 2024 per-year stats into a single forward-looking PPG.

    Order of operations:
       1. Combined injury multiplier — TOTAL missed games across both years, 1%/game,
          cap 10% — applied uniformly to both years' PPG.
       2. Blend 0.6 × adj_2025 + 0.4 × adj_2024 (fall back to whichever exists).
       3. Age multiplier (forward-looking, applied AFTER blend).
    """
    v25 = year_data.get("2025", {})
    v24 = year_data.get("2024", {})
    gp25 = int(v25.get("games_played", 0) or 0)
    gp24 = int(v24.get("games_played", 0) or 0)
    p25 = v25.get("ppg") if gp25 > 0 else None
    p24 = v24.get("ppg") if gp24 > 0 else None
    if p25 is None and p24 is None:
        return None, {
            "p25": p25, "p24": p24, "gp25": gp25, "gp24": gp24,
            "blend": None, "age_mult": 1.0,
        }
    mult = _combined_injury_multiplier(gp25, gp24)
    adj25 = p25 * mult if p25 is not None else None
    adj24 = p24 * mult if p24 is not None else None
    if adj25 is not None and adj24 is not None:
        blend = 0.6 * adj25 + 0.4 * adj24
    else:
        blend = adj25 if adj25 is not None else adj24
    age_mult = _age_multiplier(age, position)
    final = blend * age_mult
    return final, {
        "p25": p25, "p24": p24, "gp25": gp25, "gp24": gp24,
        "injury_mult": mult, "adj25": adj25, "adj24": adj24,
        "blend": blend, "age": age, "age_mult": age_mult, "final": final,
    }


FA_ALLOWED_FROM_ROUND = 11  # FAs only eligible starting round 11


def _fa_allowed(current_overall_pick: int | None, team_count: int | None) -> bool:
    """True if free agents are eligible at the current overall pick."""
    if current_overall_pick is None or not team_count:
        return False
    first_allowed_pick = (FA_ALLOWED_FROM_ROUND - 1) * team_count + 1
    return current_overall_pick >= first_allowed_pick


def compute_raw_list(
    players: dict[str, Player],
    ppg_data: dict[str, dict],
    adp: dict[str, float],
    top_n_by_ppg: int = 300,
    current_overall_pick: int | None = None,
    team_count: int | None = None,
) -> list[RankEntry]:
    """Build the Raw List.

    `ppg_data` is {pid: {"2025": {ppg,gp,total}, "2024": {...}}}.

    Per-player pipeline:
      1. Per-year injury multiplier (1%/missed game, cap 10%)
      2. Blend: 0.6 × adj_2025 + 0.4 × adj_2024 (fall back if one missing)
      3. Age multiplier (QB none; WR/TE cliff at 30; RB cliff at 28)
      4. Final PPG → ranked
      5. Vet raw_score = 0.4 × PPG_rank + 0.6 × ADP. Rookies + zero-stat vets = 100% ADP.
    """
    fa_ok = _fa_allowed(current_overall_pick, team_count)

    try:
        from ffassist.eliminator_picks import picks_count_by_player_id
        fce_counts = picks_count_by_player_id()
    except Exception:
        fce_counts = {}

    def is_fa(pid: str) -> bool:
        pl = players.get(pid)
        return bool(pl and (pl.team or "").upper() == "FA")

    # 1-3: Build final adjusted-PPG map for skill-position vets with any signal
    adjusted_ppg: dict[str, float] = {}
    debug_by_pid: dict[str, dict] = {}
    for pid, info in ppg_data.items():
        p = players.get(pid)
        if not p or p.position not in SKILL_POSITIONS:
            continue
        if not fa_ok and (p.team or "").upper() == "FA":
            continue  # FA gated out until round 11
        age = _age_years(p.birthdate)
        final, dbg = _blended_ppg(info, age, p.position)
        if final is None or final <= 0:
            continue
        adjusted_ppg[pid] = final
        debug_by_pid[pid] = dbg

    all_ppg_ranks = compute_ppg_ranks(adjusted_ppg)

    sorted_by_ppg = sorted(adjusted_ppg.keys(), key=lambda pid: -adjusted_ppg[pid])
    top_pids = set(sorted_by_ppg[:top_n_by_ppg])

    rookie_pids = {
        pid for pid, p in players.items()
        if p.is_rookie and p.position in SKILL_POSITIONS and pid in adp
        and (fa_ok or (p.team or "").upper() != "FA")
    }
    # Vet who played 0 games in BOTH years and has ADP — Mixon rule
    zero_stat_vets = {
        pid for pid, info in ppg_data.items()
        if pid in adp
        and pid in players
        and players[pid].position in SKILL_POSITIONS
        and not players[pid].is_rookie
        and (fa_ok or (players[pid].team or "").upper() != "FA")
        and int(info.get("2025", {}).get("games_played", 0) or 0) == 0
        and int(info.get("2024", {}).get("games_played", 0) or 0) == 0
    }
    eligible = top_pids | rookie_pids | zero_stat_vets

    adp_vals = list(adp.values())
    no_adp_value = (max(adp_vals) + 1) if adp_vals else 100.0

    entries: list[RankEntry] = []
    for pid in eligible:
        p = players.get(pid)
        if not p or p.position not in SKILL_POSITIONS:
            continue
        p_adp_real = adp.get(pid)
        p_adp_effective = p_adp_real if p_adp_real is not None else no_adp_value
        ppg_r = all_ppg_ranks.get(pid)
        adj_ppg = adjusted_ppg.get(pid)
        has_signal = pid in adjusted_ppg

        if p.is_rookie or not has_signal:
            if p_adp_real is None:
                continue
            raw_score = p_adp_real
            ppg_r_stored = None
            ppg_stored = None
        else:
            if ppg_r is None:
                continue
            raw_score = 0.4 * ppg_r + 0.6 * p_adp_effective
            ppg_r_stored = ppg_r
            ppg_stored = adj_ppg

        entries.append(
            RankEntry(
                player_id=pid,
                player_name=p.name,
                position=p.position,
                team=p.team,
                is_rookie=p.is_rookie,
                ppg=ppg_stored,
                ppg_rank=ppg_r_stored,
                adp=p_adp_real,
                fce_picks=fce_counts.get(pid, 0),
                raw_score=raw_score,
            )
        )

    entries.sort(key=lambda e: e.raw_score)
    raw_ranks = _max_tie_ranks_asc([e.raw_score for e in entries])
    for e, r in zip(entries, raw_ranks):
        e.raw_rank = r
    return entries


def apply_overall_modified(
    entries: list[RankEntry], dupes: dict[str, int]
) -> list[RankEntry]:
    """Compute overall_score = raw_score * (1 + 0.05 * dupes); set overall_rank."""
    for e in entries:
        n = dupes.get(e.player_id, 0)
        e.dupes = n
        e.overall_score = e.raw_score * (1 + 0.05 * n)
    entries.sort(key=lambda e: e.overall_score)
    ranks = _max_tie_ranks_asc([e.overall_score for e in entries])
    for e, r in zip(entries, ranks):
        e.overall_rank = r
    return entries


def apply_league_adjusted(
    entries: list[RankEntry],
    league_id: str,
    my_drafted_in_league: list[Player],
    byes: dict[str, int] | None = None,
) -> list[RankEntry]:
    """Compute league_score with bye-week multiplier; set league_rank.

    Pre-Thursday (byes is None or empty for a team): use same-team as proxy.
    """
    use_byes = bool(byes)

    # Cache my-drafted bye/team data
    if use_byes:
        my_byes = [byes.get(p.team) for p in my_drafted_in_league if byes.get(p.team)]
    else:
        my_teams = [p.team for p in my_drafted_in_league]

    for e in entries:
        if use_byes:
            this_bye = byes.get(e.team)
            n = sum(1 for b in my_byes if b == this_bye) if this_bye else 0
        else:
            n = my_teams.count(e.team)
        e.bye_twins = n
        if n == 0:
            e.league_score = e.overall_score
        else:
            e.league_score = e.overall_score * (1 + (3 ** n) / 100)

    entries.sort(key=lambda e: e.league_score)
    ranks = _max_tie_ranks_asc([e.league_score for e in entries])
    for e, r in zip(entries, ranks):
        e.league_rank = r
    return entries


def build_league_top_20(
    league_id: str,
    mfl_client,
    settings_module,
) -> tuple[str, list[dict]]:
    """Compute and format the league-adjusted top 20 for a league.

    Returns (formatted_text, presented_list_of_dicts) for state storage.
    """
    from ffassist.draft_state import parse_picks, my_picks, parse_players
    from ffassist.poller import find_my_franchise
    from ffassist.ppg_2025 import cached as cached_ppg
    from ffassist.rankings import (
        get_filtered_adp,
        load_csv_override,
        merge_rankings,
        my_drafted_counts,
    )

    from ffassist.draft_state import (
        extract_draft_order as _edo,
        next_pick_number as _npn,
    )

    players_lookup = parse_players(mfl_client.players())
    league = mfl_client.league(league_id)
    dr_full = mfl_client.draft_results(league_id)
    picks = parse_picks(dr_full)
    order = _edo(dr_full)
    team_count = len(order) if order else (
        len(league.get("franchises", {}).get("franchise", []))
        if isinstance(league.get("franchises", {}).get("franchise", []), list)
        else 0
    )
    current_pick = _npn(picks, team_count=team_count) if team_count else None
    adp = get_filtered_adp(mfl_client.adp(), players_lookup)
    dupes = my_drafted_counts(
        mfl_client, settings_module.league_ids, settings_module.mfl_username
    )
    my_id = find_my_franchise(league, settings_module.mfl_username)

    ppg_data = cached_ppg()
    overrides = load_csv_override(league_id, players=players_lookup)
    adp_eff = merge_rankings(adp, overrides)

    raw = compute_raw_list(
        players_lookup, ppg_data, adp_eff, top_n_by_ppg=300,
        current_overall_pick=current_pick, team_count=team_count,
    )
    overall = apply_overall_modified(raw, dupes)

    mine_in_league = [
        players_lookup[p.player_id]
        for p in my_picks(picks, my_id)
        if my_id and p.player_id in players_lookup
    ]
    drafted_in_league = {p.player_id for p in picks}

    league_adj = apply_league_adjusted(overall, league_id, mine_in_league, byes=load_byes())
    text, presented = format_top_n(league_adj, drafted_in_league, n=20, current_pick=current_pick)

    # FantasyPros overlay (tier + news) — additive; silent on any failure.
    fp_tags = _fp_tags_for(presented)
    if fp_tags:
        text = _append_fp_tags(text, presented, fp_tags)

    presented_dicts = [
        {
            "rank": i + 1,
            "player_id": e.player_id,
            "player_name": e.player_name,
            "position": e.position,
            "team": e.team,
            "fp": fp_tags.get(e.player_id) if fp_tags else None,
        }
        for i, e in enumerate(presented)
    ]
    return text, presented_dicts


def _fp_tags_for(presented: list["RankEntry"]) -> dict[str, str]:
    """Map mfl player_id -> short FP tag string. Empty dict if FP unavailable."""
    try:
        from ffassist.config import settings as _s
        if not _s.fp_api_key:
            return {}
        from ffassist import fantasypros as fp
        mflids = [e.player_id for e in presented]
        enriched = fp.enrich_for_mflids(mflids, scoring="PPR", type_="draft")
    except Exception:
        return {}
    tags: dict[str, str] = {}
    for pid, en in enriched.items():
        # Format: ECR/PosRk (e.g., "127/TE15"). Tier and news icon dropped per
        # user feedback 2026-05-15 — tier is too subjective to surface inline.
        if en.ecr is None and not en.pos_rank:
            continue
        ecr_s = str(en.ecr) if en.ecr is not None else "-"
        pos_s = en.pos_rank or "-"
        tags[pid] = f"{ecr_s}/{pos_s}"
    return tags


def _append_fp_tags(text: str, presented: list["RankEntry"], tags: dict[str, str]) -> str:
    """Append the FP tag to each top-20 line. Lines without a tag are left alone."""
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if i < len(presented):
            tag = tags.get(presented[i].player_id)
            if tag:
                line = f"{line}  [{tag}]"
        out.append(line)
    return "\n".join(out)


def format_top_n(
    entries: list[RankEntry],
    drafted_pids_in_league: set[str],
    n: int = 20,
    current_pick: int | None = None,
) -> tuple[str, list[RankEntry]]:
    """Filter out drafted-in-league, take top N, format per user spec.

    Returns (text, presented_entries) — second is the list in display order so the
    caller can store it for "reply with number" lookups.

    No-ADP suppression rule (added 2026-05-15): if `current_pick` is provided AND
    any candidate in the top-N has ADP > current_pick + 15, drop players without
    a recorded ADP from the available pool, then re-take top N. The idea: when
    the list already includes someone going much later than us, the "mystery"
    no-ADP players (often PNs / DSTs / rookies with no signal) add only noise.

    Format: 'rank. Name (Pos, Team) (PPG_rank/ADP/Raw_rank-fce) {dupes/bye_twins}'
    where `fce` = # of tracked FCE drafts this player has been selected in.
    """
    available = [e for e in entries if e.player_id not in drafted_pids_in_league]
    top = available[:n]
    if current_pick is not None:
        threshold = current_pick + 15
        if any(e.adp is not None and e.adp >= threshold for e in top):
            available = [e for e in available if e.adp is not None]
            top = available[:n]
    lines = []
    for i, e in enumerate(top, start=1):
        adp_s = f"{e.adp:.2f}" if e.adp is not None else "—"
        ppg_rank_s = str(e.ppg_rank) if e.ppg_rank is not None else "R"
        fce_s = str(e.fce_picks)
        lines.append(
            f"{i}. {e.player_name} ({e.position}, {e.team}) "
            f"({ppg_rank_s}/{adp_s}/{e.raw_rank}-{fce_s}) "
            f"{{{e.dupes}/{e.bye_twins}}}"
        )
    return "\n".join(lines), top
