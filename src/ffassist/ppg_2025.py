"""Multi-year per-game NFL scoring under 2026 FCEliminator rules.

Pulls weekly playerScores for 2024 + 2025 from a 2026 league (so MFL applies
2026 rules to historical raw stats). Stores per-year so downstream callers can
blend (60% 2025 / 40% 2024) and apply per-year injury multipliers.

Cached forever — historical stats don't change.
"""

from __future__ import annotations

import json
import time

from ffassist.config import DATA_DIR
from ffassist.mfl.client import MFLClient

CACHE_PATH = DATA_DIR / "ppg_2025.json"  # legacy filename; content is multi-year
YEARS = (2025, 2024)
WEEKS = list(range(1, 19))


def _fetch_year(mfl: MFLClient, league_id: str, year: int) -> dict[str, dict[int, float]]:
    weekly: dict[str, dict[int, float]] = {}
    for w in WEEKS:
        data = mfl._export("playerScores", league_id, YEAR=year, W=str(w), force=False)
        scores = data.get("playerScores", {}).get("playerScore", [])
        if isinstance(scores, dict):
            scores = [scores]
        for s in scores:
            pid = s.get("id")
            try:
                pts = float(s.get("score", 0))
            except (TypeError, ValueError):
                pts = 0.0
            if pid:
                weekly.setdefault(pid, {})[w] = pts
    return weekly


def fetch(league_id: str = "37714") -> dict[str, dict]:
    """Fetch weekly scores for 2024 and 2025; return {pid: {'2025': {...}, '2024': {...}}}.

    games_played counts every week MFL has an entry for the player (including 0-score
    weeks) — captures "healthy but didn't play" as 0 toward PPG. Players genuinely on
    bye / IR / suspended don't appear in the weekly scores and aren't counted.
    """
    out: dict[str, dict] = {}
    with MFLClient(year=2026) as mfl:
        for year in YEARS:
            weekly = _fetch_year(mfl, league_id, year)
            for pid, weeks in weekly.items():
                gp = len(weeks)  # any entry = "played or could have"
                total = sum(weeks.values())
                ppg = round(total / gp, 4) if gp > 0 else 0.0
                out.setdefault(pid, {})[str(year)] = {
                    "ppg": ppg,
                    "games_played": gp,
                    "total": round(total, 2),
                }
    return out


def cached(force_refresh: bool = False) -> dict[str, dict]:
    """Return per-player per-year {ppg, games_played, total}. Cached forever."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not force_refresh:
        try:
            entry = json.loads(CACHE_PATH.read_text())
            data = entry.get("data", {})
            # Detect legacy single-year flat schema and refresh
            if data:
                sample = next(iter(data.values()))
                if isinstance(sample, dict) and ("2025" in sample or "2024" in sample):
                    return data
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    data = fetch()
    CACHE_PATH.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
    return data


def cached_ppg_only() -> dict[str, float]:
    """Legacy-compatible flat ppg dict for callers using best_available_blended.

    Returns the same 60/40 blend with COMBINED-year injury multiplier the main
    ranking uses. Age regression NOT applied here.
    """
    out: dict[str, float] = {}
    for pid, info in cached().items():
        v25 = info.get("2025", {})
        v24 = info.get("2024", {})
        gp25 = int(v25.get("games_played", 0) or 0)
        gp24 = int(v24.get("games_played", 0) or 0)
        ppg25 = v25.get("ppg") if gp25 > 0 else None
        ppg24 = v24.get("ppg") if gp24 > 0 else None
        # Combined injury multiplier: total missed across both years, cap 10%
        combined_missed = max(0, 34 - gp25 - gp24)
        mult = max(0.90, 1 - 0.01 * combined_missed)
        adj25 = ppg25 * mult if ppg25 is not None else None
        adj24 = ppg24 * mult if ppg24 is not None else None
        if adj25 is not None and adj24 is not None:
            out[pid] = 0.6 * adj25 + 0.4 * adj24
        elif adj25 is not None:
            out[pid] = adj25
        elif adj24 is not None:
            out[pid] = adj24
    return out


def cache_status() -> dict:
    if not CACHE_PATH.exists():
        return {"present": False}
    try:
        entry = json.loads(CACHE_PATH.read_text())
    except Exception:
        return {"present": True, "error": "unreadable"}
    return {
        "present": True,
        "count": len(entry.get("data", {})),
        "fetched_at": entry.get("fetched_at"),
    }
