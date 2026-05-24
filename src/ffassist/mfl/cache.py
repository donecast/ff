from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ffassist.config import DATA_DIR

CACHE_DIR = DATA_DIR / "mfl_cache"


def _key(year: int, type_: str, params: dict) -> str:
    norm = {k: v for k, v in sorted(params.items()) if v is not None}
    blob = json.dumps([year, type_, norm], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def get(year: int, type_: str, params: dict, ttl: float) -> dict | None:
    if ttl <= 0:
        return None
    path = CACHE_DIR / f"{_key(year, type_, params)}.json"
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - entry["fetched_at"] > ttl:
        return None
    return entry["data"]


def put(year: int, type_: str, params: dict, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_key(year, type_, params)}.json"
    path.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
