"""Fantasy Cares Eliminator full pick log from spoonfull's data repo.

Every individual pick across every tracked eliminator league:
  league_name, league_id, league_home, timestamp, round, pick, overall,
  franchise_id, franchise_name, player_id, player_name, pos, age, team,
  time_to_pick_int, time_to_pick

Cached 1h. Source updates roughly every 1-4 hours during active drafting.
"""

from __future__ import annotations

import csv
import io
import json
import time

import httpx

from ffassist.config import DATA_DIR

URL = "https://github.com/mohanpatrick/elim-data-2025/releases/download/data-mfl/all_picks.csv"
CACHE_PATH = DATA_DIR / "eliminator_picks.json"
TTL = 3600


def fetch() -> list[dict]:
    r = httpx.get(URL, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def cached(force_refresh: bool = False) -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not force_refresh:
        try:
            entry = json.loads(CACHE_PATH.read_text())
            if time.time() - entry.get("fetched_at", 0) < TTL:
                return entry["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    try:
        data = fetch()
    except Exception:
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text())["data"]
            except Exception:
                pass
        raise
    CACHE_PATH.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
    return data


def cache_status() -> dict:
    if not CACHE_PATH.exists():
        return {"present": False}
    try:
        entry = json.loads(CACHE_PATH.read_text())
    except Exception:
        return {"present": True, "error": "unreadable"}
    age_sec = time.time() - entry.get("fetched_at", 0)
    return {
        "present": True,
        "count": len(entry.get("data", [])),
        "age_sec": int(age_sec),
        "stale": age_sec > TTL,
    }


def _overall(row: dict) -> int:
    try:
        return int(row.get("overall", 0))
    except (TypeError, ValueError):
        return 0


# ---------- query helpers ----------


def picks_by_player_name(query: str, *, limit: int = 50) -> list[dict]:
    """Fuzzy-match player name; return list of picks (each row from the CSV)."""
    from rapidfuzz import fuzz

    rows = cached()
    name_set = list({r["player_name"] for r in rows})
    # Quick fuzz: keep rows whose player_name fuzzy-matches at >=80
    keep = set()
    q_lower = query.lower()
    for n in name_set:
        if q_lower in n.lower() or fuzz.token_sort_ratio(query, n) >= 80:
            keep.add(n)
    matched = [r for r in rows if r["player_name"] in keep]
    matched.sort(key=_overall)
    return matched[:limit]


def picks_in_overall_range(min_overall: int, max_overall: int, *, position: str | None = None) -> list[dict]:
    rows = cached()
    out = [r for r in rows if min_overall <= _overall(r) <= max_overall]
    if position:
        pos = position.upper()
        out = [r for r in out if r.get("pos", "").upper() == pos]
    out.sort(key=lambda r: (_overall(r), r.get("league_id", "")))
    return out


def picks_count_by_player_id() -> dict[str, int]:
    """Return {mfl_player_id: number_of_FCE_drafts_this_player_was_selected_in}.

    Counts each (league_id, player_id) pair once — a player picked in 12 different
    FCE leagues returns 12, even if the source CSV has duplicate rows.
    """
    rows = cached()
    seen: set[tuple[str, str]] = set()
    counter: dict[str, int] = {}
    for r in rows:
        pid = r.get("player_id") or ""
        lid = r.get("league_id") or ""
        if not pid:
            continue
        key = (lid, pid)
        if key in seen:
            continue
        seen.add(key)
        counter[pid] = counter.get(pid, 0) + 1
    return counter


def position_distribution_at_pick(overall_pick: int, *, window: int = 0) -> dict[str, int]:
    """Count positions taken at `overall_pick` (+/- window) across all leagues."""
    rows = cached()
    lo, hi = overall_pick - window, overall_pick + window
    counter: dict[str, int] = {}
    for r in rows:
        if lo <= _overall(r) <= hi:
            pos = r.get("pos", "?")
            counter[pos] = counter.get(pos, 0) + 1
    return counter
