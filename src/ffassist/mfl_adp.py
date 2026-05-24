"""Self-aggregated Fantasy Cares Eliminator ADP, pulled directly from MFL.

Replaces the dependency on spoonfull's CSV. Discovers leagues via MFL's leagueSearch,
filters out auction/BBID/rookie/IDP/NON-SCORING variants, pulls draftResults for each,
and computes per-player ADP.

Min draft threshold: 3 (configurable). Cached 1h.
"""

from __future__ import annotations

import json
import re
import time

from ffassist.config import DATA_DIR
from ffassist.draft_state import parse_picks
from ffassist.mfl.client import MFLClient

CACHE_PATH = DATA_DIR / "mfl_eliminator_adp.json"
TTL_SEC = 3600
MIN_DRAFTS_VET = 3
MIN_DRAFTS_ROOKIE = 1

# Case-insensitive substrings — any match excludes the league
EXCLUDE_PATTERNS = [
    "NON SCORING",
    "NON Scoring",
    "non scoring",
    "Rookie",
    "rookie",
    "BBID",
    "Auction",
    "auction",
    "IDP",
    "with Trading",
    "trading",
]


def is_eligible(league_name: str) -> bool:
    n = league_name.lower()
    for p in EXCLUDE_PATTERNS:
        if p.lower() in n:
            return False
    return True


def _normalize_home_url(url: str) -> str:
    """MFL sometimes returns 'https//' missing the colon. Fix it."""
    return url.replace("https//", "https://").replace("http//", "http://")


def _seed_league_hosts(leagues: list[dict], year: int) -> int:
    """Populate state['league_hosts'] from each league's homeURL so subsequent
    draft_results calls go directly to the right wwwXX host instead of api.*.

    Prevents 429 cascades on the rate-limited api.myfantasyleague.com host.
    """
    from ffassist import state as state_mod

    state = state_mod.load()
    hosts = state.setdefault("league_hosts", {})
    added = 0
    for lg in leagues:
        lid = lg.get("id")
        url = _normalize_home_url(lg.get("homeURL", ""))
        if not lid or "://" not in url:
            continue
        scheme, rest = url.split("://", 1)
        host = rest.split("/", 1)[0]
        host_key = f"{scheme}://{host}/{year}"
        if hosts.get(lid) != host_key:
            hosts[lid] = host_key
            added += 1
    if added:
        state_mod.save(state)
    return added


def discover_leagues(year: int) -> list[dict]:
    """leagueSearch for FCEliminator, return eligible leagues only.

    Side effect: seeds state['league_hosts'] from homeURL so future per-league calls
    skip the rate-limited api.* endpoint.
    """
    with MFLClient(year=year) as mfl:
        data = mfl._export("leagueSearch", None, SEARCH="FCEliminator", YEAR=year, force=False)
    raw = data.get("leagues", {})
    if isinstance(raw, dict):
        lst = raw.get("league", [])
    else:
        lst = raw
    if isinstance(lst, dict):
        lst = [lst]
    eligible = [l for l in lst if is_eligible(l.get("name", ""))]
    _seed_league_hosts(eligible, year)
    return eligible


def aggregate(
    year: int,
    min_drafts_vet: int = MIN_DRAFTS_VET,
    min_drafts_rookie: int = MIN_DRAFTS_ROOKIE,
) -> tuple[dict[str, float], dict]:
    """Pull draftResults for every eligible league, return ({player_id: overall_avg}, metadata).

    Rookies (MFL status='R') qualify with min_drafts_rookie picks (default 1); vets need min_drafts_vet (default 3).
    """
    from ffassist.draft_state import parse_players

    leagues = discover_leagues(year)
    picks_by_player: dict[str, list[int]] = {}
    league_count_used = 0
    league_count_skipped = 0
    total_picks = 0
    with MFLClient(year=year) as mfl:
        players_lookup = parse_players(mfl.players())
        for lg in leagues:
            lid = lg.get("id")
            if not lid:
                continue
            try:
                draft = mfl.draft_results(lid)
            except Exception:
                league_count_skipped += 1
                continue
            picks = parse_picks(draft)
            if not picks:
                league_count_skipped += 1
                continue
            league_count_used += 1
            total_picks += len(picks)
            # Team count must come from the league record — inferring from picks
            # underestimates it when round 1 isn't complete yet.
            team_count = 0
            try:
                lg_obj = mfl.league(lid)
                fr = lg_obj.get("franchises", {}).get("franchise", [])
                if isinstance(fr, dict):
                    fr = [fr]
                team_count = len(fr)
            except Exception:
                pass
            if not team_count:
                # Last-resort fallback — best signal from picks alone
                team_count = max((p.pick for p in picks), default=0)
            if not team_count:
                continue
            for p in picks:
                overall = (p.round - 1) * team_count + p.pick
                picks_by_player.setdefault(p.player_id, []).append(overall)

    adp: dict[str, float] = {}
    rookies_included = 0
    for pid, p_list in picks_by_player.items():
        p = players_lookup.get(pid)
        is_rookie = bool(p and p.is_rookie)
        min_required = min_drafts_rookie if is_rookie else min_drafts_vet
        if len(p_list) < min_required:
            continue
        adp[pid] = sum(p_list) / len(p_list)
        if is_rookie:
            rookies_included += 1

    meta = {
        "year": year,
        "total_leagues_discovered": len(leagues),
        "leagues_with_picks": league_count_used,
        "leagues_skipped": league_count_skipped,
        "total_picks_aggregated": total_picks,
        "min_drafts_vet": min_drafts_vet,
        "min_drafts_rookie": min_drafts_rookie,
        "players_ranked": len(adp),
        "rookies_included": rookies_included,
        "computed_at": time.time(),
    }
    return adp, meta


def cached_adp(force_refresh: bool = False, year: int | None = None) -> dict[str, float]:
    """Read from cache, refresh if stale or missing. Caller should expect a slow first call."""
    from ffassist.config import settings

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not force_refresh:
        try:
            entry = json.loads(CACHE_PATH.read_text())
            if time.time() - entry.get("fetched_at", 0) < TTL_SEC:
                return entry["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    y = year or settings.mfl_year
    try:
        adp, meta = aggregate(y)
    except Exception:
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text())["data"]
            except Exception:
                pass
        raise

    CACHE_PATH.write_text(
        json.dumps({"fetched_at": time.time(), "data": adp, "meta": meta})
    )
    return adp


def cache_status() -> dict:
    if not CACHE_PATH.exists():
        return {"present": False}
    try:
        entry = json.loads(CACHE_PATH.read_text())
    except Exception:
        return {"present": True, "error": "unreadable"}
    age = time.time() - entry.get("fetched_at", 0)
    return {
        "present": True,
        "count": len(entry.get("data", {})),
        "age_sec": int(age),
        "stale": age > TTL_SEC,
        "meta": entry.get("meta", {}),
    }
