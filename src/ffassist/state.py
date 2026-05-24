from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ffassist.config import DATA_DIR

STATE_FILE = DATA_DIR / "draft_threads.json"


def load() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"threads": {}, "leagues": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"threads": {}, "leagues": {}}


def save(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
