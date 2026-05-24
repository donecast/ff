"""FantasyPros public v2 API client.

Read-only enrichment layer: ECR/tiers, projections, news, and FP↔MFL id mapping.
Does NOT feed into rankings_v2 scoring — it's context overlay only.

Rate limits (free/public tier): 1 req/sec, 100/day. We enforce both with a
disk-backed daily counter and a 2s floor between calls. Cache aggressively.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ffassist.config import DATA_DIR, settings

BASE_URL = "https://api.fantasypros.com/public/v2/json"
CACHE_DIR = DATA_DIR / "fp_cache"
COUNTER_FILE = DATA_DIR / "fp_calls.json"
MAP_FILE = DATA_DIR / "fp_to_mfl.json"

DAILY_LIMIT = 100
MIN_INTERVAL_SEC = 2.0

# TTLs in seconds — be generous; FP data updates daily at most
TTL_CONSENSUS = 6 * 3600       # 6h
TTL_PROJECTIONS = 12 * 3600    # 12h
TTL_PLAYERS = 24 * 3600        # 24h
TTL_NEWS = 15 * 60             # 15m


class FPError(RuntimeError):
    pass


class FPRateLimitExceeded(FPError):
    pass


@dataclass
class CallCounter:
    date: str  # YYYY-MM-DD UTC
    count: int
    last_call_ts: float

    @classmethod
    def load(cls) -> "CallCounter":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not COUNTER_FILE.exists():
            return cls(date=today, count=0, last_call_ts=0.0)
        try:
            d = json.loads(COUNTER_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return cls(date=today, count=0, last_call_ts=0.0)
        if d.get("date") != today:
            return cls(date=today, count=0, last_call_ts=0.0)
        return cls(date=today, count=int(d.get("count", 0)), last_call_ts=float(d.get("last_call_ts", 0.0)))

    def save(self) -> None:
        COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        COUNTER_FILE.write_text(json.dumps({
            "date": self.date,
            "count": self.count,
            "last_call_ts": self.last_call_ts,
        }))


def _cache_path(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


def _cache_get(url: str, ttl: int) -> dict | None:
    p = _cache_path(url)
    if not p.exists():
        return None
    try:
        wrapped = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    ts = wrapped.get("_ts", 0)
    if time.time() - ts > ttl:
        return None
    return wrapped.get("data")


def _cache_put(url: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(url)
    p.write_text(json.dumps({"_ts": time.time(), "data": data}))


def _get(path: str, params: dict[str, Any], ttl: int, force: bool = False) -> dict:
    if not settings.fp_api_key:
        raise FPError("FP_API_KEY not set in environment")

    # Build URL with deterministic param order for stable cache keys
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"

    if not force:
        cached = _cache_get(url, ttl)
        if cached is not None:
            return cached

    counter = CallCounter.load()
    if counter.count >= DAILY_LIMIT:
        raise FPRateLimitExceeded(
            f"FP daily limit reached ({counter.count}/{DAILY_LIMIT}). Resets at UTC midnight."
        )
    wait = MIN_INTERVAL_SEC - (time.time() - counter.last_call_ts)
    if wait > 0:
        time.sleep(wait)

    headers = {"x-api-key": settings.fp_api_key}
    try:
        r = httpx.get(url, headers=headers, timeout=30.0)
    except httpx.HTTPError as e:
        raise FPError(f"FP request failed: {e!r}") from e

    counter.count += 1
    counter.last_call_ts = time.time()
    counter.save()

    if r.status_code != 200:
        raise FPError(f"FP {r.status_code} on {path}: {r.text[:200]}")

    data = r.json()
    _cache_put(url, data)
    return data


# ---- Public API ----

def get_consensus(
    position: str = "ALL",
    type_: str = "draft",
    scoring: str = "PPR",
    week: int = 0,
    season: int | None = None,
    force: bool = False,
) -> dict:
    """Consensus rankings (ECR). position: QB/RB/WR/TE/K/DST/OP/ALL.
    type_: draft/weekly/ros/dynasty. scoring: PPR/HALF/STD.
    """
    season = season or settings.mfl_year
    return _get(
        f"/nfl/{season}/consensus-rankings",
        {
            "position": position,
            "type": type_,
            "scoring": scoring,
            "week": week,
            "experts": "available",
        },
        TTL_CONSENSUS,
        force=force,
    )


def get_projections(
    position: str = "ALL",
    scoring: str = "PPR",
    week: int = 0,
    season: int | None = None,
    force: bool = False,
) -> dict:
    """Projections — response includes BOTH `fpid` and `mflid` per player."""
    season = season or settings.mfl_year
    return _get(
        f"/nfl/{season}/projections",
        {"position": position, "scoring": scoring, "week": week},
        TTL_PROJECTIONS,
        force=force,
    )


def get_players(force: bool = False) -> dict:
    """Full ~8k player directory with rank_ecr_ppr, rank_ecr_half, rank_adp_ppr."""
    return _get("/nfl/players", {}, TTL_PLAYERS, force=force)


def get_news(force: bool = False) -> dict:
    """Recent player news with impact field."""
    return _get("/nfl/news", {}, TTL_NEWS, force=force)


# ---- FP ↔ MFL id mapping ----

def build_fp_to_mfl_map(force: bool = False) -> dict[str, str]:
    """Build {fpid: mflid} from the projections endpoint and persist to disk.

    A single projections call covers every player FantasyPros projects for the
    season — typically every relevant fantasy player. Costs 1 daily call.
    """
    if not force and MAP_FILE.exists():
        try:
            return json.loads(MAP_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    mapping: dict[str, str] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        # Some positions return more useful mflids than ALL alone; iterate to be safe.
        try:
            data = get_projections(position=pos, force=force)
        except FPError:
            continue
        for pl in data.get("players", []):
            fpid = pl.get("fpid")
            mflid = pl.get("mflid")
            if fpid and mflid:
                mapping[str(fpid)] = str(mflid)

    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    return mapping


def load_fp_to_mfl_map() -> dict[str, str]:
    """Read-only: load the mapping from disk, or return {} if not built yet."""
    if not MAP_FILE.exists():
        return {}
    try:
        return json.loads(MAP_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def mfl_to_fp_map() -> dict[str, str]:
    return {v: k for k, v in load_fp_to_mfl_map().items()}


# ---- Convenience: enrich MFL player by mflid ----

@dataclass
class FPEnrichment:
    fpid: str | None
    ecr: int | None
    tier: int | None
    pos_rank: str | None
    bye: str | None
    rank_min: int | None
    rank_max: int | None
    rank_ave: float | None
    points_ppr: float | None
    news_impact: str | None  # most recent if any in last 24h


def enrich_for_mflids(
    mflids: list[str],
    scoring: str = "PPR",
    type_: str = "draft",
) -> dict[str, FPEnrichment]:
    """Return {mflid: FPEnrichment} for the given MFL player ids.

    Uses cached data only — no force refreshes. Best-effort; missing players
    map to an empty enrichment with all None fields.
    """
    mfl_to_fp = mfl_to_fp_map()

    # Pull consensus rankings ALL once (1 cached call)
    try:
        consensus = get_consensus(position="ALL", type_=type_, scoring=scoring)
    except FPError:
        consensus = {"players": []}
    fp_consensus = {str(p["player_id"]): p for p in consensus.get("players", [])}

    # Pull projections ALL once
    try:
        proj = get_projections(position="ALL", scoring=scoring)
    except FPError:
        proj = {"players": []}
    fp_proj = {str(p["fpid"]): p for p in proj.get("players", [])}

    # News window: last 24h, indexed by FP player_id (string)
    try:
        news = get_news()
    except FPError:
        news = {"items": []}
    now_ts = time.time()
    recent_news: dict[str, str] = {}
    for item in news.get("items", []):
        try:
            created = item.get("created", 0)
            if isinstance(created, str):
                created = int(created)
        except (TypeError, ValueError):
            continue
        if now_ts - created > 24 * 3600:
            continue
        pid = str(item.get("player_id", ""))
        if pid and pid not in recent_news:
            recent_news[pid] = item.get("impact", "") or "unknown"

    out: dict[str, FPEnrichment] = {}
    for mflid in mflids:
        fpid = mfl_to_fp.get(str(mflid))
        if not fpid:
            out[mflid] = FPEnrichment(None, None, None, None, None, None, None, None, None, None)
            continue
        c = fp_consensus.get(fpid, {})
        p = fp_proj.get(fpid, {})
        scoring_key = {"PPR": "points_ppr", "HALF": "points_half", "STD": "points"}.get(scoring.upper(), "points_ppr")
        pts = (p.get("stats") or {}).get(scoring_key)
        out[mflid] = FPEnrichment(
            fpid=fpid,
            ecr=_int_or_none(c.get("rank_ecr")),
            tier=_int_or_none(c.get("tier")),
            pos_rank=c.get("pos_rank"),
            bye=c.get("player_bye_week"),
            rank_min=_int_or_none(c.get("rank_min")),
            rank_max=_int_or_none(c.get("rank_max")),
            rank_ave=_float_or_none(c.get("rank_ave")),
            points_ppr=_float_or_none(pts),
            news_impact=recent_news.get(fpid),
        )
    return out


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calls_today() -> tuple[int, int]:
    c = CallCounter.load()
    return c.count, DAILY_LIMIT
