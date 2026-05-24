"""Fantasy Cares Eliminator ADP from spoonfull's data repo.

Pulls adp_mfl.csv from https://github.com/mohanpatrick/elim-data-2025 — a community
data project that aggregates eliminator drafts across MFL, excluding rookie / RB-only
/ auction / BBID / trading drafts. Uses MFL player IDs directly so no name matching.

Cached locally for 1 hour.
"""

from __future__ import annotations

import csv
import io
import json
import time
from pathlib import Path

import httpx

from ffassist.config import DATA_DIR

ADP_URL = "https://github.com/mohanpatrick/elim-data-2025/releases/download/data-mfl/adp_mfl.csv"
META_URL = "https://github.com/mohanpatrick/elim-data-2025/releases/download/data-mfl/adp_metadata.csv"
TS_URL = "https://github.com/mohanpatrick/elim-data-2025/releases/download/data-mfl/timestamp.txt"

CACHE_PATH = DATA_DIR / "eliminator_adp.json"
CACHE_TTL_SEC = 3600


def fetch_adp() -> dict[str, float]:
    """Fetch eliminator ADP CSV and return {mfl_player_id: overall_avg_pick}."""
    r = httpx.get(ADP_URL, timeout=20.0, follow_redirects=True)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    out: dict[str, float] = {}
    for row in reader:
        pid = (row.get("player_id") or "").strip()
        avg = row.get("overall_avg") or ""
        if not pid or not avg:
            continue
        try:
            out[pid] = float(avg)
        except ValueError:
            continue
    return out


def fetch_metadata() -> dict[str, str] | None:
    try:
        r = httpx.get(META_URL, timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        return next(reader, None)
    except Exception:
        return None


def fetch_timestamp() -> str | None:
    try:
        r = httpx.get(TS_URL, timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return None


def cached_adp(force_refresh: bool = False) -> dict[str, float]:
    """Read ADP from local cache; fetch if stale or missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists() and not force_refresh:
        try:
            entry = json.loads(CACHE_PATH.read_text())
            if time.time() - entry.get("fetched_at", 0) < CACHE_TTL_SEC:
                return entry["data"]
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    try:
        data = fetch_adp()
    except Exception as e:
        # Fall back to stale cache rather than nothing
        if CACHE_PATH.exists():
            try:
                entry = json.loads(CACHE_PATH.read_text())
                return entry["data"]
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
        "count": len(entry.get("data", {})),
        "age_sec": int(age_sec),
        "stale": age_sec > CACHE_TTL_SEC,
    }
