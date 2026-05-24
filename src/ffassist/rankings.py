from __future__ import annotations

import csv
from pathlib import Path

from rapidfuzz import fuzz, process

from ffassist.config import DATA_DIR
from ffassist.draft_state import Player

OVERRIDES_DIR = DATA_DIR / "rankings"

_TEAM_ALIASES = {
    "JAX": "JAC",
    "WSH": "WAS",
    "LA": "LAR",
    "OAK": "LV",
    "SF": "SFO",
    "GB": "GBP",
    "TB": "TBB",
    "KC": "KCC",
    "NO": "NOS",
    "NE": "NEP",
}


def _normalize_team(t: str) -> str:
    t = (t or "").upper().strip()
    return _TEAM_ALIASES.get(t, t)


def _normalize_pos(p: str) -> str:
    p = (p or "").upper().strip()
    if p in {"DST", "D/ST", "DEF"}:
        return "Def"
    if p == "PK":
        return "K"
    return p


def _cols(rows: list[dict]) -> dict[str, str]:
    return {c.lower(): c for c in rows[0].keys()}


def _get(row: dict, cols: dict[str, str], *names: str, default: str = "") -> str:
    for n in names:
        col = cols.get(n.lower())
        if col is not None and row.get(col) is not None:
            v = str(row[col]).strip()
            if v:
                return v
    return default


def _load_by_id(rows: list[dict], cols: dict[str, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        pid = _get(row, cols, "player_id", "id")
        rank_s = _get(row, cols, "rank", "overall_rank", "overall rank")
        if not pid or not rank_s:
            continue
        try:
            out[pid] = float(rank_s)
        except ValueError:
            continue
    return out


def _load_by_name(
    rows: list[dict], cols: dict[str, str], players: dict[str, Player]
) -> tuple[dict[str, float], list[str]]:
    # Build name -> [Player] index (ambiguous names get team/pos filtering)
    by_name: dict[str, list[Player]] = {}
    for p in players.values():
        by_name.setdefault(p.name, []).append(p)
    names_list = list(by_name.keys())

    out: dict[str, float] = {}
    unresolved: list[str] = []
    for row in rows:
        name = _get(row, cols, "name", "player", "player name", "player_name")
        if not name:
            continue
        rank_s = _get(row, cols, "rank", "overall_rank", "overall rank", "ovr")
        if not rank_s:
            continue
        try:
            rank = float(rank_s)
        except ValueError:
            continue
        team = _normalize_team(_get(row, cols, "team", "tm"))
        pos = _normalize_pos(_get(row, cols, "position", "pos"))

        candidates = by_name.get(name, [])
        if not candidates:
            match = process.extractOne(name, names_list, scorer=fuzz.WRatio, score_cutoff=88)
            if not match:
                unresolved.append(f"{name} ({team} {pos})")
                continue
            candidates = by_name.get(match[0], [])

        if len(candidates) > 1 and (team or pos):
            filtered = [
                c
                for c in candidates
                if (not pos or c.position == pos) and (not team or c.team == team)
            ]
            if filtered:
                candidates = filtered

        if not candidates:
            unresolved.append(f"{name} ({team} {pos})")
            continue
        out[candidates[0].id] = rank
    return out, unresolved


def load_csv_override(
    league_id: str, players: dict[str, Player] | None = None
) -> dict[str, float]:
    """Load per-league CSV at data/rankings/<league_id>.csv.

    Two supported schemas:
      - ID-based: columns include `player_id` and `rank`. Works without `players`.
      - Name-based: columns include `name`/`player` and `rank`, optional `team`/`pos`.
        Requires `players` to resolve names to MFL IDs.

    Unresolved name-based rows are silently dropped here; use `import_rankings`
    in the CLI to see resolution diagnostics.
    """
    path = OVERRIDES_DIR / f"{league_id}.csv"
    if not path.exists():
        return {}
    with path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    cols = _cols(rows)
    if "player_id" in cols or "id" in cols:
        return _load_by_id(rows, cols)
    if players is None:
        return {}
    resolved, _ = _load_by_name(rows, cols, players)
    return resolved


def diagnose_csv(
    league_id: str, players: dict[str, Player]
) -> tuple[int, int, list[str]]:
    """Return (rows_total, resolved_count, unresolved_names) for diagnostics."""
    path = OVERRIDES_DIR / f"{league_id}.csv"
    if not path.exists():
        return (0, 0, [])
    with path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return (0, 0, [])
    cols = _cols(rows)
    total = len(rows)
    if "player_id" in cols or "id" in cols:
        return (total, len(_load_by_id(rows, cols)), [])
    resolved, unresolved = _load_by_name(rows, cols, players)
    return (total, len(resolved), unresolved)


def parse_mfl_adp(
    adp_data: dict, exclude_player_ids: set[str] | None = None
) -> dict[str, float]:
    raw = adp_data.get("player", [])
    if isinstance(raw, dict):
        raw = [raw]
    out: dict[str, float] = {}
    excluded = exclude_player_ids or set()
    for p in raw:
        pid = p.get("id")
        if not pid or pid in excluded:
            continue
        avg = p.get("averagePick") or p.get("avgPick")
        if avg is None:
            continue
        try:
            out[pid] = float(avg)
        except (TypeError, ValueError):
            continue
    return out


def rookie_ids(players: dict[str, Player]) -> set[str]:
    return {pid for pid, p in players.items() if p.is_rookie}


def get_filtered_adp(adp_data: dict, players: dict[str, Player]) -> dict[str, float]:
    """Return the primary ADP source for ranking.

    Default: eliminator-only (settings 'eliminator_only_adp' = True). Players without
    eliminator ADP are returned unranked.

    If 'eliminator_only_adp' is False: layered — eliminator ADP wins, MFL ADP fills gaps.
    The per-league CSV override is applied on top by callers via merge_rankings.
    """
    from ffassist import settings_store

    # Prefer MFL self-aggregated ADP; fall back to spoonfull's CSV if our cache is empty
    try:
        from ffassist.mfl_adp import cached_adp as mfl_adp_cached

        elim = mfl_adp_cached()
    except Exception:
        try:
            from ffassist.eliminator_adp import cached_adp as elim_cached

            elim = elim_cached()
        except Exception:
            elim = {}

    if settings_store.get("eliminator_only_adp"):
        return dict(elim)

    # Layered mode (legacy): MFL ADP base + eliminator on top
    exclude: set[str] = set()
    if settings_store.get("exclude_rookies_from_adp"):
        exclude |= rookie_ids(players)
    mfl_adp = parse_mfl_adp(adp_data, exclude_player_ids=exclude)
    merged = dict(mfl_adp)
    merged.update(elim)
    return merged


def get_adp_with_source(
    adp_data: dict, players: dict[str, Player], league_id: str | None = None
) -> dict[str, tuple[float, str]]:
    """Return {player_id: (rank, source_label)} for diagnostic / transparent display.

    Sources: 'elim' (eliminator ADP), 'csv' (per-league CSV override), 'mfl' (MFL ADP fallback).
    """
    from ffassist import settings_store

    out: dict[str, tuple[float, str]] = {}

    try:
        from ffassist.mfl_adp import cached_adp as mfl_adp_cached

        for pid, rank in mfl_adp_cached().items():
            out[pid] = (rank, "elim")
    except Exception:
        try:
            from ffassist.eliminator_adp import cached_adp as elim_cached

            for pid, rank in elim_cached().items():
                out[pid] = (rank, "elim")
        except Exception:
            pass

    if not settings_store.get("eliminator_only_adp"):
        exclude: set[str] = set()
        if settings_store.get("exclude_rookies_from_adp"):
            exclude |= rookie_ids(players)
        for pid, rank in parse_mfl_adp(adp_data, exclude_player_ids=exclude).items():
            out.setdefault(pid, (rank, "mfl"))

    if league_id:
        for pid, rank in load_csv_override(league_id, players=players).items():
            out[pid] = (rank, "csv")  # CSV wins
    return out


def merge_rankings(
    adp: dict[str, float], override: dict[str, float]
) -> dict[str, float]:
    merged = dict(adp)
    merged.update(override)
    return merged


def rank_players(
    players: list[Player], rankings: dict[str, float]
) -> list[tuple[Player, float | None]]:
    out = [(p, rankings.get(p.id)) for p in players]
    out.sort(key=lambda t: (t[1] is None, t[1] if t[1] is not None else 9999.0))
    return out


def best_available(
    players: list[Player], rankings: dict[str, float], n: int = 10
) -> list[tuple[Player, float | None]]:
    return rank_players(players, rankings)[:n]


def blended_rank(
    players: list[Player],
    adp: dict[str, float],
    ppg: dict[str, float],
    weight_ppg: float = 0.6,
    weight_adp: float = 0.4,
    already_drafted: dict[str, int] | None = None,
) -> list[tuple[Player, dict]]:
    """Rank players by Blend = w_ppg * PPG_rank + w_adp * raw_ADP_value. Lower blended = better.

    Formula details:
    - PPG_rank: 1 = highest 2025 PPG, ascending. Players with no PPG get last+1.
    - raw_ADP_value: the actual average pick number from eliminator data (e.g., 2.4, 8.7).
      Players with no ADP get max_adp + 1, so they all tie on the ADP side and sort by PPG.
    - Rookies (no 2025 stats): use 100% raw_ADP_value (no PPG component).
    - already_drafted: blend *= (1 + 0.05 * N) where N is times Scott has drafted that player
      across other tracked leagues. Penalizes over-concentration.

    Returns (player, info) — info has ppg, ppg_rank, adp, blend, dupes, penalty_pct, is_rookie.
    """
    # PPG rank (1 = best)
    have_ppg = [p for p in players if p.id in ppg]
    have_ppg.sort(key=lambda p: -ppg[p.id])
    ppg_rank_map = {p.id: i + 1 for i, p in enumerate(have_ppg)}
    no_ppg_rank = len(have_ppg) + 1

    # ADP raw values — players without ADP get max_adp + 1 (compete on PPG)
    have_adp_vals = [adp[p.id] for p in players if p.id in adp]
    max_adp = max(have_adp_vals) if have_adp_vals else 0.0
    no_adp_value = max_adp + 1.0

    already_drafted = already_drafted or {}

    results = []
    for p in players:
        pr = ppg_rank_map.get(p.id, no_ppg_rank)
        av = adp.get(p.id, no_adp_value)  # raw ADP value

        if p.is_rookie:
            base_blend = av  # rookies: 100% raw ADP
        else:
            base_blend = weight_ppg * pr + weight_adp * av

        dupes = already_drafted.get(p.id, 0)
        penalty_pct = 5 * dupes
        blend = base_blend * (1 + 0.05 * dupes)

        results.append(
            (
                p,
                {
                    "ppg": ppg.get(p.id),
                    "ppg_rank": pr if p.id in ppg else None,
                    "adp": adp.get(p.id),
                    "blend": blend,
                    "dupes": dupes,
                    "penalty_pct": penalty_pct,
                    "is_rookie": p.is_rookie,
                },
            )
        )
    results.sort(key=lambda x: x[1]["blend"])
    return results


def best_available_blended(
    players: list[Player],
    adp: dict[str, float],
    ppg: dict[str, float],
    n: int = 10,
    weight_ppg: float | None = None,
    weight_adp: float | None = None,
    already_drafted: dict[str, int] | None = None,
) -> list[tuple[Player, dict]]:
    from ffassist import settings_store

    wp = weight_ppg if weight_ppg is not None else settings_store.get("blend_weight_ppg")
    wa = weight_adp if weight_adp is not None else settings_store.get("blend_weight_adp")
    return blended_rank(
        players, adp, ppg, weight_ppg=wp, weight_adp=wa, already_drafted=already_drafted
    )[:n]


def my_drafted_counts(mfl_client, league_ids: list[str], my_username: str) -> dict[str, int]:
    """Count how many times user has drafted each player across all tracked leagues."""
    from ffassist.draft_state import parse_picks, my_picks
    from ffassist.poller import find_my_franchise

    counts: dict[str, int] = {}
    for lid in league_ids:
        try:
            league = mfl_client.league(lid)
            picks = parse_picks(mfl_client.draft_results(lid))
            my_id = find_my_franchise(league, my_username)
            if not my_id:
                continue
            for p in my_picks(picks, my_id):
                counts[p.player_id] = counts.get(p.player_id, 0) + 1
        except Exception:
            continue
    return counts
